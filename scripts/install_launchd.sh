#!/usr/bin/env bash
# Installs a launchd user agent that runs the GRAM/TON monitor bot
# persistently in the background on macOS (survives terminal close,
# restarts on crash, and can auto-start on login).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
MAIN_PY="$PROJECT_DIR/main.py"
PLIST_LABEL="com.grambot.monitor"
PLIST_DEST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Error: $PYTHON_BIN not found. Create the venv first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "Warning: $PROJECT_DIR/.env not found. Copy .env.example and fill in your Telegram credentials first." >&2
fi

mkdir -p "$HOME/Library/LaunchAgents"

sed \
    -e "s#__PYTHON_BIN__#$PYTHON_BIN#g" \
    -e "s#__MAIN_PY__#$MAIN_PY#g" \
    -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" \
    "$SCRIPT_DIR/com.grambot.monitor.plist.template" > "$PLIST_DEST"

launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load -w "$PLIST_DEST"

echo "Installed and started launchd agent: $PLIST_LABEL"
echo "Logs: $PROJECT_DIR/grambot.log"
echo "Stop with:    launchctl unload $PLIST_DEST"
echo "Restart with: launchctl unload $PLIST_DEST && launchctl load -w $PLIST_DEST"
echo "Uninstall with: ./scripts/uninstall_launchd.sh"
