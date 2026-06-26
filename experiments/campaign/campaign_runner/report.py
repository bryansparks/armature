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


def render_report(*, campaign: dict, rows: list[dict], verdicts: list[tuple[str, str, dict]],
                  gaps: list[dict], reproduce_cmd: str, out_path: Path) -> Path:
    auth_series = [r["hqs_ours"]["authoritative"] for r in rows
                   if r["hqs_ours"].get("authoritative") is not None]
    dash_series = [r["hqs_ours"]["dashboard"] for r in rows
                   if r["hqs_ours"].get("dashboard") is not None]
    chart = svgplot.line_chart({"authoritative": auth_series or [0.0],
                                 "dashboard": dash_series or [0.0]})

    diverg = []
    for r in rows:
        for k in r["hqs_ours"]:
            a, b = r["hqs_ours"].get(k), r["hqs_armature"].get(k)
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
    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<title>Campaign report — {escape(campaign.get('name',''))}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}}
h2{{border-bottom:1px solid #ddd;padding-bottom:.3em}} code{{background:#f5f5f5;padding:0 .2em}}
table{{border-collapse:collapse}} td,th{{border:1px solid #ddd;padding:.3em .6em}}</style></head>
<body>
<h1>Campaign report — {escape(campaign.get('name',''))}</h1>
<h2>Campaign summary</h2>
<p>git SHA: <code>{escape(campaign.get('git_sha',''))}</code></p>
<p>Totals: <code>{escape(str(totals))}</code></p>
<h2>HQS over runs</h2>
{chart}
<h2>Formula-divergence matrix</h2>
{div_bars}
<h2>Fire &rarr; recover narratives</h2>
{fire_html}
<h2>Verdict table</h2>
<table><tr><th>Hypothesis</th><th>Result</th><th>Detail</th></tr>
{_verdict_rows_html(verdicts)}</table>
<h2>Observability gaps</h2>
<ul>{gaps_html}</ul>
<h2>Reproduce this</h2>
<pre><code>{escape(reproduce_cmd)}</code></pre>
</body></html>"""
    Path(out_path).write_text(html)
    return Path(out_path)