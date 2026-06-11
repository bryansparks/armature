#!/usr/bin/env bash
# Regenerate all Armature HTML docs from their Markdown sources.
# Run from the project root: bash docs/regen-guide.sh
# Optionally pass a filename to regenerate just one:
#   bash docs/regen-guide.sh USER-GUIDE
#   bash docs/regen-guide.sh BUILD_FIRST_WORKFLOW
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

pandoc docs/USER-GUIDE.md \
  --standalone \
  --toc \
  --toc-depth=2 \
  --highlight-style=breezedark \
  --metadata title="Armature User Guide" \
  --include-before-body=docs/header.html \
  --embed-resources \
  --variable "include-before=<style>
html{background-color:#0d1117 !important;color:#c9d1d9 !important}
:root{--bg:#0d1117;--surface:#161b22;--border:#21262d;--text:#c9d1d9;--muted:#6e7681;--accent:#4ade80;--accent2:#60a5fa;--accent3:#c084fc;--code-bg:#0a0e14}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:15px;line-height:1.75;background:var(--bg);color:var(--text);max-width:920px;margin:0 auto;padding:2rem 2rem 5rem}
h1{font-size:1.8rem;font-weight:700;color:var(--accent);border-bottom:1px solid var(--border);padding-bottom:.5rem;margin:2.5rem 0 1rem}
h2{font-size:1.3rem;font-weight:600;color:var(--accent2);border-left:3px solid var(--accent2);padding-left:.75rem;margin:2.5rem 0 .9rem}
h3{font-size:1.05rem;font-weight:600;color:#e6edf3;margin:2rem 0 .6rem}
h4{font-size:.8rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:1.5rem 0 .5rem}
p{margin:.7rem 0}
a{color:var(--accent2);text-decoration:none}
a:hover{text-decoration:underline}
code{font-family:'JetBrains Mono','Fira Code','Cascadia Code',Menlo,Monaco,monospace;font-size:.84em;background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:.12em .38em;color:#e6edf3}
pre,pre.sourceCode,div.sourceCode{background:var(--code-bg) !important;border:1px solid var(--border) !important;border-radius:8px;padding:1.2rem 1.4rem;overflow-x:auto;margin:1rem 0}
pre code,pre.sourceCode code,div.sourceCode code{background:none !important;border:none;padding:0;font-size:.84em;line-height:1.65;color:#cdd6f4}
.sourceCode{background:var(--code-bg) !important}
code span{background:none !important}
table{width:100%;border-collapse:collapse;margin:1rem 0;font-size:.88em}
th{background:var(--surface);color:var(--accent2);font-weight:600;text-align:left;padding:.55rem .85rem;border:1px solid var(--border);font-size:.8rem;text-transform:uppercase;letter-spacing:.05em}
td{padding:.5rem .85rem;border:1px solid var(--border);vertical-align:top;color:var(--text)}
tr:nth-child(even){background:rgba(255,255,255,.025)}
ul,ol{margin:.5rem 0 .5rem 1.6rem}
li{margin:.3rem 0}
blockquote{border-left:3px solid var(--accent);padding:.5rem 1rem;color:var(--muted);margin:1rem 0;background:rgba(74,222,128,.04);border-radius:0 6px 6px 0}
hr{border:none;border-top:1px solid var(--border);margin:2.5rem 0}
nav#TOC{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:1.2rem 1.5rem;margin-bottom:2.5rem}
nav#TOC>h2{font-size:.85rem;color:var(--muted);border:none;padding:0;margin:0 0 .75rem;text-transform:uppercase;letter-spacing:.08em}
nav#TOC ul{margin:0;list-style:none;padding:0}
nav#TOC ul ul{margin-left:1.1rem;border-left:1px solid var(--border);padding-left:.75rem;margin-top:.2rem}
nav#TOC a{color:var(--text);font-size:.87em;line-height:1.8}
nav#TOC a:hover{color:var(--accent2)}
nav#TOC>ul>li{margin:.2rem 0}
strong{color:#e6edf3}
@media print{html,body{background:white !important;color:black !important}}
</style>" \
  -o USER-GUIDE.html

# Fix pandoc's hardcoded light-mode html{} defaults
python3 - <<'PY'
path = 'USER-GUIDE.html'
html = open(path).read()
html = html.replace(
    'color: #1a1a1a;\nbackground-color: #fdfdfd;',
    'color: #c9d1d9;\nbackground-color: #0d1117;'
)
html = html.replace('max-width: 36em;', 'max-width: none;')
open(path, 'w').write(html)
PY

echo "USER-GUIDE.html regenerated ($(wc -c < USER-GUIDE.html | tr -d ' ') bytes)"

pandoc docs/BUILD_FIRST_WORKFLOW.md \
  --standalone \
  --toc \
  --toc-depth=2 \
  --highlight-style=breezedark \
  --metadata title="Build Your First Armature Workflow" \
  --include-before-body=docs/header.html \
  --embed-resources \
  --variable "include-before=<style>
html{background-color:#0d1117 !important;color:#c9d1d9 !important}
:root{--bg:#0d1117;--surface:#161b22;--border:#21262d;--text:#c9d1d9;--muted:#6e7681;--accent:#4ade80;--accent2:#60a5fa;--accent3:#c084fc;--code-bg:#0a0e14}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:15px;line-height:1.75;background:var(--bg);color:var(--text);max-width:920px;margin:0 auto;padding:2rem 2rem 5rem}
h1{font-size:1.8rem;font-weight:700;color:var(--accent);border-bottom:1px solid var(--border);padding-bottom:.5rem;margin:2.5rem 0 1rem}
h2{font-size:1.3rem;font-weight:600;color:var(--accent2);border-left:3px solid var(--accent2);padding-left:.75rem;margin:2.5rem 0 .9rem}
h3{font-size:1.05rem;font-weight:600;color:#e6edf3;margin:2rem 0 .6rem}
h4{font-size:.8rem;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:1.5rem 0 .5rem}
p{margin:.7rem 0}
a{color:var(--accent2);text-decoration:none}
a:hover{text-decoration:underline}
code{font-family:'JetBrains Mono','Fira Code','Cascadia Code',Menlo,Monaco,monospace;font-size:.84em;background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:.12em .38em;color:#e6edf3}
pre,pre.sourceCode,div.sourceCode{background:var(--code-bg) !important;border:1px solid var(--border) !important;border-radius:8px;padding:1.2rem 1.4rem;overflow-x:auto;margin:1rem 0}
pre code,pre.sourceCode code,div.sourceCode code{background:none !important;border:none;padding:0;font-size:.84em;line-height:1.65;color:#cdd6f4}
.sourceCode{background:var(--code-bg) !important}
code span{background:none !important}
table{width:100%;border-collapse:collapse;margin:1rem 0;font-size:.88em}
th{background:var(--surface);color:var(--accent2);font-weight:600;text-align:left;padding:.55rem .85rem;border:1px solid var(--border);font-size:.8rem;text-transform:uppercase;letter-spacing:.05em}
td{padding:.5rem .85rem;border:1px solid var(--border);vertical-align:top;color:var(--text)}
tr:nth-child(even){background:rgba(255,255,255,.025)}
ul,ol{margin:.5rem 0 .5rem 1.6rem}
li{margin:.3rem 0}
blockquote{border-left:3px solid var(--accent);padding:.5rem 1rem;color:var(--muted);margin:1rem 0;background:rgba(74,222,128,.04);border-radius:0 6px 6px 0}
hr{border:none;border-top:1px solid var(--border);margin:2.5rem 0}
nav#TOC{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:1.2rem 1.5rem;margin-bottom:2.5rem}
nav#TOC>h2{font-size:.85rem;color:var(--muted);border:none;padding:0;margin:0 0 .75rem;text-transform:uppercase;letter-spacing:.08em}
nav#TOC ul{margin:0;list-style:none;padding:0}
nav#TOC ul ul{margin-left:1.1rem;border-left:1px solid var(--border);padding-left:.75rem;margin-top:.2rem}
nav#TOC a{color:var(--text);font-size:.87em;line-height:1.8}
nav#TOC a:hover{color:var(--accent2)}
nav#TOC>ul>li{margin:.2rem 0}
strong{color:#e6edf3}
@media print{html,body{background:white !important;color:black !important}}
</style>" \
  -o BUILD_FIRST_WORKFLOW.html

python3 - <<'PY'
path = 'BUILD_FIRST_WORKFLOW.html'
html = open(path).read()
html = html.replace(
    'color: #1a1a1a;\nbackground-color: #fdfdfd;',
    'color: #c9d1d9;\nbackground-color: #0d1117;'
)
html = html.replace('max-width: 36em;', 'max-width: none;')
open(path, 'w').write(html)
PY

echo "BUILD_FIRST_WORKFLOW.html regenerated ($(wc -c < BUILD_FIRST_WORKFLOW.html | tr -d ' ') bytes)"
