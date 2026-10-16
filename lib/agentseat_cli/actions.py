"""Pointer, keyboard, text and window input through the daemon."""

from __future__ import annotations

import time
from typing import Any

from . import common, transport


def sync_window_tree(*, focus_newest: bool = True) -> list[dict[str, Any]]:
    """Read the compositor's mapped application windows without implicit writes."""
    return transport.rpc("window.list")


def pause_seat() -> dict[str, Any]:
    result = transport.rpc("seat.pause")
    deadline = time.monotonic() + 3
    while True:
        heartbeat = transport.rpc("session.heartbeat")
        if heartbeat.get("session_id") != transport.RPC_SESSION_ID:
            raise common.AgentSeatError("SESSION_MISMATCH: session changed while waiting for pause")
        if "gate_epoch" not in heartbeat:
            return result  # Existing v1 sessions have no final-delivery acknowledgement.
        if not heartbeat["gate_allowed"] and heartbeat["gate_epoch"] >= heartbeat["control_epoch"]:
            return {**result, "compositor_acknowledged": True}
        if time.monotonic() >= deadline:
            raise common.AgentSeatError("PAUSE_UNCONFIRMED: compositor has not acknowledged the input gate")
        time.sleep(0.01)


def window_action(method: str, params: dict[str, Any]) -> dict[str, Any]:
    result = transport.rpc(method, params)
    if "operation_id" not in result:
        return result
    operation_id = result["operation_id"]
    deadline = time.monotonic() + 4
    while result.get("execution") == "running" and time.monotonic() < deadline:
        time.sleep(.01)
        result = transport.rpc("operation.get", {"operation_id": operation_id})
    if result.get("execution") != "finished":
        raise common.AgentSeatError(f"WINDOW_CHANGE_UNCONFIRMED: operation_id={operation_id}; {result.get('execution')}")
    return result


def pointer_click(button: int) -> Any:
    sync_window_tree()
    return transport.rpc("pointer.click", {"button": button})


def type_text(text: str, *, keys: bool = False) -> dict[str, Any]:
    # IME sessions paste so the engine cannot rewrite text; `keys` forces key
    # events for canvases, games and terminals that ignore paste.
    if not keys and common.load_state().get("launch", {}).get("ime") in {"fcitx5", "desktop", "inherit"}:
        return private_unicode_paste(text)
    result = transport.rpc("keyboard.type", {"text": text})
    # A pre-upgrade daemon returns the legacy synchronous result.
    if "operation_id" not in result:
        return result
    operation_id = result["operation_id"]
    origin = {key: result[key] for key in ("request_id", "request_controller_id") if key in result}
    deadline = time.monotonic() + 185
    renew_at = time.monotonic() + 2
    try:
        while result.get("execution") == "running":
            if time.monotonic() >= deadline:
                raise common.AgentSeatError("OPERATION_TIMEOUT: query the operation before any retry")
            time.sleep(0.025)
            if transport.RPC_WRITE_LEASE and time.monotonic() >= renew_at:
                transport.rpc("control.renew", {"lease_id": transport.RPC_WRITE_LEASE["lease_id"], "ttl_ms": 10000})
                renew_at = time.monotonic() + 2
            result = transport.rpc("operation.get", {"operation_id": operation_id})
    except (KeyboardInterrupt, common.AgentSeatError) as exc:
        try:
            transport.rpc("operation.cancel", {"operation_id": operation_id})
        except common.AgentSeatError:
            pass
        if isinstance(exc, common.AgentSeatError):
            raise common.AgentSeatError(
                f"OPERATION_UNCONFIRMED: operation_id={operation_id}; {exc}; "
                "observe before continuing"
            ) from exc
        raise
    if result.get("execution") != "finished":
        raise common.AgentSeatError(
            f"INPUT_INTERRUPTED: {result.get('execution')}; "
            f"submitted={result.get('submitted')}/{result.get('total')}; "
            f"operation_id={operation_id}; observe before continuing"
        )
    return {**result, **origin, "typed": result["submitted"]}


def pointer_move(x: float, y: float) -> Any:
    sync_window_tree()
    return transport.rpc("pointer.move_absolute", {"x": x, "y": y})


def pointer_scroll(axis: str, value120: int) -> Any:
    return transport.rpc("pointer.scroll", {"axis": axis, "value120": value120})


def keyboard_key(keycode: int, pressed: bool) -> Any:
    sync_window_tree()
    return transport.rpc("keyboard.key", {"keycode": keycode, "pressed": pressed})


PASTE_CHUNK_CODEPOINTS = 256


def paste_chunks(text: str, limit: int = PASTE_CHUNK_CODEPOINTS) -> list[str]:
    """Split text into bounded pastes, preferring a line or word boundary."""
    chunks: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = max(window.rfind("\n"), window.rfind(" "))
        cut = cut + 1 if cut >= limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:]
    if text:
        chunks.append(text)
    return chunks


def paste_once(text: str) -> dict[str, Any]:
    result = transport.rpc("keyboard.paste", {"text": text})
    operation_id = result["operation_id"]
    origin = {k: result[k] for k in ("request_id", "request_controller_id") if k in result}
    deadline = time.monotonic() + 7
    renew_at = time.monotonic() + 2
    try:
        while result.get("execution") == "running":
            if time.monotonic() >= deadline:
                raise common.AgentSeatError("OPERATION_TIMEOUT: paste transfer was not confirmed")
            time.sleep(0.025)
            if transport.RPC_WRITE_LEASE and time.monotonic() >= renew_at:
                transport.rpc("control.renew", {"lease_id": transport.RPC_WRITE_LEASE["lease_id"], "ttl_ms": 10000})
                renew_at = time.monotonic() + 2
            result = transport.rpc("operation.get", {"operation_id": operation_id})
    except (KeyboardInterrupt, common.AgentSeatError) as exc:
        try:
            transport.rpc("operation.cancel", {"operation_id": operation_id})
        except common.AgentSeatError:
            pass
        if isinstance(exc, common.AgentSeatError):
            raise common.AgentSeatError(f"OPERATION_UNCONFIRMED: operation_id={operation_id}; {exc}") from exc
        raise
    if result.get("execution") != "finished":
        raise common.AgentSeatError(f"INPUT_INTERRUPTED: {result.get('execution')}; operation_id={operation_id}")
    return {**result, **origin}


def private_unicode_paste(text: str) -> dict[str, Any]:
    """Native, lease-guarded paste; receipt confirms transport only.

    Long text is pasted in bounded chunks so pause, human takeover or lease
    loss stops the remaining text instead of delivering it in one transfer.
    """
    chunks = paste_chunks(text)
    transferred = 0
    operation_ids: list[str] = []
    result: dict[str, Any] = {}
    for index, chunk in enumerate(chunks):
        try:
            result = paste_once(chunk)
        except common.AgentSeatError as exc:
            raise common.AgentSeatError(
                f"{exc}; pasted_chunks={index}/{len(chunks)}; "
                f"transferred_codepoints={transferred}/{len(text)}; observe before any retry"
            ) from exc
        transferred += len(chunk)
        operation_ids.append(result["operation_id"])
    return {**result, "lane": "private-wayland-clipboard", "chunks": len(chunks),
            "operation_ids": operation_ids, "transferred_codepoints": transferred}
