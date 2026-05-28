#!/bin/bash
# Switch the Clawdmeter host daemon usage provider and reload the active service manager.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/daemon/config.toml"
SERVICE_NAME="claude-usage-daemon"
SERVICE_LABEL="com.user.claude-usage-daemon"
PLIST="$HOME/Library/LaunchAgents/$SERVICE_LABEL.plist"
SYSTEMD_UNIT="$HOME/.config/systemd/user/$SERVICE_NAME.service"
PROVIDER="${1:-}"

if [[ "$PROVIDER" != "claude" && "$PROVIDER" != "codex" && "$PROVIDER" != "both" ]]; then
    echo "Usage: $0 <claude|codex|both>"
    exit 1
fi

mkdir -p "$(dirname "$CONFIG")"
if [[ -f "$CONFIG" ]] && grep -Eq '^[[:space:]]*provider[[:space:]]*=' "$CONFIG"; then
    tmp="$(mktemp)"
    sed -E "s|^[[:space:]]*provider[[:space:]]*=.*|provider = \"$PROVIDER\"|" "$CONFIG" > "$tmp"
    mv "$tmp" "$CONFIG"
else
    printf 'provider = "%s"\n' "$PROVIDER" >> "$CONFIG"
fi

echo "Provider set to: $PROVIDER"

if command -v launchctl >/dev/null 2>&1 && [[ -f "$PLIST" ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load -w "$PLIST"

    echo "LaunchAgent reloaded: $SERVICE_LABEL"
    echo "The next usage poll should appear in the log within about 60 seconds:"
    echo "  tail -F ~/Library/Logs/claude-usage-daemon.out.log"
    exit 0
fi

if command -v systemctl >/dev/null 2>&1 && [[ -f "$SYSTEMD_UNIT" ]]; then
    if ! systemctl --user restart "$SERVICE_NAME"; then
        echo "Failed to restart systemd user service: $SERVICE_NAME"
        exit 1
    fi

    echo "systemd user service restarted: $SERVICE_NAME"
    echo "The next usage poll should appear in the journal within about 60 seconds:"
    echo "  journalctl --user -u $SERVICE_NAME -f"
    exit 0
fi

echo "Daemon is not installed yet."
echo "Run ./install-mac.sh on macOS or ./install.sh on Linux, then re-run this command."
