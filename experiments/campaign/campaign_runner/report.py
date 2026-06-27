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


def build_index(out_dir: Path) -> Path:
    """Scan out/*/meta.json; render a self-contained out/index.html linking
    every campaign/soak report with name / purpose / date / overall verdict."""
    import json
    rows = []
    for meta_p in sorted(Path(out_dir).glob("*/meta.json")):
        try:
            m = json.loads(meta_p.read_text())
        except Exception:
            continue
        statuses = [v.get("result") for v in m.get("verdict_statuses", [])]
        if statuses and all(s == "PASS" for s in statuses):
            overall = "good"
        elif any(s == "FAIL" for s in statuses):
            overall = "bad"
        else:
            overall = "inconclusive"
        color = {"good": "#2e7d32", "bad": "#c62828", "inconclusive": "#f57c00"}[overall]
        rows.append(
            f"<tr><td><a href='{escape(meta_p.parent.name)}/report.html'>{escape(m.get('name', meta_p.parent.name))}</a></td>"
            f"<td>{escape(m.get('purpose', ''))}</td>"
            f"<td>{escape(m.get('date', ''))}</td>"
            f"<td style='color:{color}'>{overall}</td></tr>")
    body = "\n".join(rows) or "<tr><td colspan='4'>(no reports found)</td></tr>"
    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<title>Armature test reports</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ddd;padding:.4em .6em;text-align:left}}
h1{{border-bottom:2px solid #333}}</style></head>
<body><h1>Armature test reports</h1>
<p>Each row is one test run. Click a test name to open its full report (what it tests, the data,
verdicts, and a narrative of the results). Overall: <b>good</b> = all verdicts PASS,
<b>bad</b> = any FAIL, <b>inconclusive</b> = unsettled.</p>
<table><tr><th>Test</th><th>What it tests</th><th>Run</th><th>Overall</th></tr>
{body}</table></body></html>"""
    out = Path(out_dir) / "index.html"
    out.write_text(html)
    return out