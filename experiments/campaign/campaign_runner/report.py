"""Render a self-contained, shareable report.html — 7 sections, inline SVG."""
from __future__ import annotations

from html import escape
from pathlib import Path

from campaign_runner import svgplot


def _verdict_rows_html(verdicts: list[tuple[str, str, dict]]) -> str:
    rows = []
    for name, result, detail in verdicts:
        color = {"PASS": "#2e7d32", "FAIL": "#c62828", "INCONCLUSIVE": "#f57c00"}.get(result, "#666")
        rows.append(f"<tr><td>{escape(name)}</td><td style='color:{color}'>{result}</td>"
                    f"<td><code>{escape(str(detail))}</code></td></tr>")
    return "\n".join(rows)


def narrative(verdicts: list[tuple[str, str, dict]], rows: list[dict], plan) -> str:
    if not verdicts:
        return "<h2>Narrative</h2><p>(no verdicts recorded)</p>"
    statuses = [v[1] for v in verdicts]
    if any(s == "FAIL" for s in statuses):
        head = "Overall: this run does NOT support the stated purpose — at least one verdict FAILED."
    elif statuses and all(s == "PASS" for s in statuses):
        head = "Overall: this run supports the stated purpose — all verdicts PASS."
    else:
        head = "Overall: this run is INCONCLUSIVE for the stated purpose — one or more verdicts could not be settled."
    word = {"PASS": "good", "FAIL": "bad", "INCONCLUSIVE": "inconclusive"}
    items = "\n".join(
        f"<li><b>{escape(name)}</b>: {word.get(result, result)} — "
        f"<code>{escape(str(detail))}</code></li>"
        for name, result, detail in verdicts)
    return (f"<h2>Narrative</h2><p><b>{head}</b></p><ul>{items}</ul>")


def _soak_metrics_html(verdicts: list[tuple[str, str, dict]]) -> str:
    d = {n: detail for n, _r, detail in verdicts}
    asc = d.get("agent_spawn_count")
    if asc is None:
        return ""
    total = asc.get("total_agents", 0)
    return ("<h2>Soak metrics</h2><ul>"
            f"<li>Total agent spawns: <b>{total:,}</b> (min {asc.get('min_total', 5000):,})</li>"
            "</ul>")


def _tiers_html(tiers: list[dict]) -> str:
    if not tiers:
        return ""
    body = "\n".join(
        f"<tr><td><code>{escape(str(t.get('tier','')))}</code></td>"
        f"<td>{escape(str(t.get('provider','')))}</td>"
        f"<td><code>{escape(str(t.get('model','')))}</code></td></tr>"
        for t in tiers)
    return ("<p><b>Model tiers:</b></p><table>"
            "<tr><th>tier</th><th>provider</th><th>model</th></tr>"
            f"{body}</table>")


