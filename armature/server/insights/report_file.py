"""The daily report as one self-contained HTML file: inline CSS, inline SVG, no script, every value escaped.

The folder is the config file's reports.dir. It must be a real directory this user owns that no one else can read, as
the master key file must be, and each file is written 0600 then moved into place.
"""
from __future__ import annotations

import html
import os
import secrets
import stat
from datetime import date
from pathlib import Path
from typing import Any

from armature.server.insights.report import pr_link

CSS = """
:root { color-scheme: light dark;
  --page: #f7f7f8; --surface: #ffffff; --ink: #111113; --muted: #66666d; --rule: #e4e4e7;
  --ok: #2f6b3a; --warn: #8a5a12; --bad: #9b2c2c; --accent: #0a64d8;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100; --s5: #e87ba4; --s6: #008300; --s7: #6250d6; --s8: #e34948;
  --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, sans-serif;
  --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace; }
@media (prefers-color-scheme: dark) { :root {
  --page: #0b0b0c; --surface: #141416; --ink: #ececee; --muted: #8e8e94; --rule: #26262a;
  --ok: #7fb98a; --warn: #d9a54a; --bad: #e08a86; --accent: #4c9bff;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500; --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink); font: 15px/1.55 var(--font); }
a { color: var(--accent); }
main { max-width: 960px; margin: 0 auto; padding: 32px 24px 64px; }
h1 { font-size: 26px; line-height: 1.2; margin: 0 0 4px; letter-spacing: -0.01em; }
h2 { font-size: 17px; margin: 0 0 4px; }
.sub, .note, .empty { color: var(--muted); }
.note { font-size: 13px; margin: 4px 0 12px; }
.empty { margin: 8px 0 0; }
section { background: var(--surface); border: 1px solid var(--rule); border-radius: 12px; padding: 18px 20px;
  margin-top: 16px; }
.tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 20px; }
.tile { background: var(--surface); border: 1px solid var(--rule); border-radius: 12px; padding: 14px 16px; }
.tile-label { color: var(--muted); font-size: 13px; }
.tile-value { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; }
.tile-delta { font-size: 12.5px; color: var(--muted); }
.learned { margin: 8px 0 0; padding-left: 20px; }
.learned li { margin: 4px 0; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--rule); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 12.5px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.barcell { display: block; width: 100%; min-width: 80px; }
.bar { display: block; height: 8px; border-radius: 4px; background: var(--s1); }
code, .mono { font-family: var(--mono); font-size: 0.86em; overflow-wrap: break-word; }
svg.days { width: 100%; height: auto; display: block; margin-top: 8px; }
svg.days .b { fill: var(--s1); } svg.days .b.prior { opacity: 0.45; }
svg.days text { fill: var(--muted); font: 11px var(--font); }
.bad { color: var(--bad); } .ok { color: var(--ok); }
footer { color: var(--muted); font-size: 12.5px; margin-top: 24px; }
@media (max-width: 640px) {
  main { padding: 20px 14px 40px; }
  .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  section { padding: 14px; }
}
"""


