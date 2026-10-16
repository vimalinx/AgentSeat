"""Frame capture and waiting for content changes."""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from . import actions, common, paths, transport
from .i18n import _


def safe_capture_path(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute() or candidate.suffix.casefold() != ".png":
        raise common.AgentSeatError(_("capture output must be an absolute .png path"))
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise common.AgentSeatError(_("capture output parent is unavailable: {error}").format(error=exc)) from exc
    if not parent.is_dir() or parent.stat().st_uid != os.getuid():
        raise common.AgentSeatError(_("capture output parent must be a directory owned by the current user"))
    output = parent / candidate.name
    if output.is_symlink():
        raise common.AgentSeatError(_("capture output cannot be a symbolic link"))
    if output.exists() and (not output.is_file() or output.stat().st_uid != os.getuid()):
        raise common.AgentSeatError(_("existing capture output must be a regular file owned by the current user"))
    return output


def observe(path: str | None) -> dict[str, Any]:
    state_value = common.load_state()
    windows = actions.sync_window_tree()
    seat_status = transport.rpc("seat.status")
    focused_id = str(seat_status["seat"].get("focused_window") or state_value.get("window_id") or "")
    window = next((item for item in windows if str(item["id"]) == focused_id), windows[0] if windows else None)
    if not window:
        raise common.AgentSeatError(_("no leased application window is available"))
    common.ensure_private_directory(paths.EVIDENCE)
    output = safe_capture_path(Path(path) if path else paths.EVIDENCE / f"agentseat-{int(time.time())}.png")
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp.png")
    frame = transport.rpc("capture.submit")
    deadline = time.monotonic() + 5
    while frame["status"] in {"pending", "encoding"}:
        if time.monotonic() >= deadline:
            raise common.AgentSeatError("FRAME_UNCONFIRMED: capture receipt remains pending")
        time.sleep(0.01)
        frame = transport.rpc("capture.get", {"frame_id": frame["frame_id"]})
    if frame["status"] != "finished":
        raise common.AgentSeatError(f"CAPTURE_FAILED: {frame.get('error')}")
    try:
        source = Path(frame["path"])
        if source.parent != paths.CONTROL_SOCKET.parent or source.is_symlink():
            raise common.AgentSeatError("invalid private frame location")
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            os.chmod(temporary, 0o600)
            shutil.copyfileobj(incoming, outgoing)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(output), "backend": "wlr-screencopy", "window": window,
        "capture": {"backend": "native-wlr-screencopy", "scope": "single-app-output", **frame},
        "capture_passes": 1, "settle_ms": 0,
        # Pass to wait-change --since so a change after this frame is never missed.
        "content_serial": frame.get("content_serial"),
    }


WAIT_CHANGE_DEFAULT_MIN_EXTENT = 5


def wait_for_change(
    since: int | None = None,
    timeout_ms: int = 5000,
    region: tuple[float, float, float, float] | None = None,
    min_area_ratio: float = 0.0,
    settle_ms: int = 0,
    min_extent: float = WAIT_CHANGE_DEFAULT_MIN_EXTENT,
) -> dict[str, Any]:
    """Wait for damaged application output, read-only and bounded.

    A change qualifies when its damage box is at least min_extent logical
    pixels wide and tall, covers min_area_ratio of the output and intersects
    region (normalized x, y, width, height). Thin caret blinks are ignored.
    """
    if not 0 <= timeout_ms <= 60000:
        raise common.AgentSeatError("INVALID_PARAMS: --timeout-ms must be from 0 to 60000")
    if not 0 <= settle_ms <= 10000:
        raise common.AgentSeatError("INVALID_PARAMS: --settle-ms must be from 0 to 10000")
    if not 0 <= min_area_ratio <= 1:
        raise common.AgentSeatError("INVALID_PARAMS: --min-area-ratio must be from 0 to 1")
    if not 0 <= min_extent <= 1000:
        raise common.AgentSeatError("INVALID_PARAMS: --min-extent must be from 0 to 1000 logical pixels")
    if region is not None:
        x, y, width, height = region
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < width <= 1 and 0 < height <= 1):
            raise common.AgentSeatError("INVALID_PARAMS: --region uses normalized X Y WIDTH HEIGHT")
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    if since is None:
        since = transport.rpc("session.heartbeat").get("output", {}).get("content_serial")
        if since is None:
            raise common.AgentSeatError("UNSUPPORTED: this microhost does not report content changes")
    elif not 0 <= since < 2**32:
        raise common.AgentSeatError("INVALID_PARAMS: --since must be a content serial")
    cursor = since
    qualifying = ignored = 0
    gap = False
    union: list[int] | None = None
    last_change: float | None = None
    output: dict[str, Any] = {}
    status = "timeout"
    while True:
        reply = transport.rpc("content.changes", {"after_serial": cursor})
        output = reply["output"]
        pixel_width = max(int(output.get("pixel_width") or 0), 1)
        pixel_height = max(int(output.get("pixel_height") or 0), 1)
        min_pixels = min_extent * float(output.get("scale") or 1)
        if region is not None:
            rx, ry = region[0] * pixel_width, region[1] * pixel_height
            rx2, ry2 = rx + region[2] * pixel_width, ry + region[3] * pixel_height
        if reply["gap"]:
            # Evicted changes may have qualified; fail toward "changed".
            gap = True
            last_change = time.monotonic()
        for change in reply["changes"]:
            x1, y1 = change["x"], change["y"]
            x2, y2 = x1 + change["width"], y1 + change["height"]
            inside = region is None or (x1 < rx2 and x2 > rx and y1 < ry2 and y2 > ry)
            large = change["width"] >= min_pixels and change["height"] >= min_pixels
            if inside and large and change["area"] >= min_area_ratio * pixel_width * pixel_height:
                qualifying += 1
                last_change = time.monotonic()
                union = [x1, y1, x2, y2] if union is None else [
                    min(union[0], x1), min(union[1], y1), max(union[2], x2), max(union[3], y2)]
            else:
                ignored += 1
        cursor = reply["content_serial"]
        now = time.monotonic()
        if last_change is not None and (settle_ms == 0 or now - last_change >= settle_ms / 1000):
            status = "settled" if settle_ms else "changed"
            break
        if now >= deadline:
            status = "timeout" if last_change is None else "changed_unsettled"
            break
        time.sleep(0.02)
    result: dict[str, Any] = {
        "status": status, "since_serial": since, "content_serial": cursor,
        "qualifying_changes": qualifying, "ignored_changes": ignored, "gap": gap,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "min_area_ratio": min_area_ratio, "min_extent": min_extent, "region": list(region) if region else None,
        "verification": "damage_only; observe to read the new content",
    }
    if union is not None:
        pixel_width = max(int(output.get("pixel_width") or 0), 1)
        pixel_height = max(int(output.get("pixel_height") or 0), 1)
        result["changed_box"] = {
            "x": union[0] / pixel_width, "y": union[1] / pixel_height,
            "width": (union[2] - union[0]) / pixel_width, "height": (union[3] - union[1]) / pixel_height,
            "coordinate_space": "normalized-output",
        }
    return result