def render_report(*, campaign: dict, rows: list[dict], verdicts: list[tuple[str, str, dict]],
                  gaps: list[dict], reproduce_cmd: str, out_path: Path) -> Path:
    auth_series = [r["hqs_ours"]["authoritative"] for r in rows
                   if (r.get("hqs_ours") or {}).get("authoritative") is not None]
    dash_series = [r["hqs_ours"]["dashboard"] for r in rows
                   if (r.get("hqs_ours") or {}).get("dashboard") is not None]
    chart = svgplot.line_chart({"authoritative": auth_series or [0.0],
                                 "dashboard": dash_series or [0.0]})

    diverg = []
    for r in rows:
        ours = r.get("hqs_ours") or {}
        arm = r.get("hqs_armature") or {}
        for k in ours:
            a, b = ours.get(k), arm.get(k)
            if a is not None and b is not None:
                diverg.append(abs(a - b))
    div_bars = svgplot.bar(["max_delta"], [max(diverg) if diverg else 0.0])

    fire_narr = []
    for r in rows:
        for lr in r.get("improve_log", []):
            if lr.get("needs_improvement"):
                fire_narr.append(
                    f"<p>Run <code>{escape(r['run_id'])}</code>: fired at "
                    f"hqs_before={escape(str(lr.get('hqs_before')))}, "
                    f"target={escape(str(lr.get('target_hqs')))}, "
                    f"applied={escape(str(lr.get('applied')))}. Recovery probe HQS="
                    f"{escape(str((r.get('recovery_hqs_ours') or {}).get('authoritative')))}.</p>")
    fire_html = "\n".join(fire_narr) or "<p>(no firings recorded)</p>"

    gaps_html = "\n".join(
        f"<li><b>{escape(g.get('want',''))}</b> — needed {escape(g.get('needed',''))} "
        f"[{escape(g.get('severity',''))}]</li>" for g in gaps) or "<li>(none)</li>"

    totals = campaign.get("totals", {})
    name = escape(campaign.get("name", ""))
    desc = campaign.get("description", "")
    desc_html = f"<p><em>{escape(desc)}</em></p>" if desc else ""
    purpose = campaign.get("purpose", "") or campaign.get("description", "")
    purpose_html = (f"<h2>What this test is</h2><p>{escape(purpose)}</p>"
                    if purpose else "")
    date_html = escape(campaign.get("date", ""))
    workflow_html = escape(campaign.get("workflow", ""))
    sha = escape(campaign.get("git_sha", ""))
    tiers_html = _tiers_html(campaign.get("tiers", []) or [])
    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<title>Campaign report — {name}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
