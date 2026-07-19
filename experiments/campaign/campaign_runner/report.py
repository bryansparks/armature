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


def _agents_html(campaign: dict) -> str:
    """Per-workflow agent-spawn tally: total LLM-stage invocations (incl. fan-out
    partitions + retries) per workflow, with a grand total. The count matches
    Armature's own llm_calls per run. Omitted when the campaign carried no
    per-workflow breakdown (e.g. older recordings)."""
    apw = campaign.get("agents_per_workflow")
    if not apw:
        return ""
    grand = int(campaign.get("grand_total_agents", 0) or 0)
    body = "\n".join(
        f"<tr><td><code>{escape(wf)}</code></td>"
        f"<td style='text-align:right'>{d['runs']}</td>"
        f"<td style='text-align:right'>{int(d['agents']):,}</td></tr>"
        for wf, d in apw.items())
    return ("<h2>Agents run per workflow</h2>"
            "<p>Total LLM-stage invocations (incl. fan-out partitions + retries) "
            "per workflow — our agent spawn count.</p>"
            "<table><tr><th>workflow</th><th>runs</th><th>agents run</th></tr>"
            f"{body}"
            f"<tr><td><b>grand total</b></td><td></td>"
            f"<td style='text-align:right'><b>{grand:,}</b></td></tr></table>")


def _provider_health_html(campaign: dict, verdicts: list[tuple[str, str, dict]]) -> str:
    """Red banner at the very top when provider_health != PASS or the campaign
    was aborted — the fix for account exhaustion being 'completely hidden'.
    Omitted entirely on PASS so clean reports stay clean."""
    aborted = campaign.get("aborted")
    ph = next((v for v in verdicts if v[0] == "provider_health"), None)
    if (ph is None or ph[1] == "PASS") and not aborted:
        return ""
    detail = (ph[2] if ph else {}) or {}
    reason = campaign.get("abort_reason") or detail.get("abort_reason") or "provider account exhausted"
    models = ", ".join(detail.get("models") or []) or "—"
    buckets = ", ".join(f"{k}×{v}" for k, v in (detail.get("buckets") or {}).items()) or "—"
    run_ids = ", ".join(str(x) for x in (detail.get("run_ids") or [])) or "—"
    K = detail.get("K", 3)
    aborted_line = "campaign aborted" if aborted else "account-scoped failures present"
    return (f"<div style='border:2px solid #c62828;background:#ffebee;padding:1em;margin:1em 0'>"
            f"<b>⚠ Provider account exhausted — {aborted_line} after {escape(str(K))} consecutive account-scoped runs.</b><br>"
            f"Provider/model: <code>{escape(models)}</code> &nbsp; Bucket: <code>{escape(buckets)}</code><br>"
            f"Failed runs: <code>{escape(run_ids)}</code><br>"
            f"{escape(reason)} — add credits / fix the key at your provider and resume."
            f"</div>")


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


def _fmt(x, digits=4):
    return f"{x:.{digits}f}" if isinstance(x, (int, float)) else "—"


def _chip(ok: bool) -> str:
    color = "#2e7d32" if ok else "#c62828"
    label = "PASS" if ok else "FAIL"
    return f"<span style='color:{color};font-weight:bold'>{label}</span>"


