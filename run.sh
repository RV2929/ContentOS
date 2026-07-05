#!/bin/zsh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load API key
if [ -f "$SCRIPT_DIR/.env" ]; then
  export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

"$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/contentos.py" "$@"
