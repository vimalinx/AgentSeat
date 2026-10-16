"""Starting, inspecting and stopping a session and its HUD."""

from __future__ import annotations

import json
import math
import os
import shlex
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from . import actions, common, desktop, i18n, instances, paths, procs, transport
from .i18n import _


def microhost_launch_command(
    launch_properties: str,
    host_scale: float,
    mode: str = "wayland",
    hud_position_file: Path | None = None,
) -> str:
    environment = {
        # This is compositor output scale, not an application zoom override.
        # Render physical pixels while preserving the Hyprland logical size.
        "AGENTSEAT_HOST_SCALE": f"{host_scale:.6g}",
        "AGENTSEAT_APPLICATION_MODE": mode,
        "AGENTSEAT_HUMAN_GRACE_MS": str(common.PREFERENCES["human_priority_grace_ms"]),
        "AGENTSEAT_RUNTIME_DIR": str(paths.RUNTIME),
        "AGENTSEAT_STATE_HOME": str(paths.STATE_HOME),
    }
    if hud_position_file is not None:
        environment["AGENTSEAT_HUD_POSITION_FILE"] = str(hud_position_file)
    assignments = " ".join(
        shlex.quote(f"{name}={value}") for name, value in environment.items()
    )
    return f"{launch_properties} env {assignments} {shlex.quote(str(paths.RUN_MICROHOST))}"


def startup_window() -> dict[str, Any] | None:
    # The first mapped surface may be a splash or an application-owned popup.
    # Wait for the micro-host's real focus decision instead of forcing focus on
    # whichever window was enumerated first.
    candidates = [window for window in transport.rpc("window.list")
                  if window.get("focused") and not window.get("popup")
                  and window.get("mapped") and not window.get("hidden")]
    return candidates[0] if len(candidates) == 1 else None