def _three_arm_html(verdicts: list[tuple[str, str, dict]], th: dict) -> str:
    """3-arm cold/warm/nav comparison for the memory_carry_forward_helps verdict.
    Renders bar charts (per-arm coverage, latency, input tokens), a pairwise
    diff table (warm-cold, nav-cold, nav-warm) with bootstrap CIs + per-gate
    PASS/FAIL chips, and n_excluded per arm. Returns '' when the H4 verdict is
    2-arm (no nav fields) or absent — the existing verdict table covers those."""
    h4 = next((v for v in verdicts if v[0] == "memory_carry_forward_helps"), None)
    if h4 is None:
        return ""
    _name, result, d = h4
    if "nav_minus_cold_mean" not in d:
        return ""  # 2-arm fallback — nothing 3-arm to show

    arms = ["cold", "warm", "nav"]
    quorum = [d.get(f"mean_quorum_{a}") for a in arms]
    latency = [d.get(f"mean_latency_{a}") for a in arms]
    tokens = [d.get(f"mean_input_tokens_{a}") for a in arms]

    def _bars(vals, title):
        clean = [(v if isinstance(v, (int, float)) else 0.0) for v in vals]
        return f"<h3>{title}</h3>" + svgplot.bar(arms, clean)

    quorum_svg = _bars(quorum, "Mean judge quorum (coverage)")
    latency_svg = _bars(latency, "Mean researcher latency (ms)")
    tokens_svg = _bars(tokens, "Mean researcher input tokens")

    pairs = [
        ("warm&minus;cold", d.get("warm_minus_cold_mean"), d.get("warm_minus_cold_ci_low"),
         th.get("warm_minus_cold_mean_ge", 0.05),
         (d.get("warm_minus_cold_mean", 0) >= th.get("warm_minus_cold_mean_ge", 0.05)
          and d.get("warm_minus_cold_ci_low", 0) >= th.get("bootstrap_ci_lower_ge", 0.0))),
        ("nav&minus;cold", d.get("nav_minus_cold_mean"), d.get("nav_minus_cold_ci_low"),
         th.get("nav_minus_cold_mean_ge", 0.05),
         (d.get("nav_minus_cold_mean", 0) >= th.get("nav_minus_cold_mean_ge", 0.05))),
        ("nav&minus;warm", d.get("nav_minus_warm_mean"), d.get("nav_minus_warm_ci_low"),
         th.get("nav_minus_warm_mean_ge", 0.0),
         (d.get("nav_minus_warm_mean", 0) >= th.get("nav_minus_warm_mean_ge", 0.0))),
    ]
    diff_rows = "\n".join(
        f"<tr><td><b>{label}</b></td>"
        f"<td style='text-align:right'>{_fmt(mean)}</td>"
        f"<td style='text-align:right'>{_fmt(lo)}</td>"
        f"<td style='text-align:right'>{_fmt(thr)}</td>"
        f"<td style='text-align:center'>{_chip(ok)}</td></tr>"
        for label, mean, lo, thr, ok in pairs)

    nexc = d.get("n_excluded") or {}
    nexc_line = ", ".join(f"{a}: {nexc.get(a, 0)}" for a in arms)
    ns = ", ".join(f"{a}={d.get(f'n_{a}', 0)}" for a in arms)

    thesis = ("<p><em>Thesis under test:</em> active navigation (nav) recovers passive "
              "injection's (warm) coverage benefit WITHOUT its ~5&times; researcher-latency "
              "tax. <b>Headline gate: nav&minus;warm &ge; 0</b> (navigation matches or beats "
              "warm on coverage). The latency + token charts are the efficiency signal — "
              "nav should match warm's coverage at substantially lower latency/tokens.</p>")

    return (f"<h2>3-arm comparison (cold vs warm vs nav)</h2>"
            f"{thesis}"
            f"<p class='meta'>Runs: {ns} &middot; model_failed excluded: {nexc_line} "
            f"&middot; overall H4: <b>{result}</b></p>"
            f"<table><tr><th>3-arm bar charts</th></tr>"
            f"<tr><td>{quorum_svg}</td></tr>"
            f"<tr><td>{latency_svg}</td></tr>"
            f"<tr><td>{tokens_svg}</td></tr></table>"
            f"<h3>Pairwise coverage diffs (bootstrap CI)</h3>"
            f"<table><tr><th>comparison</th><th>mean</th><th>CI low</th>"
            f"<th>threshold</th><th>gate</th></tr>{diff_rows}</table>")


def render_report(*, campaign: dict, rows: list[dict], verdicts: list[tuple[str, str, dict]],
                  gaps: list[dict], reproduce_cmd: str, out_path: Path,
                  verdict_thresholds: dict | None = None) -> Path:
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
    three_arm = _three_arm_html(verdicts, verdict_thresholds or {})
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
{_provider_health_html(campaign, verdicts)}
{tiers_html}
<h2>Campaign summary</h2>
<p>Totals: <code>{escape(str(totals))}</code></p>
{_agents_html(campaign)}
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
{three_arm}
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


