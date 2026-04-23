#!/bin/bash
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-/opt/data}"
BOOTSTRAP_MARKER="$HERMES_HOME/.render-bootstrap-v1"
SOURCE_CONFIG="deploy/render-config.staging.yaml"
SOURCE_SOUL="deploy/render-SOUL.md"

mkdir -p "$HERMES_HOME"

if [ ! -f "$BOOTSTRAP_MARKER" ]; then
  echo "[render-start] First boot detected"
fi

echo "[render-start] Syncing managed config and SOUL"
cp "$SOURCE_CONFIG" "$HERMES_HOME/config.yaml"
cp "$SOURCE_SOUL" "$HERMES_HOME/SOUL.md"
touch "$BOOTSTRAP_MARKER"

PYTHON_BIN="${VIRTUAL_ENV:-/opt/hermes/.venv}/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

exec "$PYTHON_BIN" scripts/render_gateway_proxy.py
