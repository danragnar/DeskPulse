#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="claude-usage-daemon"
SERVICE_FILE="$SCRIPT_DIR/daemon/$SERVICE_NAME.service"
USER_SERVICE_DIR="$HOME/.config/systemd/user"
VENV_DIR="$SCRIPT_DIR/daemon/.venv"
DAEMON_PY="$SCRIPT_DIR/daemon/claude_usage_daemon.py"
CONFIG_FILE="$SCRIPT_DIR/daemon/config.toml"
PYTHON_BIN="$VENV_DIR/bin/python"

echo "=== Claude Usage Tracker - Install ==="
echo ""

# Check dependencies
echo "[1/5] Checking dependencies..."
for cmd in python3 systemctl; do
    command -v "$cmd" >/dev/null || { echo "Error: $cmd is required but not installed"; exit 1; }
done
echo "  All dependencies found"
echo ""

# Install Python daemon dependencies
echo "[2/5] Installing Python daemon environment..."
if [ ! -d "$VENV_DIR" ]; then
    if ! python3 -m venv "$VENV_DIR"; then
        echo "Error: python3 venv support is required (install python3-venv or equivalent)"
        exit 1
    fi
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet "bleak>=0.22" "httpx>=0.27"
echo "  OK ($PYTHON_BIN)"
echo ""

# Ensure daemon config exists
echo "[3/5] Ensuring daemon config..."
if [ ! -f "$CONFIG_FILE" ]; then
    printf 'provider = "both"\n' > "$CONFIG_FILE"
    echo "  Created: $CONFIG_FILE"
else
    echo "  Preserved: $CONFIG_FILE"
fi
echo ""

# Install systemd user service with resolved paths
echo "[4/5] Installing systemd user service..."
mkdir -p "$USER_SERVICE_DIR"
sed \
    -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
    -e "s|__DAEMON_PATH__|${DAEMON_PY}|g" \
    "$SERVICE_FILE" > "$USER_SERVICE_DIR/$SERVICE_NAME.service"
systemctl --user daemon-reload
echo "  Installed: $USER_SERVICE_DIR/$SERVICE_NAME.service"
echo ""

# Enable service
echo "[5/5] Enabling service..."
systemctl --user enable "$SERVICE_NAME"

echo ""
echo "=== Done! ==="
echo ""
echo "The daemon will now start automatically when you log in"
echo "and connect to the device over Bluetooth Low Energy."
echo ""
echo "First-time Bluetooth pairing:"
echo "  1. Power on the device"
echo "  2. Run: bluetoothctl scan le"
echo "  3. Find 'Claude Controller' and note the MAC address"
echo "  4. Run: bluetoothctl pair <MAC>"
echo "  5. Run: bluetoothctl trust <MAC>"
echo "  6. Start the daemon: systemctl --user start $SERVICE_NAME"
echo "  7. Choose a provider: ./switch-provider.sh both|claude|codex"
echo ""
echo "Useful commands:"
echo "  systemctl --user status $SERVICE_NAME    # check status"
echo "  journalctl --user -u $SERVICE_NAME -f    # view logs"
echo "  systemctl --user restart $SERVICE_NAME   # restart"
echo "  systemctl --user stop $SERVICE_NAME      # stop"
echo ""
