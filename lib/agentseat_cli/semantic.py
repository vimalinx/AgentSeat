"""Accessibility reads and actions, activity and messages."""

from __future__ import annotations

import json
import os
import socket
import struct
import time
from pathlib import Path
from typing import Any

from . import common, paths, procs, transport

EXPECTED_A11Y_SCOPE = None


def a11y_read(action: str, **params: Any) -> Any:
    state_value = common.load_state()
    if not state_value.get("launch", {}).get("a11y"):
        raise common.AgentSeatError("A11Y_DISABLED: launch this application with --a11y")
    try:
        receipt = json.loads((paths.RUNTIME / "a11y.json").read_text())
        pid = int(receipt["bridge_pid"])
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[19] != receipt["bridge_start"]:
            raise common.AgentSeatError("A11Y_SCOPE_STALE")
        cursor = pid
        for _ in range(32):
            if cursor == procs.microhost_pid():
                break
            cursor = int(Path(f"/proc/{cursor}/stat").read_text().rsplit(")", 1)[1].split()[1])
            if cursor <= 1:
                raise common.AgentSeatError("A11Y_SCOPE_OUTSIDE_APPLICATION")
        else:
            raise common.AgentSeatError("A11Y_SCOPE_OUTSIDE_APPLICATION")
        if receipt["socket"] != str(paths.RUNTIME / "a11y.sock"):
            raise common.AgentSeatError("A11Y_SOCKET_MISMATCH")
        method = action if action.startswith(("events.", "wait.")) else f"a11y.{action}"
        expected_scope = params.pop("expect_scope", EXPECTED_A11Y_SCOPE)
        if expected_scope is not None and expected_scope != receipt["scope_id"]:
            raise common.AgentSeatError("A11Y_SCOPE_CHANGED")
        request = {**params, "scope_id": receipt["scope_id"], "method": method}
        if "value" in request:
            request["node_ref" if action in {"get", "text", "text_state"} else "query"] = request.pop("value")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(4)
            client.connect(receipt["socket"])
            peer_pid, peer_uid, _ = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if peer_pid != pid or peer_uid != os.getuid():
                raise common.AgentSeatError("A11Y_BRIDGE_PEER_MISMATCH")
            client.sendall((json.dumps(request) + "\n").encode())
            data = bytearray()
            while b"\n" not in data:
                chunk = client.recv(65536)
                if not chunk or len(data) + len(chunk) > transport.RPC_LIMIT:
                    raise common.AgentSeatError("A11Y_RESPONSE_INVALID")
                data.extend(chunk)
        response = json.loads(data.split(b"\n")[0])
        if "error" in response:
            raise common.AgentSeatError(response["error"]["kind"])
        return response["result"]
    except (OSError, KeyError, ValueError) as error:
        raise common.AgentSeatError(f"A11Y_UNAVAILABLE: {error}") from error


def semantic_action(method: str, params: dict[str, Any]) -> dict[str, Any]:
    result = transport.rpc(method, params)
    origin = {key: result[key] for key in ("request_id", "request_controller_id") if key in result}
    operation_id = result["operation_id"]
    deadline, renew_at = time.monotonic() + 16, time.monotonic() + 2
    try:
        while result.get("execution") == "running":
            if time.monotonic() >= deadline:
                raise common.AgentSeatError(f"OPERATION_UNCONFIRMED: query {operation_id} before attempting another action")
            time.sleep(.02)
            result = transport.rpc("operation.get", {"operation_id": operation_id})
            if transport.RPC_WRITE_LEASE and result.get("execution") == "running" and time.monotonic() >= renew_at:
                transport.rpc("control.renew", {"lease_id": transport.RPC_WRITE_LEASE["lease_id"], "ttl_ms": 10000})
                renew_at = time.monotonic() + 2
    except (KeyboardInterrupt, common.AgentSeatError):
        try: transport.rpc("operation.cancel", {"operation_id": operation_id})
        except common.AgentSeatError: pass
        raise
    return {**result, **origin}


def semantic_wait(condition: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    result = a11y_read("wait.start", condition=condition, timeout_ms=timeout_ms)
    wait_id = result["wait_id"]
    deadline = time.monotonic() + timeout_ms / 1000 + 4
    try:
        while result["status"] == "pending":
            if time.monotonic() >= deadline:
                raise common.AgentSeatError("WAIT_UNCONFIRMED")
            time.sleep(.05)
            result = a11y_read("wait.get", wait_id=wait_id)
    except (KeyboardInterrupt, common.AgentSeatError):
        try: a11y_read("wait.cancel", wait_id=wait_id)
        except common.AgentSeatError: pass
        raise
    return result


def collaboration_status() -> dict[str, Any]:
    return transport.rpc("collaboration.status")


def set_activity(summary: str, detail: str) -> dict[str, Any]:
    return transport.rpc("activity.set", {"summary": summary, "detail": detail})


def list_messages(after_id: int) -> dict[str, Any]:
    return transport.rpc("chat.list", {"after_id": after_id})


def reply_message(text: str) -> dict[str, Any]:
    return transport.rpc("chat.reply", {"text": text})
