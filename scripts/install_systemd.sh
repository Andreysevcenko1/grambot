#!/usr/bin/env bash
# Installs (or updates) a systemd service that runs the GRAM/TON monitor bot
# 24/7 on a Linux server (Debian/Ubuntu, e.g. a Google Cloud / any VPS VM).
# Idempotent: re-run after `git pull` to refresh dependencies and restart.
#
#   ./scripts/install_systemd.sh              # install + start
#   BOT_TZ=Europe/Riga MEMORY_MAX=400M ./scripts/install_systemd.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-grambot}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
RUN_USER="${SUDO_USER:-$(id -un)}"
BOT_TZ="${BOT_TZ:-Europe/Riga}"
# Hard ceiling enforced by systemd; the bot's own watchdog restarts it earlier
# at MAX_MEMORY_MB from .env, so set MAX_MEMORY_MB below this value.
MEMORY_MAX="${MEMORY_MAX:-400M}"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"

if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "Error: $PROJECT_DIR/.env not found." >&2
    echo "Create it first: cp .env.example .env && nano .env  (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "Error: systemd not found. Use docker compose or 'python main.py' instead." >&2
    exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
    echo "Creating virtualenv in $PROJECT_DIR/.venv"
    python3 -m venv "$PROJECT_DIR/.venv" || {
        echo "Error: could not create a virtualenv. Install it with: sudo apt install -y python3-venv" >&2
        exit 1
    }
fi
echo "Installing dependencies"
"$PROJECT_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"

echo "Writing $UNIT_PATH"
sudo tee "$UNIT_PATH" >/dev/null <<UNIT
[Unit]
Description=GRAM/TON monitor bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
Environment=TZ=$BOT_TZ
Environment=PYTHONUNBUFFERED=1
# systemd handles restarts here, so the bot's built-in supervisor is disabled.
ExecStart=$PYTHON_BIN main.py --no-supervise
Restart=always
RestartSec=10
MemoryMax=$MEMORY_MAX
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
sudo systemctl restart "$SERVICE_NAME"
sleep 3
sudo systemctl --no-pager --lines=0 status "$SERVICE_NAME" || true

echo
echo "Service installed: $SERVICE_NAME (user $RUN_USER, TZ $BOT_TZ, memory limit $MEMORY_MAX)"
echo "Logs:    sudo journalctl -u $SERVICE_NAME -f"
echo "Status:  sudo systemctl status $SERVICE_NAME"
echo "Update:  cd $PROJECT_DIR && git pull && ./scripts/install_systemd.sh"
echo "Stop:    sudo systemctl stop $SERVICE_NAME    (disable autostart: sudo systemctl disable $SERVICE_NAME)"
