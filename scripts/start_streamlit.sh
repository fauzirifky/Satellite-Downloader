#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -x "$ROOT_DIR/.venv/bin/streamlit" ]]; then
  exec "$ROOT_DIR/.venv/bin/streamlit" run "$ROOT_DIR/app.py"
fi

exec streamlit run "$ROOT_DIR/app.py"
