"""Codex attention-state helpers for DeskPulse."""

from __future__ import annotations

import json
import os
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_PATH = Path(
    os.environ.get(
        "DESKPULSE_CODEX_ATTENTION_PATH",
        "~/.config/claude-usage-monitor/codex-attention.json",
    )
).expanduser()
DECISION_PATH = STATE_PATH.with_name("codex-attention-decision.json")
ATTENTION_STATES = {"approval_needed", "needs_input"}
DEFAULT_TTL_SECONDS = 30 * 60
VALID_DECISIONS = {"allow", "deny"}


@dataclass(frozen=True)
class CodexAttentionState:
    status: str
    updated_at: int
    expires_at: int
    message: str = ""
    request_id: str = ""


def _now() -> int:
    return int(time.time())


def write_attention_state(
    status: str,
    payload: dict[str, Any],
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    now = _now()
    data = {
        "status": status,
        "updated_at": now,
        "expires_at": now + ttl_seconds,
        "hook_event_name": payload.get("hook_event_name", ""),
        "session_id": payload.get("session_id", ""),
        "turn_id": payload.get("turn_id", ""),
        "cwd": payload.get("cwd", ""),
        "message": _message_preview(payload),
        "request_id": attention_request_id(payload),
    }
    _write_state(data)


def clear_attention_state(payload: dict[str, Any] | None = None) -> None:
    now = _now()
    data = {
        "status": "idle",
        "updated_at": now,
        "expires_at": now,
    }
    if payload:
        data["hook_event_name"] = payload.get("hook_event_name", "")
        data["session_id"] = payload.get("session_id", "")
        data["turn_id"] = payload.get("turn_id", "")
        data["cwd"] = payload.get("cwd", "")
    _write_state(data)


def write_attention_decision(request_id: str, decision: str) -> bool:
    if not request_id or decision not in VALID_DECISIONS:
        return False
    now = _now()
    data = {
        "request_id": request_id,
        "decision": decision,
        "updated_at": now,
        "expires_at": now + DEFAULT_TTL_SECONDS,
    }
    DECISION_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DECISION_PATH.with_suffix(DECISION_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(DECISION_PATH)
    return True


def read_attention_decision(request_id: str) -> str | None:
    if not request_id:
        return None
    try:
        raw = DECISION_PATH.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("request_id") != request_id:
        return None
    if data.get("expires_at", 0) <= _now():
        return None
    decision = data.get("decision")
    return decision if decision in VALID_DECISIONS else None


def clear_attention_decision(request_id: str) -> None:
    if not request_id:
        return
    try:
        raw = DECISION_PATH.read_text()
        data = json.loads(raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return
    if isinstance(data, dict) and data.get("request_id") == request_id:
        DECISION_PATH.unlink(missing_ok=True)


def read_attention_state() -> CodexAttentionState | None:
    try:
        raw = STATE_PATH.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    status = data.get("status")
    expires_at = data.get("expires_at")
    updated_at = data.get("updated_at")
    if status not in ATTENTION_STATES:
        return None
    if not isinstance(expires_at, int) or expires_at <= _now():
        return None
    if not isinstance(updated_at, int):
        updated_at = 0

    message = data.get("message")
    request_id = data.get("request_id")
    return CodexAttentionState(
        status=status,
        updated_at=updated_at,
        expires_at=expires_at,
        message=message if isinstance(message, str) else "",
        request_id=request_id if isinstance(request_id, str) else "",
    )


def _write_state(data: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(STATE_PATH)


def _message_preview(payload: dict[str, Any]) -> str:
    value = payload.get("last_assistant_message")
    if not isinstance(value, str):
        tool_input = payload.get("tool_input")
        if isinstance(tool_input, dict):
            value = (
                tool_input.get("description")
                or tool_input.get("command")
                or tool_input.get("cmd")
            )
            if not isinstance(value, str):
                value = _compact_json(tool_input)
    if not isinstance(value, str):
        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str):
            value = tool_name
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:160]


def attention_request_id(payload: dict[str, Any]) -> str:
    parts = {
        "session_id": payload.get("session_id", ""),
        "turn_id": payload.get("turn_id", ""),
        "tool_name": payload.get("tool_name", ""),
        "tool_input": payload.get("tool_input", {}),
    }
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
