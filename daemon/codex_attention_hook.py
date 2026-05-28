#!/usr/bin/env python3
"""Codex lifecycle hook that exposes attention state to DeskPulse."""

from __future__ import annotations

import json
import re
import sys
import time
from typing import Any

from codex_attention import (
    attention_request_id,
    clear_attention_decision,
    clear_attention_state,
    read_attention_decision,
    write_attention_state,
)

PERMISSION_WAIT_SECONDS = 120
PERMISSION_POLL_SECONDS = 0.25

QUESTION_PATTERNS = (
    re.compile(r"\?\s*$"),
    re.compile(r"\b(which|what|how|should|would you like|do you want)\b", re.I),
    re.compile(r"\b(please confirm|need input|need your|choose|decision|permission)\b", re.I),
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("{}")
        return 0
    if not isinstance(payload, dict):
        print("{}")
        return 0

    event = payload.get("hook_event_name")
    if event == "PermissionRequest":
        write_attention_state("approval_needed", payload, ttl_seconds=30 * 60)
        decision = _wait_for_permission_decision(payload)
        if decision:
            print(json.dumps(_permission_decision_output(decision), separators=(",", ":")))
            return 0
    elif event == "Stop":
        if _looks_like_user_input_needed(payload.get("last_assistant_message")):
            write_attention_state("needs_input", payload, ttl_seconds=60 * 60)
        else:
            clear_attention_state(payload)
    elif event in {"SessionStart", "UserPromptSubmit"}:
        clear_attention_state(payload)

    # Return neutral JSON so Stop/UserPromptSubmit hooks do not alter Codex flow.
    print("{}")
    return 0


def _wait_for_permission_decision(payload: dict[str, Any]) -> str | None:
    request_id = _request_id_from_state(payload)
    if not request_id:
        return None
    deadline = time.monotonic() + PERMISSION_WAIT_SECONDS
    while time.monotonic() < deadline:
        decision = read_attention_decision(request_id)
        if decision:
            clear_attention_decision(request_id)
            clear_attention_state(payload)
            return decision
        time.sleep(PERMISSION_POLL_SECONDS)
    clear_attention_state(payload)
    return None


def _permission_decision_output(decision: str) -> dict[str, Any]:
    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": decision,
            },
        },
    }
    if decision == "deny":
        output["hookSpecificOutput"]["decision"]["message"] = "Denied from DeskPulse."
    return output


def _request_id_from_state(payload: dict[str, Any]) -> str:
    return attention_request_id(payload)


def _looks_like_user_input_needed(message: Any) -> bool:
    if not isinstance(message, str):
        return False
    text = message.strip()
    if not text:
        return False
    tail = text[-400:]
    return any(pattern.search(tail) for pattern in QUESTION_PATTERNS)


if __name__ == "__main__":
    raise SystemExit(main())
