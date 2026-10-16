"""Read-only doctor and capability reports."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import common, lifecycle, paths, procs, semantic, transport

# Chromium and Electron expose only the window frame until renderer
# accessibility is on; the web contents appear as an AT-SPI null child.
CHROMIUM_WEB_HIDDEN = ("Chromium/Electron web content is not exposed; relaunch the application "
                       "with --force-renderer-accessibility for semantic reads")


def probe_session() -> dict[str, Any]:
    """Collect read-only session facts; never focuses, acquires or injects."""
    facts: dict[str, Any] = {"running": bool(procs.microhost_pid() and paths.CONTROL_SOCKET.exists())}
    if not facts["running"]:
        return facts
    try:
        facts["heartbeat"] = transport.rpc("session.heartbeat")
        facts["windows"] = transport.rpc("window.list")
    except common.AgentSeatError as exc:
        facts["error"] = str(exc)
        return facts
    facts["daemon"] = transport.version().get("daemon") or {}
    facts["launch"] = common.load_state().get("launch", {})
    if facts["launch"].get("a11y"):
        try:
            facts["a11y"] = semantic.a11y_read("status")
            snapshot = semantic.a11y_read("snapshot", max_nodes=8, max_depth=3)
            facts["a11y_nodes"] = len(snapshot.get("nodes", []))
            toolkits = {node.get("toolkit") for node in snapshot.get("nodes", [])} - {None}
            if "Chromium" in toolkits and snapshot.get("null_children"):
                facts["a11y_note"] = CHROMIUM_WEB_HIDDEN
        except common.AgentSeatError as exc:
            if "SEMANTICS_UNAVAILABLE" in str(exc):
                facts["a11y_nodes"] = 0
            else:
                facts["a11y_error"] = str(exc)
    return facts


def python_module_available(statement: str) -> bool:
    try:
        return subprocess.run([sys.executable, "-c", statement], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def doctor(probe_capture: bool = False) -> dict[str, Any]:
    """Diagnose only: report backends, scope, mapping and degradations."""
    checks: list[dict[str, Any]] = []

    def check(name: str, status: str, detail: Any = None) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    check("python", "ok" if sys.version_info >= (3, 11) else "fail", platform.python_version())
    hyprland = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"))
    check("hyprland_host", "ok" if hyprland else "fail",
          None if hyprland else "HYPRLAND_INSTANCE_SIGNATURE and hyprctl are required for the host window")
    check("wayland_display", "ok" if os.environ.get("WAYLAND_DISPLAY") else "fail", None)
    binaries = {path.name: os.access(path, os.X_OK) for path in (paths.NATIVE_DAEMON, paths.MICROHOST_EXE, paths.EYE_BINARY, paths.RUN_MICROHOST, paths.LAUNCH_APP)}
    binaries["libagentseat-atk-dispatch.so"] = (paths.ROOT / "lib/libagentseat-atk-dispatch.so").is_file()
    missing = sorted(name for name, present in binaries.items() if not present)
    check("runtime_files", "fail" if missing else "ok", {"root": str(paths.ROOT), "missing": missing})
    check("xwayland", "ok" if Path("/usr/bin/Xwayland").is_file() else "warn",
          None if Path("/usr/bin/Xwayland").is_file() else "--x11 and --ime fcitx5 are unavailable")
    atspi = python_module_available("import gi; gi.require_version('Atspi', '2.0'); from gi.repository import Atspi")
    check("a11y_dependencies", "ok" if atspi else "warn", None if atspi else "--a11y needs Python GObject and AT-SPI2")
    modules = {name: os.environ.get(name) for name in ("GTK_IM_MODULE", "QT_IM_MODULE", "XMODIFIERS")}
    check("input_method", "ok", {"modules": modules, "auto_resolves_to": lifecycle.resolve_ime("auto")})

    facts = probe_session()
    if not facts["running"]:
        check("session", "skip", "no AgentSeat session for this instance")
    elif "error" in facts:
        check("session", "fail", facts["error"])
    else:
        heartbeat = facts["heartbeat"]
        daemon = facts["daemon"]
        check("session", "ok", {"session_id": heartbeat["session_id"], "uptime_ms": heartbeat.get("uptime_ms"),
                                "instance": paths.SELECTED_INSTANCE})
        same_version = daemon.get("version") == paths.AGENTSEAT_VERSION
        check("daemon_version", "ok" if same_version else "warn",
              {"controller": paths.AGENTSEAT_VERSION, "daemon": daemon.get("version")})
        windows = facts["windows"]
        unverified = [w["id"] for w in windows if not w.get("pid_verified") or not w.get("leased")]
        output = heartbeat.get("output", {})
        check("application_scope", "warn" if unverified or output.get("truncated") else "ok",
              {"windows": len(windows), "unverified_or_unleased": unverified, "truncated": output.get("truncated")})
        scale = float(output.get("scale") or 0)
        consistent = scale > 0 and all(abs(output.get(f"pixel_{k}", 0) - output.get(k, 0) * scale) <= 1 for k in ("width", "height"))
        check("coordinate_mapping", "ok" if consistent else "warn",
              {"logical": [output.get("width"), output.get("height")],
               "pixels": [output.get("pixel_width"), output.get("pixel_height")], "scale": scale,
               "pointer_space": "normalized-output"})
        check("visual_wait", "ok" if output.get("content_serial") is not None else "warn",
              None if output.get("content_serial") is not None else "microhost predates content serial; wait-change unavailable")
        collaboration = heartbeat.get("collaboration", {})
        writer = heartbeat.get("write_control", {})
        holder = writer.get("controller_id") or None
        writable = collaboration.get("agent_allowed") and not heartbeat.get("seat_paused") and holder in {None, transport.RPC_CONTROLLER_ID}
        check("agent_input", "ok" if writable else "warn",
              {"seat_paused": heartbeat.get("seat_paused"), "human_active": collaboration.get("human_active"),
               "write_lease_holder": writer.get("controller_id") or None})
        ime = facts["launch"].get("ime")
        check("text_lane", "ok", {"ime": ime, "lane": text_lane(ime)})
        if not facts["launch"].get("a11y"):
            check("a11y", "skip", "launch with --a11y for semantic reads and actions")
        elif "a11y_error" in facts:
            check("a11y", "fail", facts["a11y_error"])
        else:
            nodes = facts.get("a11y_nodes", 0)
            note = facts.get("a11y_note") or (
                None if nodes > 1 else "application exposes no controls beyond its root; use visual input")
            check("a11y", "ok" if note is None else "warn",
                  {"scope": facts["a11y"].get("scope"), "sample_nodes": nodes, "note": note})
        if probe_capture:
            started = time.monotonic()
            try:
                frame = transport.rpc("capture.submit")
                while frame["status"] in {"pending", "encoding"} and time.monotonic() - started < 5:
                    time.sleep(0.01)
                    frame = transport.rpc("capture.get", {"frame_id": frame["frame_id"]})
                matches = [frame.get("width"), frame.get("height")] == [output.get("pixel_width"), output.get("pixel_height")]
                check("capture", "ok" if frame["status"] == "finished" and matches else "warn",
                      {"status": frame["status"], "latency_ms": int((time.monotonic() - started) * 1000),
                       "frame": [frame.get("width"), frame.get("height")], "matches_output_pixels": matches})
            except common.AgentSeatError as exc:
                check("capture", "fail", str(exc))
        else:
            check("capture", "skip", "pass --probe-capture to time one private frame")
    worst = {"ok": 0, "skip": 0, "warn": 1, "fail": 2}
    level = max((worst[item["status"]] for item in checks), default=0)
    return {"status": ("ok", "degraded", "failing")[level], "checks": checks,
            "degradations": [f"{item['check']}: {item['detail']}" for item in checks if item["status"] in {"warn", "fail"}]}


def text_lane(ime: str | None) -> str:
    if ime == "simple":
        return "keyboard.type"
    return "keyboard.paste (chunked); type --keys sends key events"


def capabilities() -> dict[str, Any]:
    """Separate implemented, exposed by the application, authorized and available."""
    facts = probe_session()
    live = facts["running"] and "error" not in facts
    heartbeat = facts.get("heartbeat", {})
    windows = facts.get("windows", [])
    collaboration = heartbeat.get("collaboration", {})
    writer = heartbeat.get("write_control", {}).get("controller_id")
    writes_allowed = live and bool(collaboration.get("agent_allowed")) and not heartbeat.get("seat_paused") and \
        writer in {"", None, transport.RPC_CONTROLLER_ID}
    launch = facts.get("launch", {})
    content = heartbeat.get("output", {}).get("content_serial") is not None
    clipboard = bool(facts.get("daemon", {}).get("private_clipboard", {}).get("available"))
    a11y_ready = live and bool(launch.get("a11y")) and "a11y" in facts
    a11y_nodes = facts.get("a11y_nodes", 0) > 1

    def entry(implemented: bool, exposes: bool | None, authorized: bool | None, *,
              missing: str | None = None, hidden: str | None = None, denied: str | None = None) -> dict[str, Any]:
        available = bool(implemented and live and exposes is not False and authorized is not False)
        # Report the check that actually failed, in evaluation order.
        if available:
            reason = None
        elif not implemented:
            reason = missing or "not implemented by this runtime"
        elif not live:
            reason = "no session"
        elif exposes is False:
            reason = hidden
        else:
            reason = denied
        return {"implemented": implemented, "application_exposes": exposes, "session_authorized": authorized,
                "available": available, "reason": reason}

    has_windows = bool(windows) if live else None
    write_reason = "seat paused, human active or another controller holds the write lease"
    a11y_launched = bool(launch.get("a11y")) if live else None
    semantic_exposes = a11y_nodes if a11y_ready else None
    semantic_hidden = facts.get("a11y_error") or (
        "application exposes no accessible controls; use visual input" if a11y_ready else None)
    semantic_denied = "launch with --a11y"
    result = {
        "observe.capture": entry(True, has_windows, True if live else None, hidden="no mapped window"),
        "observe.wait_change": entry(content, True if live else None, True if live else None,
                                     missing="microhost predates content serial"),
        "input.pointer": entry(True, has_windows, writes_allowed if live else None,
                               hidden="no mapped window", denied=write_reason),
        "input.keys": entry(True, has_windows, writes_allowed if live else None,
                            hidden="no mapped window", denied=write_reason),
        "input.text": {**entry(True, has_windows, writes_allowed if live else None,
                               hidden="no mapped window", denied=write_reason),
                       "lane": text_lane(launch.get("ime")) if live else None},
        "input.private_paste": entry(clipboard, has_windows, writes_allowed if live else None,
                                     missing="daemon has no private clipboard", hidden="no mapped window",
                                     denied=write_reason),
        "windows.focus_move_resize_close": {**entry(True, has_windows, writes_allowed if live else None,
                                                    hidden="no mapped window", denied=write_reason),
                                            "verification": "compositor_confirmed"},
        "semantic.read": {**entry(True, semantic_exposes, a11y_launched, hidden=semantic_hidden, denied=semantic_denied),
                          "note": facts.get("a11y_note")},
        "semantic.actions": {**entry(True, semantic_exposes, (a11y_launched and writes_allowed) if live else None,
                                     hidden=semantic_hidden,
                                     denied=semantic_denied if live and not launch.get("a11y") else write_reason),
                             "per_node": "inspect a11y get; each node lists its own actions and interfaces"},
        "semantic.events_wait": entry(True, semantic_exposes, a11y_launched, hidden=semantic_hidden,
                                      denied=semantic_denied),
    }
    return {"session": heartbeat.get("session_id"), "controller_id": transport.RPC_CONTROLLER_ID, "capabilities": result}
