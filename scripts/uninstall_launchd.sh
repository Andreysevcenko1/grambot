#!/usr/bin/env bash
# Stops and removes the GRAM/TON monitor bot's launchd user agent.
set -euo pipefail

PLIST_LABEL="com.grambot.monitor"
PLIST_DEST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

if [ -f "$PLIST_DEST" ]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
    rm -f "$PLIST_DEST"
    echo "Uninstalled launchd agent: $PLIST_LABEL"
else
    echo "No launchd agent installed at $PLIST_DEST"
fi