h2{{border-bottom:1px solid #ddd;padding-bottom:.3em}} code{{background:#f5f5f5;padding:0 .2em}}
table{{border-collapse:collapse}} td,th{{border:1px solid #ddd;padding:.3em .6em}}
.meta{{color:#555;font-size:.9rem}}</style></head>
<body>
<h1>Campaign report — {name}</h1>
{desc_html}
{purpose_html}
<p class='meta'><b>Generated:</b> {date_html} &middot; <b>Workflow:</b> {workflow_html}
&middot; <b>git SHA:</b> <code>{sha}</code></p>
{tiers_html}
<h2>Campaign summary</h2>
<p>Totals: <code>{escape(str(totals))}</code></p>
<h2>HQS over runs</h2>
{chart}
<h2>Formula-divergence matrix</h2>
{div_bars}
<h2>Fire &rarr; recover narratives</h2>
{fire_html}
{_soak_metrics_html(verdicts)}
<h2>Verdict table</h2>
<table><tr><th>Hypothesis</th><th>Result</th><th>Detail</th></tr>
{_verdict_rows_html(verdicts)}</table>
<h2>Observability gaps</h2>
<ul>{gaps_html}</ul>
{narrative(verdicts, rows, campaign)}
<h2>Reproduce this</h2>
<pre><code>{escape(reproduce_cmd)}</code></pre>
</body></html>"""
    Path(out_path).write_text(html)
    return Path(out_path)


def _overall(verdicts: list[dict]) -> str:
    statuses = [v.get("result") for v in verdicts]
    if not statuses:
        return "none"
    if all(s == "PASS" for s in statuses):
        return "good"
    if any(s == "FAIL" for s in statuses):
        return "bad"
    return "inconclusive"


def _kind(name: str, path: str) -> str:
    n = name.lower()
    if "replay" in path.lower() or "replay" in n:
        return "Replay (determinism check)"
    if "soak" in n:
        return "Soak / reliability"
    if n.startswith("h1") or "hypothesis" in n:
        return "Hypothesis (HQS dynamics)"
    return "Campaign"


def build_index(out_dir: Path) -> Path:
    """Recursively scan out_dir for report.html files; render a self-contained
    out_dir/index.html that describes the test program and links to every
    report with its kind / what-it-tests / runs / verdict tally / date /
    overall verdict. Catches nested replay dirs and report-only dirs that
    lack a meta.json."""
    import json

    out_dir = Path(out_dir)
    idx_path = out_dir / "index.html"
    entries = []
    for rp in out_dir.rglob("report.html"):
        meta_p = rp.parent / "meta.json"
        m = json.loads(meta_p.read_text()) if meta_p.exists() else {}
        name = m.get("name") or rp.parent.name
        verdicts = m.get("verdict_statuses", [])
        overall = _overall(verdicts)
        counts = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0}
        for v in verdicts:
            r = v.get("result")
            if r in counts:
                counts[r] += 1
        tallies = [f"{counts[k]} {k}" for k in ("PASS", "FAIL", "INCONCLUSIVE") if counts[k]]
        tally = ", ".join(tallies) or "—"
        runs = (m.get("totals") or {}).get("runs")
        date = (m.get("date") or "").replace("T", " ").split("+")[0].split(".")[0] or "—"
        href = rp.relative_to(out_dir).as_posix()
        entries.append({
            "name": name, "kind": _kind(name, str(rp)), "purpose": m.get("purpose", ""),
            "runs": runs if runs is not None else "—", "tally": tally,
            "date": date, "overall": overall, "href": href, "mtime": rp.stat().st_mtime,
        })
    entries.sort(key=lambda e: e["mtime"], reverse=True)

    colors = {"good": "#2e7d32", "bad": "#c62828", "inconclusive": "#f57c00", "none": "#666"}
    labels = {"good": "PASS", "bad": "FAIL", "inconclusive": "INCONCLUSIVE", "none": "—"}
    if entries:
        body = "\n".join(
            f"<tr><td><a href='{escape(e['href'])}'>{escape(e['name'])}</a></td>"
            f"<td>{escape(e['kind'])}</td>"
            f"<td>{escape(e['purpose'].replace(chr(10), ' ')[:160])}</td>"
            f"<td style='text-align:center'>{e['runs']}</td>"
            f"<td>{escape(e['tally'])}</td>"
            f"<td>{escape(e['date'])}</td>"
            f"<td style='color:{colors[e['overall']]}'>{labels[e['overall']]}</td></tr>"
            for e in entries)
    else:
        body = "<tr><td colspan='7'>(no reports found)</td></tr>"

    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<title>Armature test reports</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}}
h1{{border-bottom:2px solid #333}}
table{{border-collapse:collapse;width:100%;margin-top:1rem}}
td,th{{border:1px solid #ddd;padding:.4em .6em;text-align:left;vertical-align:top}}
th{{background:#f5f5f5}}
a{{color:#1565c0;text-decoration:none}}a:hover{{text-decoration:underline}}
code,small{{color:#666}}
.lead{{line-height:1.5}}
</style></head>
<body><h1>Armature campaign test reports</h1>
<p class='lead'>This is the black-box test harness for the Armature workflow engine. It runs
real campaigns through the <code>armature</code> CLI (as a subprocess) and judges each run
against a set of <b>verdicts</b> — named, pass/fail/inconclusive checks that turn a raw run into
a claim about engine behavior. Each row below is one completed test; click the name to open its
full self-contained report (what it tests, the per-run data, the verdict table, and a narrative
of the results). Replay rows re-execute a recording at zero LLM cost to confirm a run's verdicts
reproduce — that is the determinism check. Overall: <b>PASS</b> = every verdict passed,
<b>FAIL</b> = at least one verdict failed, <b>INCONCLUSIVE</b> = the data could not settle a
verdict (an observability gap, not a quiet failure).</p>
<table>
<tr><th>Test</th><th>Kind</th><th>What it tests</th><th>Runs</th><th>Verdicts</th><th>Run date</th><th>Overall</th></tr>
{body}
</table>
<p><small>Generated by <code>python run.py --build-index</code>. Reports are self-contained
HTML; no external assets are required to view them.</small></p>
</body></html>"""
    idx_path.write_text(html)
    return idx_path