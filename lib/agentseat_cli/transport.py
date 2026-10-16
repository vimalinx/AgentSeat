"""JSON-RPC to the session daemon, with session pinning and the write lease."""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any

from . import common, paths
from .i18n import _

RPC_LIMIT = 1_048_576
RPC_SESSION_ID: str | None = None
RPC_CONTROL_EPOCH: int | None = None
RPC_WRITE_LEASE: dict[str, Any] | None = None
RPC_CONTROLLER_ID = os.environ.get("AGENTSEAT_CONTROLLER_ID", "")
if not RPC_CONTROLLER_ID:
    parent_pid = os.getppid()
    try:
        parent_start = Path(f"/proc/{parent_pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        parent_start = uuid.uuid4().hex
    RPC_CONTROLLER_ID = f"cli:{parent_pid}:{parent_start}"
WRITE_METHODS = frozenset({
    "seat.create", "seat.destroy", "seat.pause", "seat.resume", "shutdown",
    "window.geometry", "window.lease", "window.release", "window.focus",
    "window.move", "window.resize", "window.close", "pointer.move_absolute", "pointer.button",
    "pointer.click", "pointer.scroll", "keyboard.key", "keyboard.type", "keyboard.paste",
    "operation.cancel", "activity.set", "chat.send", "chat.reply",
    "control.acquire", "control.renew", "control.release",
    "a11y.perform", "a11y.set_text", "a11y.set_value", "a11y.focus", "a11y.select", "a11y.scroll_to",
})


def version() -> dict[str, Any]:
    result: dict[str, Any] = {
        "controller": "agentseat",
        "version": paths.AGENTSEAT_VERSION,
        "protocol_version": 2,
        "daemon": None,
    }
    if paths.CONTROL_SOCKET.exists():
        try:
            result["daemon"] = rpc("version")
        except common.AgentSeatError as exc:
            result["daemon_error"] = str(exc)
    return result


def rpc(method: str, params: dict[str, Any] | None = None) -> Any:
    """Call the resident AgentSeat daemon without spawning an intermediate CLI."""
    global RPC_SESSION_ID, RPC_CONTROL_EPOCH, RPC_WRITE_LEASE
    payload = dict(params or {})
    if method in WRITE_METHODS:
        if RPC_SESSION_ID is None:
            value = common.load_state().get("session_id")
            if not isinstance(value, str) or not value:
                raise common.AgentSeatError("SESSION_REQUIRED: no pinned session is available for this write")
            RPC_SESSION_ID = value
        payload.setdefault("session_id", RPC_SESSION_ID)
        payload.setdefault("controller_id", RPC_CONTROLLER_ID)
        if method.startswith(("pointer.", "keyboard.", "a11y.")) or method in {"window.focus", "window.move", "window.resize", "window.close"}:
            if RPC_CONTROL_EPOCH is None:
                heartbeat = rpc("session.heartbeat")
                if heartbeat.get("session_id") != RPC_SESSION_ID:
                    raise common.AgentSeatError("SESSION_MISMATCH: session changed before input")
                RPC_CONTROL_EPOCH = int(heartbeat.get("control_epoch", 0))
                if "write_control" in heartbeat:
                    RPC_WRITE_LEASE = rpc("control.acquire", {"ttl_ms": 10000})
                    RPC_CONTROL_EPOCH = RPC_WRITE_LEASE["control_epoch"]
            payload.setdefault("control_epoch", RPC_CONTROL_EPOCH)
            if RPC_WRITE_LEASE:
                payload.setdefault("lease_id", RPC_WRITE_LEASE["lease_id"])
        # Monotonic IDs make receipt eviction fail closed for old requests.
        # Generate after any recursive heartbeat/acquire call.
        payload.setdefault("request_id", time.monotonic_ns())
    request_id = uuid.uuid4().hex
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": payload,
    }
    encoded = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    received = bytearray()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(15)
            client.connect(str(paths.CONTROL_SOCKET))
            client.sendall(encoded)
            while b"\n" not in received:
                block = client.recv(65_536)
                if not block:
                    break
                received.extend(block)
                if len(received) > RPC_LIMIT:
                    raise common.AgentSeatError(_("AgentSeat daemon response exceeds 1 MiB"))
    except OSError as exc:
        detail = _("cannot reach AgentSeat daemon: {error}").format(error=exc)
        if method in WRITE_METHODS:
            detail += f"; controller_id={payload['controller_id']}; request_id={payload['request_id']}; query receipt before retry"
        raise common.AgentSeatError(detail) from exc
    if not received:
        raise common.AgentSeatError(_("AgentSeat daemon returned no response"))
    try:
        response = json.loads(bytes(received).split(b"\n", 1)[0])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise common.AgentSeatError(_("invalid AgentSeat daemon response: {error}").format(error=exc)) from exc
    if response.get("id") != request_id:
        raise common.AgentSeatError(_("AgentSeat daemon response ID mismatch"))
    if "error" in response:
        error = response["error"]
        details = error.get("data", {})
        code = details.get("kind", error.get("code", "AGENTSEAT_ERROR"))
        message = error.get("message", _("AgentSeat daemon rejected the request"))
        raise common.AgentSeatError(f"{code}: {message}")
    return response.get("result")


def release_write_control() -> dict[str, Any]:
    global RPC_WRITE_LEASE, RPC_CONTROL_EPOCH
    lease = RPC_WRITE_LEASE or rpc("control.status")
    if lease.get("controller_id") != RPC_CONTROLLER_ID or not lease.get("lease_id"):
        return {"released": False}
    result = rpc("control.release", {"lease_id": lease["lease_id"]})
    RPC_WRITE_LEASE = None
    RPC_CONTROL_EPOCH = None
    return result
