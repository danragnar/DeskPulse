#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SCRIPT="$SCRIPT_DIR/daemon/codex_attention_hook.py"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
HOOKS_JSON="$CODEX_HOME/hooks.json"
HOOK_TIMEOUT_SECONDS=130

if ! command -v python3 >/dev/null; then
    echo "Error: python3 is required but not installed"
    exit 1
fi

chmod +x "$HOOK_SCRIPT"
mkdir -p "$CODEX_HOME"

python3 - "$HOOKS_JSON" "$HOOK_SCRIPT" "$HOOK_TIMEOUT_SECONDS" <<'PY'
import json
import shlex
import sys
from pathlib import Path

hooks_path = Path(sys.argv[1]).expanduser()
hook_script = Path(sys.argv[2]).resolve()
hook_timeout = int(sys.argv[3])
command = f"{shlex.quote(sys.executable)} {shlex.quote(str(hook_script))}"
events = ("SessionStart", "PermissionRequest", "UserPromptSubmit", "Stop")

if hooks_path.exists():
    try:
        data = json.loads(hooks_path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{hooks_path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{hooks_path} must contain a JSON object")
else:
    data = {}

hooks = data.setdefault("hooks", {})
if not isinstance(hooks, dict):
    raise SystemExit(f"{hooks_path}: hooks must be an object")

changed = False
for event in events:
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        raise SystemExit(f"{hooks_path}: hooks.{event} must be a list")

    found = False
    filtered_groups = []
    for group in groups:
        if not isinstance(group, dict):
            filtered_groups.append(group)
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            filtered_groups.append(group)
            continue
        kept_handlers = []
        for handler in handlers:
            if not isinstance(handler, dict):
                kept_handlers.append(handler)
                continue
            handler_command = handler.get("command")
            is_deskpulse_hook = (
                isinstance(handler_command, str)
                and "codex_attention_hook.py" in handler_command
            )
            if handler_command == command:
                found = True
                if handler.get("timeout") != hook_timeout:
                    handler["timeout"] = hook_timeout
                    changed = True
                kept_handlers.append(handler)
            elif is_deskpulse_hook:
                changed = True
            else:
                kept_handlers.append(handler)
        if len(kept_handlers) != len(handlers):
            group["hooks"] = kept_handlers
            changed = True
        if kept_handlers:
            filtered_groups.append(group)
        else:
            changed = True

    if not found:
        filtered_groups.append({
            "hooks": [{
                "type": "command",
                "command": command,
                "timeout": hook_timeout,
                "statusMessage": "Updating DeskPulse",
            }]
        })
        changed = True
    if len(filtered_groups) != len(groups):
        changed = True
    hooks[event] = filtered_groups

if changed:
    hooks_path.write_text(json.dumps(data, indent=2) + "\n")

print(hooks_path)
PY

echo "Codex hook installed in $HOOKS_JSON"
echo "Open /hooks in Codex and trust the DeskPulse hook if Codex marks it for review."
