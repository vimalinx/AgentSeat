"""Read-only queries of the host Hyprland session and output geometry."""

from __future__ import annotations

import json
import math
import os
from typing import Any

from . import common, instances, transport
from .i18n import _


def host_instance() -> dict[str, Any]:
    candidates = instances.instances()
    if not candidates:
        raise common.AgentSeatError(_("host Hyprland instance not found"))
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    return next((item for item in candidates if item["instance"] == signature), candidates[0])


def hypr_json(signature: str, command: str) -> Any:
    return json.loads(common.run(["hyprctl", "--instance", signature, command, "-j"]).stdout)


def host_client_for_pid(host_signature: str, pid: int) -> dict[str, Any] | None:
    clients = json.loads(common.run(["hyprctl", "--instance", host_signature, "clients", "-j"]).stdout)
    return next((client for client in clients if int(client.get("pid", -1)) == pid), None)


def host_snapshot(host: dict[str, Any]) -> dict[str, Any]:
    signature = str(host["instance"])
    workspace = hypr_json(signature, "activeworkspace")
    window = hypr_json(signature, "activewindow")
    cursor = hypr_json(signature, "cursorpos")
    return {
        "workspace": workspace.get("id"),
        "window": window.get("address"),
        "title": window.get("title"),
        "cursor": [cursor.get("x"), cursor.get("y")],
    }


def same_host_focus(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return all(before.get(key) == after.get(key) for key in ("workspace", "window", "title"))


def host_focus_relation(before: dict[str, Any], after: dict[str, Any], outer_window: str) -> str:
    if same_host_focus(before, after):
        return "unchanged"
    if after.get("window") == outer_window:
        return "outer-focused"
    return "concurrent-host-change"


def launch_window_properties(host_workspace: int, active_workspace: int | None) -> str:
    if host_workspace <= 0:
        raise common.AgentSeatError(_("host workspace must be a positive integer"))
    properties = [
        "no_initial_focus true",
        "focus_on_activate false",
        "suppress_event activate activatefocus",
    ]
    if host_workspace != active_workspace:
        # Hyprland 0.56 uses snake_case rule effects. This is a one-shot
        # placement rule, not `workspace N silent`.
        # It assigns only the new outer window and does not switch the host.
        properties.insert(0, f"workspace {host_workspace}")
    return f"[{'; '.join(properties)}]"


def select_host_output_scale(
    workspaces: list[dict[str, Any]],
    monitors: list[dict[str, Any]],
    host_workspace: int,
    active_workspace: int | None,
) -> float:
    target = next((item for item in workspaces if int(item.get("id", -1)) == host_workspace), None)
    target_monitor_id = target.get("monitorID") if target else None
    target_monitor_name = target.get("monitor") if target else None
    monitor = next(
        (
            item
            for item in monitors
            if (target_monitor_id is not None and item.get("id") == target_monitor_id)
            or (target_monitor_name and item.get("name") == target_monitor_name)
        ),
        None,
    )
    if monitor is None and active_workspace is not None:
        monitor = next(
            (item for item in monitors if item.get("activeWorkspace", {}).get("id") == active_workspace),
            None,
        )
    if monitor is None:
        monitor = next((item for item in monitors if item.get("focused")), monitors[0] if monitors else None)
    try:
        return max(1.0, float(monitor.get("scale", 1.0))) if monitor else 1.0
    except (TypeError, ValueError):
        return 1.0


def physical_size_for_host(logical_size: int, host_scale: float) -> int:
    return max(1, math.ceil(logical_size * host_scale))


def refresh_outer_geometry(state_value: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read geometry reported by the private compositor; never query or write host geometry."""
    current = dict(state_value if state_value is not None else common.load_state())
    if not current:
        return current
    geometry = transport.rpc("session.heartbeat").get("output")
    if not geometry:
        return current
    current["application_coordinate_size"] = [geometry["width"], geometry["height"]]
    if current.get("launch", {}).get("mode") == "x11":
        host_scale = float(current.get("host_output_scale", 1.0))
        current["viewport_logical_size"] = [
            round(geometry[key] / host_scale) for key in ("pixel_width", "pixel_height")
        ]
    else:
        current["viewport_logical_size"] = [geometry["width"], geometry["height"]]
    current["physical_buffer_size"] = [geometry["pixel_width"], geometry["pixel_height"]]
    current["render_scale"] = geometry["scale"]
    current["geometry_version"] = geometry["geometry_version"]
    return current