def build_3arm_preview(*, out_path: Path) -> Path:
    """Generate a PREVIEW report.html with synthetic 3-arm data so the report
    format is visible before a live campaign run (OpenRouter credits required
    for the real run). The data is illustrative, not measured. Watermarked."""
    campaign = {
        "name": "cold-vs-warm (PREVIEW — synthetic data)",
        "description": "PREVIEW of the 3-arm report format. Data is synthetic; "
                       "the live campaign run is deferred to OpenRouter credits.",
        "purpose": "Preview the 3-arm cold/warm/nav comparison report section.",
        "date": "2026-07-19 (PREVIEW)", "workflow": "specs/campaign_research_brief_memory_nav.yml",
        "git_sha": "preview", "totals": {"runs": 30},
    }
    rows = [{"run_id": f"r{i}", "hqs_ours": {"authoritative": 0.7, "dashboard": 0.7},
             "hqs_armature": {"authoritative": 0.7}} for i in range(3)]
    detail = {
        "signal": "quorum", "n_cold": 10, "n_warm": 10, "n_nav": 10,
        "n_excluded": {"cold": 0, "warm": 1, "nav": 0},
        "mean_quorum_cold": 0.50, "mean_quorum_warm": 0.86, "mean_quorum_nav": 0.85,
        "mean_latency_cold": 1200.0, "mean_latency_warm": 6000.0, "mean_latency_nav": 1400.0,
        "mean_input_tokens_cold": 200.0, "mean_input_tokens_warm": 1000.0, "mean_input_tokens_nav": 250.0,
        "warm_minus_cold_mean": 0.36, "warm_minus_cold_ci_low": 0.30,
        "nav_minus_cold_mean": 0.35, "nav_minus_cold_ci_low": 0.28,
        "nav_minus_warm_mean": -0.01, "nav_minus_warm_ci_low": -0.05,
    }
    verdicts = [("memory_carry_forward_helps", "FAIL", detail),
                ("provider_health", "PASS", {"abort_reason": None})]
    th = {"warm_minus_cold_mean_ge": 0.05, "bootstrap_ci_lower_ge": 0.0,
          "nav_minus_cold_mean_ge": 0.05, "nav_minus_warm_mean_ge": 0.0}
    banner = ("<div style='border:2px solid #f57c00;background:#fff3e0;padding:1em;margin:1em 0'>"
              "<b>⚠ PREVIEW — synthetic data.</b> This is what the 3-arm report will look like "
              "after the live campaign run (deferred to OpenRouter credits). Numbers are "
              "illustrative; the real run will populate them from measured rows.</div>")
    out = Path(out_path)
    render_report(campaign=campaign, rows=rows, verdicts=verdicts, gaps=[],
                  reproduce_cmd="python -m armature loop experiments/campaign/plans/cold_vs_warm.yml  # (live run deferred to credits)",
                  out_path=out, verdict_thresholds=th)
    text = out.read_text()
    out.write_text(text.replace("<h1>Campaign report", banner + "<h1>Campaign report", 1))
    return out


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
        agents = m.get("grand_total_agents")
        date = (m.get("date") or "").replace("T", " ").split("+")[0].split(".")[0] or "—"
        href = rp.relative_to(out_dir).as_posix()
        entries.append({
            "name": name, "kind": _kind(name, str(rp)), "purpose": m.get("purpose", ""),
            "runs": runs if runs is not None else "—", "tally": tally,
            "agents": f"{agents:,}" if isinstance(agents, int) else "—",
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
            f"<td style='text-align:center'>{escape(e['agents'])}</td>"
            f"<td>{escape(e['tally'])}</td>"
            f"<td>{escape(e['date'])}</td>"
            f"<td style='color:{colors[e['overall']]}'>{labels[e['overall']]}</td></tr>"
            for e in entries)
    else:
        body = "<tr><td colspan='8'>(no reports found)</td></tr>"

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
<tr><th>Test</th><th>Kind</th><th>What it tests</th><th>Runs</th><th>Agents</th><th>Verdicts</th><th>Run date</th><th>Overall</th></tr>
{body}
</table>
<p><small>Generated by <code>python run.py --build-index</code>. Reports are self-contained
HTML; no external assets are required to view them.</small></p>
</body></html>"""
    idx_path.write_text(html)
    return idx_path
