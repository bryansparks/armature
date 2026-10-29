"""Load harness for live run events (DESIGN.md: 20 runs at 200 events a second, 100 viewers, p99 under 1 s).

The server runs in this process on a free port. Emitters and SSE viewers run in
child processes, so their CPU doesn't slow the server's event loop. Half the
runs send over the Unix socket and half over HTTP, through the real emitter.

    python -m tests.server.live_load [--db URL] [--runs 20] [--rate 200] [--viewers 100] [--seconds 15]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import os
import statistics
import tempfile
import threading
import time
from typing import Any

PAYLOAD_TEXT = "x" * 200


def _pct(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))]


def _emit_worker(jobs: list[tuple[str, str]], rate: int, seconds: float, start_at: float, out) -> None:
    from armature.events.emitter import Emitter

    def one(endpoint: str, token: str, stats: list) -> None:
        emitter = Emitter(endpoint, token)
        while time.time() < start_at:
            time.sleep(0.005)
        interval = 1.0 / rate
        sent = 0
        began = time.monotonic()
        while time.monotonic() - began < seconds:
            due = began + sent * interval
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if sent % 50 == 0:
                emitter.emit("cost", "step", input_tokens=10, output_tokens=2, cost_usd=0.001, t=time.time())
            elif sent % 10 == 0:
                emitter.emit("step_start" if sent % 20 == 0 else "step_end", f"s{sent // 20 % 5}", t=time.time())
            else:
                emitter.emit("tool_call", "step", tool="Bash", input=PAYLOAD_TEXT, t=time.time())
            sent += 1
        emitter.emit("run_end", None, status="succeeded", t=time.time())
        emitter.flush(10)
        stats.append({"emitted": sent + 1, **emitter.stats()})
        emitter.close()

    stats: list[dict] = []
    threads = [threading.Thread(target=one, args=(e, t, stats)) for e, t in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    out.put(stats)


def _view_worker(origin: str, cookie: dict[str, str], run_ids: list[str], seconds: float, out) -> None:
    import httpx

    latencies: list[float] = []
    counts = {"frames": 0, "reconnects": 0, "errors": 0}

    async def view(client: httpx.AsyncClient, run_id: str, deadline: float) -> None:
        last = 0
        while time.time() < deadline:
            try:
                headers = {"Last-Event-ID": str(last)} if last else {}
                async with client.stream("GET", f"/api/runs/{run_id}/stream", headers=headers) as response:
                    data = event = None
                    async for text in response.aiter_lines():
                        if text.startswith("event:"):
                            event = text[6:].strip()
                        elif text.startswith("data:"):
                            data = text[5:].strip()
                        elif text.startswith("id:"):
                            last = int(text[3:].strip())
                        elif text == "" and data is not None:
                            if event == "run-event":
                                counts["frames"] += 1
                                sent = json.loads(data)["payload"].get("t")
                                if sent:
                                    latencies.append(time.time() - sent)
                            elif event == "reconnect":
                                counts["reconnects"] += 1
                                break
                            elif event == "run" and json.loads(data)["status"] != "running":
                                return
                            data = event = None
                        if time.time() > deadline:
                            return
            except (httpx.HTTPError, ValueError):
                counts["errors"] += 1
                await asyncio.sleep(0.2)

    async def main() -> None:
        limits = httpx.Limits(max_connections=len(run_ids) + 10)
        async with httpx.AsyncClient(base_url=origin, cookies=cookie, timeout=httpx.Timeout(30, read=60),
                                     limits=limits) as client:
            deadline = time.time() + seconds
            await asyncio.gather(*(view(client, run_id, deadline) for run_id in run_ids))

    asyncio.run(main())
    out.put({"latencies": latencies, **counts})


def run_load(db_url: str | None = None, *, runs: int = 20, rate: int = 200, viewers: int = 100,
             seconds: float = 15.0, emit_procs: int = 4, view_procs: int = 4) -> dict[str, Any]:
    from sqlalchemy import MetaData, func, select

    from armature.server.crypto import Keyring
    from armature.server.db import make_engine, migrate, utcnow
    from armature.server.db.schema import run_events
    from tests.server.app_support import add_user
    from tests.server.live_support import LiveServer, add_runner_key, add_team

    scratch = tempfile.mkdtemp(prefix="armload")
    db_url = db_url or f"sqlite:///{os.path.join(scratch, 'load.db')}"
    if not db_url.startswith("sqlite"):
        engine = make_engine(db_url)
        with engine.begin() as conn:
            existing = MetaData()
            existing.reflect(conn)
            existing.drop_all(conn)
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    keyring = Keyring(os.urandom(32))
    clock = utcnow
    team, _ = add_team(engine, clock, "load")
    admin = add_user(engine, keyring, clock, "loadadmin", is_admin=True)[0]
    key = add_runner_key(engine, keyring, clock, team)
    ctx = mp.get_context("spawn")
    with LiveServer(engine, keyring, clock, sse_per_user=viewers + 10, sse_total=viewers + 50) as live:
        http = live.client()
        registered = []
        for index in range(runs):
            response = http.post("/api/runner/runs", json={"workflow": "load", "actor": "load",
                                                           "trigger_kind": "load", "trigger_ref": {"i": index}},
                                 headers={"Authorization": f"Bearer {key}"})
            body = response.json()
            endpoint = f"unix:{body['events']['unix']}" if index % 2 == 0 else body["events"]["http"]
            registered.append((body["run_id"], endpoint, body["token"]))
        cookie = dict(live.client(admin).cookies)
        out = ctx.Queue()
        targets = [registered[i % runs][0] for i in range(viewers)]
        viewer_procs = [ctx.Process(target=_view_worker, args=(live.origin, cookie, targets[i::view_procs],
                                                               seconds + 20, out)) for i in range(view_procs)]
        for proc in viewer_procs:
            proc.start()
        time.sleep(3)
        start_at = time.time() + 2
        jobs = [(endpoint, token) for _, endpoint, token in registered]
        emit_out = ctx.Queue()
        emitters = [ctx.Process(target=_emit_worker, args=(jobs[i::emit_procs], rate, seconds, start_at, emit_out))
                    for i in range(emit_procs)]
        lag_mark = len(live.hub.lag_ms)
        flush_mark = len(live.hub.writer.flush_ms)
        for proc in emitters:
            proc.start()
        emitted = [s for _ in emitters for s in emit_out.get(timeout=seconds + 120)]
        for proc in emitters:
            proc.join()
        viewed = [out.get(timeout=seconds + 120) for _ in viewer_procs]
        for proc in viewer_procs:
            proc.join()
        live.flush()
        lag = live.hub.lag_ms[lag_mark:]
        flush_ms = live.hub.writer.flush_ms[flush_mark:]
        flush_rows = live.hub.writer.flush_rows[flush_mark:]
        hub_stats = live.hub.stats()
    with engine.connect() as conn:
        stored = conn.execute(select(func.count()).select_from(run_events)).scalar()
    engine.dispose()
    latencies = [v for part in viewed for v in part["latencies"]]
    return {
        "db": db_url.split(":", 1)[0],
        "runs": runs, "rate_per_run": rate, "viewers": viewers, "seconds": seconds,
        "emitted": sum(s["emitted"] for s in emitted),
        "emitter_dropped": sum(s["dropped"] for s in emitted),
        "emitter_dropped_must_keep": sum(s["dropped_must_keep"] for s in emitted),
        "server_accepted": hub_stats["accepted"], "server_rejected": hub_stats["rejected"],
        "socket_dropped": hub_stats["socket_dropped"], "subscriber_overflows": hub_stats["overflows"],
        "stored_events": stored,
        "frames_seen": sum(p["frames"] for p in viewed), "reconnects": sum(p["reconnects"] for p in viewed),
        "viewer_errors": sum(p["errors"] for p in viewed),
        "latency_ms": {"p50": _pct(latencies, 50) * 1000, "p99": _pct(latencies, 99) * 1000,
                       "max": max(latencies, default=float("nan")) * 1000},
        "loop_lag_ms": {"p50": _pct(lag, 50), "p99": _pct(lag, 99), "max": max(lag, default=float("nan"))},
        "flush_ms": {"p50": _pct(flush_ms, 50), "p99": _pct(flush_ms, 99), "max": max(flush_ms, default=0.0)},
        "rows_per_flush": {"mean": statistics.fmean(flush_rows) if flush_rows else 0.0,
                           "max": max(flush_rows, default=0), "flushes": len(flush_rows)},
        "flush_failures": hub_stats["flush_failures"], "writer_dropped": hub_stats["writer_dropped"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None, help="database URL, default a fresh SQLite file")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--rate", type=int, default=200)
    parser.add_argument("--viewers", type=int, default=100)
    parser.add_argument("--seconds", type=float, default=15.0)
    args = parser.parse_args()
    print(json.dumps(run_load(args.db, runs=args.runs, rate=args.rate, viewers=args.viewers, seconds=args.seconds),
                     indent=2))


if __name__ == "__main__":
    main()