class Unsafe(Exception):
    """The reports folder is a symlink, not a directory, someone else's, or open to other users."""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _get(data: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return default
        data = data[key]
    return data


def money(value: Any) -> str:
    return f"${float(value):,.2f}" if isinstance(value, int | float) else "—"


def took(value: Any) -> str:
    if not isinstance(value, int | float):
        return "—"
    minutes, secs = divmod(round(value), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _num(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else f"{value:,.2f}" if isinstance(value, float) else "—"


def _delta(stat_: dict[str, Any], fmt) -> str:
    now, before, avg = stat_.get("value"), stat_.get("previous"), stat_.get("avg_7d")
    parts = []
    if before is not None:
        parts.append(f"day before {fmt(before)}")
    if avg is not None:
        parts.append(f"7-day average {fmt(avg)}")
    return " · ".join(parts) if now is not None else ""


def _tile(label: str, value: str, delta: str = "") -> str:
    return (f'<div class="tile"><div class="tile-label">{esc(label)}</div><div class="tile-value">{esc(value)}</div>'
            f'<div class="tile-delta">{esc(delta)}</div></div>')


def _section(title: str, body: str, note: str = "") -> str:
    lead = f'<p class="note">{esc(note)}</p>' if note else ""
    return f"<section><h2>{esc(title)}</h2>{lead}{body}</section>"


def _empty(text: str) -> str:
    return f'<p class="empty">{esc(text)}</p>'


def _table(headers: list[tuple[str, bool]], rows: list[list[str]]) -> str:
    head = "".join(f'<th class="num">{esc(h)}</th>' if num else f"<th>{esc(h)}</th>" for h, num in headers)
    body = "".join("<tr>" + "".join(f'<td class="num">{cell}</td>' if headers[i][1] else f"<td>{cell}</td>"
                                     for i, cell in enumerate(row)) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _bar(value: float, top: float) -> str:
    width = max(0.0, min(100.0, value / top * 100)) if top > 0 else 0.0
    return f'<span class="bar" style="width:{width:.1f}%"></span>'


def _day_bars(days: list[str], values: list[Any], fmt) -> str:
    """One bar per day, the report's day full strength, each with a hover title. No axis is needed: every bar is
    labeled with its value."""
    numbers = [float(v) if isinstance(v, int | float) else 0.0 for v in values]
    if not days or not any(numbers):
        return ""
    width, height, top_pad, bottom = 640, 150, 18, 22
    band = width / len(days)
    peak = max(numbers)
    parts = []
    for i, (day, value) in enumerate(zip(days, numbers, strict=False)):
        h = (height - top_pad - bottom) * value / peak if peak else 0
        x, y = band * i + band * 0.2, height - bottom - h
        prior = " prior" if i < len(days) - 1 else ""
        label = f"{esc(day)}: {esc(fmt(value))}"
        parts.append(f'<rect class="b{prior}" x="{x:.1f}" y="{y:.1f}" width="{band * 0.6:.1f}" height="{h:.1f}" '
                     f'rx="4"><title>{label}</title></rect>'
                     f'<text x="{x + band * 0.3:.1f}" y="{y - 4:.1f}" text-anchor="middle">{esc(fmt(value))}</text>'
                     f'<text x="{x + band * 0.3:.1f}" y="{height - 6}" text-anchor="middle">{esc(day[5:])}</text>')
    return (f'<svg class="days" viewBox="0 0 {width} {height}" role="img" aria-label="Per day, ending on the report '
            f'day">{"".join(parts)}</svg>')


def _pr(entry: dict[str, Any]) -> str:
    link = pr_link(entry.get("subject"))
    text = esc(entry.get("subject") or "unknown")
    return f'<a href="{esc(link["url"])}">{text}</a>' if link else text


def _headline(data: dict[str, Any], learned: str) -> str:
    head = _get(data, "headline", default={}) or {}
    runs, cost, failed = (head.get(k) or {} for k in ("runs", "cost_usd", "failed"))
    tiles = "".join((
        _tile("Runs", _num(runs.get("value")), _delta(runs, _num)),
        _tile("Cost", money(cost.get("value")), _delta(cost, money)),
        _tile("Failed or lost", _num(failed.get("value")), _delta(failed, _num)),
        _tile("Still open at build", _num(data.get("open_runs"))),
    ))
    series = head.get("series") or {}
    days = series.get("days") or []
    charts = _day_bars(days, series.get("runs") or [], lambda v: f"{v:,.0f}")
    spend = _day_bars(days, series.get("cost_usd") or [], lambda v: f"${v:,.0f}")
    trend = (_section("Runs per day", charts, "The last 8 days. The report's day is the full-strength bar.") if charts
             else "") + (_section("Cost per day", spend, "From each run's own cost.") if spend else "")
    rows = head.get("by_workflow") or []
    top = max([float(r.get("cost_usd") or 0) for r in rows] + [0.0])
    table = _table([("Workflow", False), ("Runs", True), ("Cost", True), ("", False), ("Median", True), ("p90", True)],
                   [[esc(r.get("workflow")), esc(_num(r.get("runs"))), esc(money(r.get("cost_usd"))),
                     f'<span class="barcell">{_bar(float(r.get("cost_usd") or 0), top)}</span>',
                     esc(took(r.get("p50"))), esc(took(r.get("p90")))] for r in rows]) if rows \
        else _empty("No runs on this day.")
    models = head.get("models") or []
    model_table = _table([("Model", False), ("Calls", True), ("Cost", True), ("Input tokens", True),
                          ("Output tokens", True)],
                         [[f'<span class="mono">{esc(m.get("model"))}</span>', esc(_num(m.get("calls"))),
                           esc(money(m.get("cost_usd"))), esc(_num(m.get("input_tokens"))),
                           esc(_num(m.get("output_tokens")))] for m in models]) if models \
        else _empty("No model calls on this day.")
    return (f'<div class="tiles">{tiles}</div>' + _section("What Armature learned", learned) + trend +
            _section("Workflows", table) +
            _section("Models", model_table, "A breakdown of model calls. The cost above comes from the runs."))


def _findings(data: dict[str, Any]) -> str:
    found = _get(data, "findings", default={}) or {}
    errors = found.get("errors") or []
    error_table = _table([("Error", False), ("Times", True), ("Runs", True), ("Workflows", False)],
                         [[f'<span class="mono">{esc(e.get("pattern"))}</span>', esc(_num(e.get("count"))),
                           esc(_num(e.get("runs"))), esc(", ".join(e.get("workflows") or []))] for e in errors]) \
        if errors else _empty("No step failed with an error.")
    failing = found.get("failing") or []
    failing_table = _table([("Workflow", False), ("Failed", True), ("Runs", True)],
                           [[esc(f.get("workflow")), esc(_num(f.get("failed"))), esc(_num(f.get("runs")))]
                            for f in failing]) if failing else _empty("No workflow had a failed run.")

    def runs_table(rows: list[dict[str, Any]], value: str) -> str:
        if not rows:
            return _empty("No finished runs.")
        return _table([("Workflow", False), ("Subject", False), ("Time" if value == "seconds" else "Cost", True)],
                      [[esc(r.get("workflow")), esc(r.get("subject") or ""),
                        esc(took(r.get("seconds")) if value == "seconds" else money(r.get("cost_usd")))]
                       for r in rows])
    dry = found.get("dry") or {}
    dry_rows = dry.get("by_workflow") or []
    dry_body = _table([("Workflow", False), ("Empty dry runs", True)],
                      [[esc(d.get("workflow")), esc(_num(d.get("runs")))] for d in dry_rows]) if dry_rows \
        else _empty("Every dry run produced something." if dry.get("runs") else "No dry runs.")
    return (_section("Errors", error_table, "Step errors grouped by their first line, with ids and numbers folded.") +
            _section("Failing workflows", failing_table) +
            _section("Slowest runs", runs_table(found.get("slowest") or [], "seconds")) +
            _section("Costliest runs", runs_table(found.get("costliest") or [], "cost")) +
            _section("Dry runs that produced nothing", dry_body, "No finding and no artifact.") +
            _churn(found.get("churn") or {}))


def _churn(review: dict[str, Any]) -> str:
    if not review.get("prs") and not review.get("findings"):
        return _section("Review churn", _empty("No review rounds were recorded on this day."))
    tiles = "".join((
        _tile("Pull requests reviewed", _num(review.get("prs")), f"{_num(review.get('multi_round'))} past round 1"),
        _tile("Most rounds", _num(review.get("max_rounds")), f"average {_num(review.get('avg_rounds'))}"),
        _tile("Late catches", _num(review.get("late")), f"of {_num(review.get('findings'))} findings"),
        _tile("Blocking", _num(review.get("blocking")), f"{_num(review.get('non_blocking'))} non-blocking, "
                                                        f"{_num(review.get('repeats'))} repeats"),
    ))
    top = review.get("top") or []
    table = _table([("Pull request", False), ("Title", False), ("Rounds", True), ("Runs", True)],
                   [[_pr(p), esc(p.get("title") or ""), esc(_num(p.get("rounds"))), esc(_num(p.get("runs")))]
                    for p in top]) if top else ""
    return _section("Review churn", f'<div class="tiles">{tiles}</div>{table}',
                    "A late catch is a finding first raised in round 2 or later. A repeat restates an earlier one.")


def _changes(data: dict[str, Any]) -> str:
    changes = _get(data, "changes", default={}) or {}
    deploy = changes.get("deploys") or {}
    lines = deploy.get("lines") or []
    live = deploy.get("live_sha")
    deploy_body = (f'<p>Live: <span class="mono">{esc(live)}</span></p>' if live else "") + (
        _table([("When", False), ("Event", False)],
               [[esc((line.get("at") or "")[11:16]), f'<span class="mono">{esc(line.get("line"))}</span>']
                for line in lines]) if lines else _empty("No deploys on this day."))
    versions = changes.get("versions") or []
    version_body = _table([("When", False), ("What", False), ("Version", True), ("Author", False), ("Note", False)],
                          [[esc((v.get("at") or "")[11:16]), esc(f"{v.get('kind')} {v.get('name')}"),
                            esc(_num(v.get("version"))), esc(v.get("author") or ""), esc(v.get("note") or "")]
                           for v in versions]) if versions else _empty("No workflow or document changed.")
    config = changes.get("config") or []
    config_body = _table([("When", False), ("Change", False), ("Team", False), ("By", False), ("Changed keys", False)],
                         [[esc((c.get("at") or "")[11:16]), esc(c.get("action")), esc(c.get("team") or "All"),
                           esc(c.get("actor") or ""), esc(", ".join(c.get("keys") or []) or "—")] for c in config]) \
        if config else _empty("No trigger, webhook rule, or setting changed.")
    prints = changes.get("fingerprint") or {}
    if prints.get("baseline"):
        print_body = _empty(f"First report, so its {_num(prints.get('files'))} files are the baseline.")
    else:
        moved = [(kind, path) for kind in ("added", "removed", "changed") for path in prints.get(kind) or []]
        print_body = _table([("File", False), ("Change", False)],
                            [[f'<span class="mono">{esc(path)}</span>', esc(kind)] for kind, path in moved]) \
            if moved else _empty("No configured file changed since the last report.")
    return (_section("Deploys", deploy_body) + _section("Workflow and document versions", version_body) +
            _section("Triggers, webhook rules, and settings", config_body, "Only the names of what changed.") +
            _section("Configured files", print_body))


def render(data: dict[str, Any]) -> str:
    day = esc(data.get("day"))
    zone = esc(data.get("zone"))
    try:
        when = date.fromisoformat(str(data.get("day")))
        long_day = f"{when:%A, %B} {when.day}, {when.year}"
    except ValueError:
        long_day = str(data.get("day"))
    learned = [item.get("text") for item in data.get("learned") or [] if isinstance(item, dict)]
    learned_body = ('<ul class="learned">' + "".join(f"<li>{esc(text)}</li>" for text in learned) + "</ul>") \
        if learned else _empty("Nothing stood out.")
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Armature daily report {day}</title><style>{CSS}</style></head><body><main>'
            f'<h1>What Armature learned on {esc(long_day)}</h1>'
            f'<p class="sub">Every workflow, the day in {zone}. Cost always shows here.</p>'
            f'{_headline(data, learned_body)}{_findings(data)}{_changes(data)}'
            f'<footer>Armature daily report for {day}. {esc(_num(data.get("open_runs")))} runs from the day were '
            f'still open when it was built.</footer></main></body></html>\n')


def folder(directory: str) -> Path:
    """The reports folder, made 0700 when missing, refused when it isn't safe."""
    path = Path(directory).expanduser()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True)
        info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Unsafe(f"reports folder {path} is not a directory")
    if info.st_uid != os.getuid():
        raise Unsafe(f"reports folder {path} is not owned by this user")
    if info.st_mode & 0o077:
        raise Unsafe(f"reports folder {path} has mode {stat.S_IMODE(info.st_mode):04o}; chmod 700 it")
    return path


def write(directory: str, day: date, text: str) -> Path:
    """Write the day's file through a 0600 temporary file and a rename, so a reader never sees half of it."""
    where = folder(directory)
    target = where / f"{date.fromisoformat(day.isoformat()).isoformat()}.html"
    tmp = where / f".{target.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return target
