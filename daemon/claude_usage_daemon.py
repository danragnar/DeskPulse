#!/usr/bin/env python3
"""Claude Usage Tracker Daemon (BLE) for Linux and macOS.

Polls Claude API rate-limit headers and writes a JSON payload to the
ESP32 "Claude Controller" peripheral over a custom GATT service. Uses
bleak on the active platform backend.
"""

import asyncio
import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from codex_attention import clear_attention_state, read_attention_state, write_attention_decision
from providers import Provider, Usage
from providers.claude import ClaudeProvider
from providers.codex import CodexProvider

DEVICE_NAME = "Claude Controller"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
TX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000003"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = 60
TICK = 5
SCAN_TIMEOUT = 8.0

SAVED_ADDR_FILE = Path.home() / ".config" / "claude-usage-monitor" / "ble-address"
CONFIG_PATH = Path(__file__).with_name("config.toml")
VALID_PROVIDERS = {"claude", "codex", "both"}
DUAL_PROVIDER_KEYS = {"claude": "c", "codex": "x"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cached_address() -> str | None:
    if not SAVED_ADDR_FILE.exists():
        return None
    addr = SAVED_ADDR_FILE.read_text().strip()
    # Accept both Linux MAC (AA:BB:CC:DD:EE:FF) and macOS CoreBluetooth UUID
    # (E621E1F8-C36C-495A-93FC-0C247A3E6E5F).
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", addr) or re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", addr
    ):
        return addr
    log("Cached address malformed, discarding")
    SAVED_ADDR_FILE.unlink(missing_ok=True)
    return None


def save_address(addr: str) -> None:
    SAVED_ADDR_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAVED_ADDR_FILE.write_text(addr)


