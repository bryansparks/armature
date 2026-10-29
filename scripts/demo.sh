#!/usr/bin/env bash
# Start a local Armature demo with replayed sample runs on http://127.0.0.1:8197/.
#
#   scripts/demo.sh           seed the data dir if it's empty, then serve in the foreground
#   scripts/demo.sh --reset   wipe the data dir, then seed and serve fresh
#
# ARMATURE_DEMO_DATA overrides the data dir. ARMATURE_DEMO_PYTHON picks the Python that has the server extra;
# otherwise the checkout's .venv, then `uv run --extra server`. This checkout's code always runs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${ARMATURE_DEMO_DATA:-/private/tmp/claude-501/-Users-stephan-projects-familysearch-armature-prod/cb08a17e-eccd-43c3-99cd-4c72161e9073/scratchpad/demo-data}"

if [[ "${1:-}" == "--reset" ]]; then
  rm -rf "$DATA"
  echo "Wiped $DATA"
elif [[ -n "${1:-}" ]]; then
  echo "usage: scripts/demo.sh [--reset]" >&2
  exit 2
fi

if [[ -n "${ARMATURE_DEMO_PYTHON:-}" ]]; then
  PY=("$ARMATURE_DEMO_PYTHON")
elif [[ -x "$ROOT/.venv/bin/python" ]] && "$ROOT/.venv/bin/python" -c "import fastapi" 2>/dev/null; then
  PY=("$ROOT/.venv/bin/python")
else
  PY=(uv run --project "$ROOT" --extra server python)
fi

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONWARNINGS=ignore
# The demo never calls an LLM. Unset keys so nothing a click starts can reach one.
unset ANTHROPIC_API_KEY OPENAI_API_KEY OPENROUTER_API_KEY

"${PY[@]}" "$ROOT/scripts/demo.py" seed "$DATA"
exec "${PY[@]}" "$ROOT/scripts/demo.py" serve "$DATA"