INPUT_ENVIRONMENT_NAMES = ('GTK_IM_MODULE', 'QT_IM_MODULE', 'QT_IM_MODULES', 'XMODIFIERS', 'XCOMPOSEFILE', 'XCOMPOSECACHE', 'IBUS_ADDRESS', 'IBUS_ADDRESS_FILE', 'IBUS_USE_PORTAL', 'DBUS_SESSION_BUS_ADDRESS', 'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'XKB_DEFAULT_RULES', 'XKB_DEFAULT_MODEL', 'XKB_DEFAULT_LAYOUT', 'XKB_DEFAULT_VARIANT', 'XKB_DEFAULT_OPTIONS')


def capture_input_environment() -> dict[str, str | None]:
    # hyprctl exec inherits the compositor's launch-time environment. Carry
    # this caller's current input settings through the private launch file.
    values = {name: os.environ.get(name) for name in INPUT_ENVIRONMENT_NAMES}
    modules = (values.get("GTK_IM_MODULE"), values.get("QT_IM_MODULE"), values.get("XMODIFIERS"))
    if any(value in {"ibus", "@im=ibus"} for value in modules):
        # GTK accepts IBUS_ADDRESS; Qt's IBus module discovers the original
        # daemon through its address file. Resolve both before changing DISPLAY.
        try:
            result = subprocess.run(["/usr/bin/python3", "-c",
                "import gi,json; gi.require_version('IBus','1.0'); from gi.repository import IBus; "
                "print(json.dumps({'IBUS_ADDRESS':IBus.get_address(),'IBUS_ADDRESS_FILE':IBus.get_socket_path()}))"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=2, check=False)
            endpoints = json.loads(result.stdout) if result.returncode == 0 else {}
            for name in ("IBUS_ADDRESS", "IBUS_ADDRESS_FILE"):
                value = endpoints.get(name)
                if not values.get(name) and isinstance(value, str) and value and len(value) <= 4096:
                    values[name] = value
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
            pass
    return values


def write_launch_spec(
    argv: list[str],
    mode: str,
    cwd: str,
    *,
    private_dbus: bool = False,
    a11y: bool = False,
    ime: str = "simple",
) -> None:
    if mode not in {"wayland", "x11"}:
        raise common.AgentSeatError(_("unsupported display mode: {mode}").format(mode=mode))
    payload = {
        "version": 1,
        "argv": argv,
        "mode": mode,
        "cwd": cwd,
        "private_dbus": private_dbus,
        "input_environment": capture_input_environment(),
    }
    if a11y:
        payload["a11y"] = True
    if ime != "simple":
        payload["ime"] = ime
    common.atomic_write_private_text(
        paths.LAUNCH_SPEC,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
    )


def load_application_env() -> dict[str, str] | None:
    try:
        value = json.loads(paths.APPLICATION_ENV.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    wayland_display = value.get("wayland_display")
    if not isinstance(wayland_display, str) or not wayland_display:
        return None
    display = value.get("display")
    return {
        "wayland_display": wayland_display,
        "display": display if isinstance(display, str) else "",
    }


def legacy_command(preset: str) -> tuple[list[str], str, str | None]:
    if preset == "onlyoffice":
        return ["/usr/bin/onlyoffice-desktopeditors"], "wayland", "onlyoffice-native-wayland"
    raise common.AgentSeatError(_("unknown legacy preset: {preset}").format(preset=preset))


def start_application(
    argv: list[str],
    *,
    mode: str,
    cwd: str,
    host_workspace: int = 1,
    legacy_preset: str | None = None,
    compat_type: str | None = None,
    private_dbus: bool = False,
    a11y: bool = False,
    ime: str = "simple",
) -> dict[str, Any]:
    existing = procs.microhost_pid()
    if existing and paths.CONTROL_SOCKET.exists():
        current = common.load_state()
        current_launch = current.get("launch", {})
        requested_launch = {
            "argv": argv,
            "mode": mode,
            "cwd": cwd,
            "private_dbus": private_dbus,
            "a11y": a11y,
            "ime": ime,
        }
        if legacy_preset and current.get("preset") == legacy_preset:
            return status()
        if all(
            current_launch.get(key, "simple" if key == "ime" else False if key in {"private_dbus", "a11y"} else None) == value
            for key, value in requested_launch.items()
        ):
            return status()
        raise common.AgentSeatError(
            _("AgentSeat is already running another application; stop it before launching a different command")
        )
    if existing:
        raise common.AgentSeatError(_("an unmanaged AgentSeat micro-host is already running; run agentseat stop first"))

    host = desktop.host_instance()
    before = desktop.host_snapshot(host)
    signature = str(host["instance"])
    host_scale = desktop.select_host_output_scale(
        desktop.hypr_json(signature, "workspaces"),
        desktop.hypr_json(signature, "monitors"),
        host_workspace,
        before.get("workspace"),
    )
    launch_properties = desktop.launch_window_properties(host_workspace, before.get("workspace"))
    common.ensure_runtime_storage()
    for stale in (paths.MICROHOST_PID, paths.EYE_PID, paths.APPLICATION_ENV, paths.MICROHOST_READY):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    write_launch_spec(argv, mode, cwd, private_dbus=private_dbus, **({"a11y": True} if a11y else {}), **({"ime": ime} if ime != "simple" else {}))
    common.run([
        "hyprctl", "--instance", str(host["instance"]), "dispatch", "exec",
        microhost_launch_command(launch_properties, host_scale, mode, instances.application_hud_position_file(argv, cwd)),
    ])
    microhost = common.wait_for(procs.microhost_pid, 12, _("AgentSeat micro-host did not appear"))
    outer = common.wait_for(
        lambda: desktop.host_client_for_pid(str(host["instance"]), int(microhost)),
        8,
        _("outer AgentSeat window did not appear"),
    )
    if int(outer.get("workspace", {}).get("id", -1)) != host_workspace:
        raise common.AgentSeatError(
            _("AgentSeat outer window did not map on requested workspace {workspace}").format(
                workspace=host_workspace
            )
        )
    if not paths.NATIVE_DAEMON.is_file() or not os.access(paths.NATIVE_DAEMON, os.X_OK):
        raise common.AgentSeatError(
            _("native AgentSeat daemon is missing or not executable: {path}").format(path=paths.NATIVE_DAEMON)
        )
    app_environment = common.wait_for(
        load_application_env, 8, _("AgentSeat application environment receipt did not appear")
    )
    common.wait_for(paths.MICROHOST_READY.exists, 12, _("application did not map in the AgentSeat micro-host"))
    daemon_log = common.open_private_log(paths.LOG_DIR / "daemon.log")
    env = dict(os.environ)
    env.update({
        "XDG_RUNTIME_DIR": str(paths.USER_RUNTIME_DIR),
        "WAYLAND_DISPLAY": app_environment["wayland_display"],
        "AGENTSEAT_LANGUAGE": i18n.CURRENT_LANGUAGE,
    })
    if i18n.CURRENT_LOCALE_DIR:
        env["AGENTSEAT_LOCALE_DIR"] = str(i18n.CURRENT_LOCALE_DIR)
    session_id = uuid.uuid4().hex
    transport.RPC_SESSION_ID = session_id
    transport.RPC_CONTROL_EPOCH = None
    daemon_argv = [
        str(paths.NATIVE_DAEMON),
        "--socket", str(paths.CONTROL_SOCKET),
        "--activity-socket", str(paths.ACTIVITY_SOCKET),
        "--session-id", session_id,
        "--human-grace-ms", str(common.PREFERENCES["human_priority_grace_ms"]),
        "--workspace", str(host_workspace),
        "--seat-id", "agent:agentseat",
        "--seat-name", "AgentSeat Agent",
        "--app-id", Path(argv[0]).name,
        "--window-title", Path(argv[0]).name,
        "--width", str(int(outer.get("size", [1280, 720])[0])),
        "--height", str(int(outer.get("size", [1280, 720])[1])),
    ]
    if mode == "x11":
        daemon_argv.append("--xwayland")
    daemon = subprocess.Popen(
        daemon_argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=daemon_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    common.wait_for(paths.CONTROL_SOCKET.exists, 8, _("AgentSeat daemon socket did not appear"))
    transport.rpc("seat.create", {"workspace": host_workspace})
    window = common.wait_for(startup_window, 12, _("application window did not appear"))
    transport.rpc("window.lease", {"window_id": str(window["id"])})
    transport.release_write_control()

    after = desktop.host_snapshot(host)
    relation = desktop.host_focus_relation(before, after, str(outer["address"]))
    restored_outer_focus = False
    if relation == "outer-focused" and before.get("window"):
        common.run([
            "hyprctl", "--instance", str(host["instance"]), "dispatch", "focuswindow",
            f"address:{before['window']}",
        ], check=False)
        restored_outer_focus = True
        after = desktop.host_snapshot(host)
    if relation == "outer-focused" and not desktop.same_host_focus(before, after):
        raise common.AgentSeatError(
            _("host focus isolation failed: before={before!r} after={after!r}").format(
                before=before, after=after
            )
        )
    state_value = {
        "instance": paths.SELECTED_INSTANCE or "default",
        "session_id": session_id,
        "launch": {
            "argv": argv,
            "mode": mode,
            "cwd": cwd,
            "private_dbus": private_dbus,
            "a11y": a11y,
            "ime": ime,
        },
        "host_instance": host["instance"],
        "backend": "cage-microhost",
        "microhost_pid": microhost,
        "outer_window": outer["address"],
        "host_workspace": host_workspace,
        "host_output_scale": host_scale,
        "buffer_scale": host_scale,
        "render_scale": host_scale,
        "physical_pixel_layout": True,
        "viewport_logical_size": list(outer.get("size", [1280, 720])),
        "physical_buffer_size": [
            desktop.physical_size_for_host(int(value), host_scale)
            for value in outer.get("size", [1280, 720])
        ],
        "wayland_display": app_environment["wayland_display"],
        "x_display": app_environment["display"],
        "daemon_pid": daemon.pid,
        "window_id": str(window["id"]),
        "host_snapshot": before,
        "host_launch_receipt": {
            "before": before,
            "after": after,
            "relation": relation,
            "restored_outer_focus": restored_outer_focus,
        },
    }
    if legacy_preset:
        state_value["preset"] = legacy_preset
    if compat_type:
        state_value["compat_type"] = compat_type
    common.save_state(state_value)
    hud = start_hud() if common.PREFERENCES["eye_hud"] else {"running": False, "disabled_by_config": True}
    state_value["eye_hud"] = hud
    common.save_state(state_value)
    return status()


def start_safely(*args: Any, **kwargs: Any) -> dict[str, Any]:
    with common.lifecycle_lock():
        preexisting_session = bool(procs.microhost_pid() or procs.agent_daemon_pids())
        try:
            return start_application(*args, **kwargs)
        except Exception as exc:
            if not preexisting_session:
                try:
                    _stop_unlocked()
                except (common.AgentSeatError, OSError, TypeError, ValueError) as cleanup_error:
                    exc.add_note(f"AgentSeat launch cleanup also failed: {cleanup_error}")
            raise


def status() -> dict[str, Any]:
    state_value = common.load_state()
    microhost = procs.microhost_pid()
    live = bool(microhost and paths.CONTROL_SOCKET.exists())
    if live:
        state_value = desktop.refresh_outer_geometry(state_value)
    result: dict[str, Any] = {"protocol_version": 2, "running": live, "state": state_value}
    result["eye_hud"] = {
        "running": procs.eye_pid() is not None,
        "pid": procs.eye_pid(),
        "enabled_by_config": bool(common.PREFERENCES["eye_hud"]),
        "scope": "private-microhost",
        "default_corner": "bottom-right",
    }
    if live:
        windows = actions.sync_window_tree()
        result["seat"] = transport.rpc("seat.status")
        result["windows"] = windows
        result["application_state"] = "windowed" if windows else "background"
        host = desktop.host_instance()
        result["host_now"] = desktop.host_snapshot(host)
        matches_start = desktop.same_host_focus(state_value.get("host_snapshot", {}), result["host_now"])
        result["host_matches_start_snapshot"] = matches_start
        result["host_focus_unchanged"] = matches_start
    return result


def session_heartbeat(expected_session_id: str | None = None) -> dict[str, Any]:
    """Verify one live session without changing its window focus or input state."""
    state_value = common.load_state()
    microhost = procs.microhost_pid()
    if not microhost or not paths.CONTROL_SOCKET.exists():
        raise common.AgentSeatError(_("AgentSeat is not running"))
    state_value = desktop.refresh_outer_geometry(state_value)
    daemon = transport.rpc("session.heartbeat")
    session_id = daemon.get("session_id") if isinstance(daemon, dict) else None
    if not isinstance(session_id, str) or not session_id:
        raise common.AgentSeatError(_("AgentSeat daemon returned an invalid session identity"))
    persisted_session_id = state_value.get("session_id")
    if isinstance(persisted_session_id, str) and persisted_session_id != session_id:
        raise common.AgentSeatError(
            _("AgentSeat runtime state belongs to a different session: expected {expected}, got {actual}").format(
                expected=persisted_session_id,
                actual=session_id,
            )
        )
    if expected_session_id and expected_session_id != session_id:
        raise common.AgentSeatError(
            _("AgentSeat session changed: expected {expected}, got {actual}").format(
                expected=expected_session_id,
                actual=session_id,
            )
        )
    daemon_pid = daemon.get("daemon_pid")
    persisted_daemon_pid = state_value.get("daemon_pid")
    if persisted_daemon_pid is not None and daemon_pid != persisted_daemon_pid:
        raise common.AgentSeatError(_("AgentSeat runtime state does not match the connected daemon"))
    windows = transport.rpc("window.list") if daemon.get("seat_created") else []
    healthy = bool(daemon.get("seat_created"))
    if not healthy:
        raise common.AgentSeatError(_("AgentSeat session has no usable application window"))
    return {
        "healthy": True,
        "session_id": session_id,
        "reconnectable": True,
        "connection": "fresh-unix-socket-per-request",
        "microhost_pid": microhost,
        "application_windows": len(windows),
        "application_state": "windowed" if windows else "background",
        "window_ready": bool(windows),
        "state_matches_daemon": True,
        "daemon": daemon,
    }


def watch_session(*, interval: float, count: int, expected_session_id: str | None = None) -> dict[str, Any]:
    """Continuously reconnect to one session and stream compact JSON heartbeats."""
    if not math.isfinite(interval) or interval < 0.1 or interval > 3600:
        raise common.AgentSeatError(_("watch interval must be between 0.1 and 3600 seconds"))
    if count < 0:
        raise common.AgentSeatError(_("watch count must be zero or a positive integer"))
    sequence = 0
    pinned_session_id = expected_session_id
    last_sample: dict[str, Any] = {}
    while count == 0 or sequence < count:
        heartbeat = session_heartbeat(pinned_session_id)
        if pinned_session_id is None:
            pinned_session_id = heartbeat["session_id"]
        sequence += 1
        daemon = heartbeat["daemon"]
        collaboration = daemon.get("collaboration", {})
        last_sample = {
            "sequence": sequence,
            "observed_unix_ms": time.time_ns() // 1_000_000,
            "healthy": True,
            "session_id": heartbeat["session_id"],
            "uptime_ms": daemon.get("uptime_ms"),
            "request_count": daemon.get("request_count"),
            "daemon_pid": daemon.get("daemon_pid"),
            "microhost_pid": heartbeat["microhost_pid"],
            "application_windows": heartbeat["application_windows"],
            "application_state": heartbeat.get("application_state", "windowed" if heartbeat["application_windows"] else "background"),
            "window_ready": heartbeat.get("window_ready", bool(heartbeat["application_windows"])),
            "seat_paused": daemon.get("seat_paused"),
            "agent_allowed": collaboration.get("agent_allowed"),
            "human_active": collaboration.get("human_active"),
        }
        print(json.dumps(last_sample, ensure_ascii=False, separators=(",", ":")), flush=True)
        if count == 0 or sequence < count:
            time.sleep(interval)
    return last_sample


def start_hud() -> dict[str, Any]:
    existing = procs.eye_pid()
    if existing:
        return {"running": True, "pid": existing, "connected": paths.CONTROL_SOCKET.exists()}
    if not paths.EYE_BINARY.is_file() or not os.access(paths.EYE_BINARY, os.X_OK):
        raise common.AgentSeatError(
            _("native AgentSeat eye HUD is missing or not executable: {path}").format(path=paths.EYE_BINARY)
        )
    state_value = common.load_state()
    if not procs.microhost_pid() or not paths.CONTROL_SOCKET.exists():
        raise common.AgentSeatError(_("AgentSeat must be running before its embedded AI head can start"))
    wayland_display = state_value.get("wayland_display")
    if not isinstance(wayland_display, str) or not wayland_display:
        application_env = load_application_env()
        wayland_display = application_env["wayland_display"] if application_env else None
    if not wayland_display:
        raise common.AgentSeatError(_("AgentSeat private Wayland display is unavailable"))
    environment = dict(os.environ)
    environment.update({
        "XDG_RUNTIME_DIR": str(paths.USER_RUNTIME_DIR),
        "WAYLAND_DISPLAY": wayland_display,
        "GDK_BACKEND": "wayland",
        # The HUD is vector text plus one tiny Cairo head. Inheriting the host's
        # GL renderer loads a full software-GL stack inside the pixman
        # micro-host for no visual benefit.
        "GSK_RENDERER": "cairo",
        "AGENTSEAT_SESSION_ID": str(state_value.get("session_id", "")),
        "AGENTSEAT_LANGUAGE": i18n.CURRENT_LANGUAGE,
        # Only the owned HUD needs logical sizing in the physical X11 scene.
        "AGENTSEAT_HUD_SCALE": str(state_value.get("host_output_scale", 1.0)
            if state_value.get("launch", {}).get("mode") == "x11" else 1.0),
    })
    if i18n.CURRENT_LOCALE_DIR:
        environment["AGENTSEAT_LOCALE_DIR"] = str(i18n.CURRENT_LOCALE_DIR)
    environment.pop("DISPLAY", None)
    environment.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
    eye_log = common.open_private_log(paths.LOG_DIR / "eye.log")
    eye = subprocess.Popen(
        [str(paths.EYE_BINARY), "--socket", str(paths.CONTROL_SOCKET), "--embedded"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=eye_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    paths.EYE_PID.write_text(f"{eye.pid}\n", encoding="utf-8")
    os.chmod(paths.EYE_PID, 0o600)
    time.sleep(0.35)
    if eye.poll() is not None:
        raise common.AgentSeatError(
            _("AgentSeat eye HUD exited during startup; inspect {path}").format(path=paths.LOG_DIR / "eye.log")
        )
    return {
        "running": True,
        "pid": eye.pid,
        "connected": paths.CONTROL_SOCKET.exists(),
        "enabled_by_config": bool(common.PREFERENCES["eye_hud"]),
        "scope": "private-microhost",
        "default_corner": "bottom-right",
    }


def stop_hud() -> dict[str, Any]:
    hud_pid = procs.eye_pid()
    if hud_pid:
        try:
            os.kill(hud_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        common.wait_for(lambda: procs.eye_pid() is None, 5, _("AgentSeat eye HUD did not stop"))
    try:
        paths.EYE_PID.unlink()
    except FileNotFoundError:
        pass
    return {"running": False}


def _stop_unlocked() -> dict[str, Any]:
    state_value = common.load_state()
    # Keep the verified PID before asking the daemon to shut down. The daemon or
    # application can remove the runtime PID file before the outer micro-host has
    # actually exited; re-reading only the file would then orphan the host window.
    owned_microhost = procs.microhost_pid()
    if not owned_microhost and state_value.get("microhost_pid"):
        persisted_microhost = int(state_value["microhost_pid"])
        if procs.microhost_belongs_to_runtime(persisted_microhost):
            owned_microhost = persisted_microhost
    stop_hud()
    daemon_pids = set(procs.agent_daemon_pids())
    if state_value.get("daemon_pid"):
        persisted_daemon_pid = int(state_value["daemon_pid"])
        if procs.daemon_belongs_to_runtime(persisted_daemon_pid):
            daemon_pids.add(persisted_daemon_pid)
    if paths.CONTROL_SOCKET.exists():
        try:
            transport.rpc("shutdown")
        except common.AgentSeatError:
            pass
    if daemon_pids:
        common.wait_for(
            lambda: all(not Path(f"/proc/{pid}").exists() for pid in daemon_pids),
            5,
            _("daemon did not stop"),
        )
    for socket_path in (paths.CONTROL_SOCKET, paths.ACTIVITY_SOCKET):
        if socket_path.exists():
            socket_stat = socket_path.lstat()
            if not stat.S_ISSOCK(socket_stat.st_mode) or socket_stat.st_uid != os.getuid():
                raise common.AgentSeatError(
                    _("refusing to remove an unexpected AgentSeat socket: {path}").format(path=socket_path)
                )
            socket_path.unlink()
    if owned_microhost and procs.microhost_belongs_to_runtime(owned_microhost):
        try:
            os.kill(owned_microhost, signal.SIGTERM)
        except ProcessLookupError:
            pass
        common.wait_for(
            lambda: not procs.microhost_belongs_to_runtime(owned_microhost),
            8,
            _("AgentSeat micro-host did not stop"),
        )
    try:
        paths.STATE.unlink()
    except FileNotFoundError:
        pass
    for transient in (paths.LAUNCH_SPEC, paths.APPLICATION_ENV, paths.MICROHOST_PID, paths.MICROHOST_READY, paths.EYE_PID):
        try:
            transient.unlink()
        except FileNotFoundError:
            pass
    return {"stopped": True}


def stop() -> dict[str, Any]:
    with common.lifecycle_lock():
        return _stop_unlocked()


def resolve_ime(choice: str, private_dbus: bool = False) -> str:
    if choice == "desktop" and private_dbus:
        raise common.AgentSeatError("--ime desktop requires the desktop session bus; use --ime fcitx5 with --private-dbus")
    if choice != "auto":
        return choice
    # Attach only a service selected by the caller, including mixed toolkit
    # choices. A running daemon alone is not the user's engine preference.
    modules = [os.environ.get(name, "") for name in ("GTK_IM_MODULE", "QT_IM_MODULE", "XMODIFIERS")]
    modules.extend(os.environ.get("QT_IM_MODULES", "").split(";"))
    if not any(value in {"fcitx", "fcitx5", "@im=fcitx"} for value in modules):
        return "inherit"
    remote = Path("/usr/bin/fcitx5-remote")
    if not remote.is_file() or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return "inherit"
    try:
        probe = subprocess.run([str(remote), "--check"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "inherit"
    return ("fcitx5" if private_dbus else "desktop") if probe.returncode == 0 else "inherit"