async def scan_for_device() -> str | None:
    log(f"Scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)
    for d in devices:
        if d.name == DEVICE_NAME:
            log(f"Found: {d.address}")
            return d.address
    return None


def usage_to_payload(usage: Usage) -> dict:
    payload = {
        "s": usage.session_pct,
        "sr": usage.session_reset_min,
        "w": usage.weekly_pct,
        "wr": usage.weekly_reset_min,
        "st": usage.status,
        "ok": usage.ok,
    }
    if usage.attention_message:
        payload["m"] = usage.attention_message
    if usage.attention_request_id:
        payload["r"] = usage.attention_request_id
    return payload


def apply_attention_state(provider: Provider, usage: Usage) -> Usage:
    if provider.name != "codex" or not usage.ok:
        return usage
    attention = read_attention_state()
    if attention is None:
        return usage
    return Usage(
        usage.session_pct,
        usage.session_reset_min,
        usage.weekly_pct,
        usage.weekly_reset_min,
        attention.status,
        usage.ok,
        attention.message,
        attention.request_id,
    )


def failed_usage(status: str) -> Usage:
    return Usage(0, 0, 0, 0, status, False)


def single_provider_payload(provider: Provider, usage: Usage) -> dict:
    payload = usage_to_payload(usage)
    payload["p"] = provider.name
    return payload


def dual_provider_payload(usages: dict[str, Usage]) -> dict:
    claude_usage = usages.get("claude", failed_usage("claude_missing"))
    payload = usage_to_payload(claude_usage)
    payload["p"] = "both"
    for name, key in DUAL_PROVIDER_KEYS.items():
        payload[key] = usage_to_payload(usages.get(name, failed_usage(f"{name}_missing")))
    return payload


def clear_delivered_transient_attention(payload: dict) -> None:
    """Approval prompts are edge notifications; clear after one successful send."""
    statuses: list[str] = []
    top_status = payload.get("st")
    if isinstance(top_status, str):
        statuses.append(top_status)
    if isinstance(payload.get("r"), str):
        return

    codex_payload = payload.get("x")
    if isinstance(codex_payload, dict):
        codex_status = codex_payload.get("st")
        if isinstance(codex_status, str):
            statuses.append(codex_status)
        if isinstance(codex_payload.get("r"), str):
            return

    if "approval_needed" in statuses:
        clear_attention_state({"hook_event_name": "DeskPulseDelivered"})


def load_config_provider() -> str | None:
    try:
        raw = CONFIG_PATH.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        log(f"Config read failed: {e}")
        return None
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or not line.startswith("provider"):
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip() == "provider":
            provider = value.strip().strip('"').strip("'").lower()
            if provider in VALID_PROVIDERS:
                return provider
            log(f"Ignoring invalid provider in config: {provider}")
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clawdmeter BLE usage daemon")
    parser.add_argument(
        "--provider",
        choices=sorted(VALID_PROVIDERS),
        help="Usage provider. Overrides CLAWDMETER_PROVIDER and daemon/config.toml.",
    )
    return parser.parse_args(argv)


def resolve_provider_name(args: argparse.Namespace) -> tuple[str, str]:
    if args.provider:
        return args.provider, "flag"
    env_provider = os.environ.get("CLAWDMETER_PROVIDER", "").strip().lower()
    if env_provider:
        if env_provider in VALID_PROVIDERS:
            return env_provider, "environment"
        log(f"Ignoring invalid CLAWDMETER_PROVIDER={env_provider}")
    config_provider = load_config_provider()
    if config_provider:
        return config_provider, "config"
    return "claude", "default"


def build_providers(name: str) -> list[Provider]:
    if name == "both":
        return [ClaudeProvider(log), CodexProvider(log)]
    if name == "codex":
        return [CodexProvider(log)]
    return [ClaudeProvider(log)]


async def fetch_usage_payload(providers: list[Provider]) -> dict | None:
    if len(providers) == 1:
        usage = await providers[0].fetch_usage()
        if usage is None:
            return None
        usage = apply_attention_state(providers[0], usage)
        return single_provider_payload(providers[0], usage)

    results = await asyncio.gather(
        *(provider.fetch_usage() for provider in providers),
        return_exceptions=True,
    )
    by_name: dict[str, Usage] = {}
    for provider, result in zip(providers, results):
        if isinstance(result, Exception):
            log(f"{provider.name} usage fetch failed: {result}")
            by_name[provider.name] = failed_usage(f"{provider.name}_error")
        elif result is None:
            by_name[provider.name] = failed_usage(f"{provider.name}_error")
        else:
            by_name[provider.name] = apply_attention_state(provider, result)

    return dual_provider_payload(by_name)


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()
        self.attention_key: tuple[str, int] | None = None

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    def _on_device_message(self, _char, data: bytearray) -> None:
        try:
            message = json.loads(data.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(message, dict):
            return
        action = message.get("action")
        request_id = message.get("request_id")
        if action in {"allow", "deny"} and isinstance(request_id, str):
            if write_attention_decision(request_id, action):
                log(f"Permission {action} received from device")

    async def setup_refresh_subscription(self) -> None:
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (BleakError, ValueError) as e:
            log(f"Refresh subscription unavailable: {e}")

    async def setup_device_message_subscription(self) -> None:
        try:
            await self.client.start_notify(TX_CHAR_UUID, self._on_device_message)
        except (BleakError, ValueError) as e:
            log(f"Device message subscription unavailable: {e}")

    def attention_changed(self) -> bool:
        attention = read_attention_state()
        if attention is None:
            key = None
        else:
            key = (attention.request_id or attention.status, attention.updated_at)
        changed = key != self.attention_key
        if not changed:
            return False
        self.attention_key = key
        return True

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending ({len(data)} bytes): {data.decode()}")
        try:
            char = self.client.services.get_characteristic(RX_CHAR_UUID)
            max_write_without_response = (
                char.max_write_without_response_size if char is not None else 20
            )
            response = len(data) > max_write_without_response
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=response)
            return True
        except BleakError as e:
            if len(data) <= 512:
                try:
                    log(f"Write without response failed, retrying with response: {e}")
                    await self.client.write_gatt_char(RX_CHAR_UUID, data, response=True)
                    return True
                except BleakError as retry_error:
                    log(f"Write failed: {retry_error}")
                    return False
            log(f"Write failed: {e}")
            return False


async def connect_and_run(
    address: str,
    stop_event: asyncio.Event,
    providers: list[Provider],
) -> bool:
    """Connect to a known address and poll until disconnected or stopped.

    Returns True if the connection was used successfully (so the caller
    keeps the cached address), False if the connection failed and the
    cache should be invalidated.
    """
    log(f"Connecting to {address}...")
    client = BleakClient(address)
    try:
        await client.connect()
    except (BleakError, asyncio.TimeoutError) as e:
        log(f"Connection failed: {e}")
        return False

    if not client.is_connected:
        log("Connection failed (no error but not connected)")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()
    await session.setup_device_message_subscription()

    last_poll = 0.0
    used_successfully = False
    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            if session.refresh_requested.is_set() or session.attention_changed() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                payload = await fetch_usage_payload(providers)
                if payload is not None and await session.write_payload(payload):
                    clear_delivered_transient_attention(payload)
                    last_poll = time.time()
                    used_successfully = True

            try:
                await asyncio.wait_for(session.refresh_requested.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await client.disconnect()
        except BleakError:
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)

    provider_name, provider_source = resolve_provider_name(args)
    providers = build_providers(provider_name)

    log("=== Clawdmeter Usage Tracker Daemon (BLE, Linux/macOS) ===")
    log(f"Provider: {provider_name} ({provider_source})")
    log(f"Poll interval: {POLL_INTERVAL}s")

    backoff = 1
    while not stop_event.is_set():
        address = load_cached_address()
        if not address:
            address = await scan_for_device()
            if address:
                save_address(address)
            else:
                log(f"Device not found, retrying in {backoff}s...")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60)
                continue

        ok = await connect_and_run(address, stop_event, providers)
        if not ok:
            log("Invalidating cached address")
            SAVED_ADDR_FILE.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
