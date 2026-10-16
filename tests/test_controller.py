from __future__ import annotations

import importlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / "bin/agentseat"
CONTROLLER_PACKAGE = ROOT / "lib/agentseat_cli"
LAUNCHER_PATH = ROOT / "bin/launch-app"
EYE_SOURCE_PATH = ROOT / "native/agentseat-eye.c"
MICROHOST_SOURCE_PATH = ROOT / "vendor/cage/cage.c"
OUTPUT_SOURCE_PATH = ROOT / "vendor/cage/output.c"
VIEW_SOURCE_PATH = ROOT / "vendor/cage/view.c"
XDG_SHELL_SOURCE_PATH = ROOT / "vendor/cage/xdg_shell.c"
BUILD_TRANSLATIONS = ROOT / "scripts/build-translations.sh"
CHECK_TRANSLATIONS = ROOT / "scripts/check-translations.sh"


def load_controller() -> Any:
    """Import a fresh controller package so path constants follow this test's env."""
    for name in [name for name in sys.modules if name == "agentseat_cli" or name.startswith("agentseat_cli.")]:
        del sys.modules[name]
    if str(CONTROLLER_PACKAGE.parent) not in sys.path:
        sys.path.insert(0, str(CONTROLLER_PACKAGE.parent))
    return importlib.import_module("agentseat_cli").Controller()


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    # Keep contract tests independent of any live AgentSeat session.
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("AGENTSEAT_RUNTIME_DIR", str(runtime))
    return load_controller()


def test_normalize_argv_requires_a_real_command(controller: ModuleType) -> None:
    assert controller.normalize_argv(["--", "/usr/bin/zenity", "--info", "--text=hello world"]) == [
        "/usr/bin/zenity",
        "--info",
        "--text=hello world",
    ]
    with pytest.raises(controller.AgentSeatError):
        controller.normalize_argv(["--"])
    with pytest.raises(controller.AgentSeatError):
        controller.normalize_argv(["bad\0argument"])


def test_startup_waits_for_the_real_focused_window(controller, monkeypatch):
    splash = {'id':'w-1','mapped':True,'hidden':False,'popup':False,'focused':False}
    popup = {**splash,'id':'w-2','popup':True,'focused':True}
    main = {**splash,'id':'w-3','focused':True}
    windows = [splash, popup]
    calls = []
    def rpc(method):
        calls.append(method)
        return windows
    monkeypatch.setattr(controller,'rpc',rpc)
    assert controller.startup_window() is None
    windows[:] = [splash, main]
    assert controller.startup_window() == main
    assert calls == ['window.list','window.list']


def test_default_cwd_is_the_callers_current_directory(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    assert controller.normalized_cwd(None) == str(tmp_path)


def test_microhost_pid_requires_the_matching_runtime(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller.RUNTIME = tmp_path / "runtime-a"
    controller.MICROHOST_PID = tmp_path / "microhost.pid"
    controller.MICROHOST_PID.write_text("33\n", encoding="utf-8")
    monkeypatch.setattr(controller, "proc_exe", lambda pid: str(controller.MICROHOST_EXE))
    monkeypatch.setattr(
        controller,
        "proc_environment_value",
        lambda pid, name: str(tmp_path / "runtime-b"),
    )

    assert controller.microhost_pid() is None

    monkeypatch.setattr(
        controller,
        "proc_environment_value",
        lambda pid, name: str(tmp_path / "runtime-a"),
    )
    assert controller.microhost_pid() == 33


def test_start_failure_never_stops_a_preexisting_session(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller.RUNTIME = tmp_path
    controller.LIFECYCLE_LOCK = tmp_path / "lifecycle.lock"
    monkeypatch.setattr(controller, "microhost_pid", lambda: 33)
    monkeypatch.setattr(controller, "agent_daemon_pids", list)
    monkeypatch.setattr(
        controller,
        "start_application",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(controller.AgentSeatError("already running")),
    )
    cleaned: list[bool] = []
    monkeypatch.setattr(controller, "_stop_unlocked", lambda: cleaned.append(True))

    with pytest.raises(controller.AgentSeatError, match="already running"):
        controller.start_safely(["/usr/bin/other"], mode="wayland", cwd=str(tmp_path))

    assert cleaned == []


def test_start_failure_cleans_only_resources_created_by_that_attempt(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller.RUNTIME = tmp_path
    controller.LIFECYCLE_LOCK = tmp_path / "lifecycle.lock"
    monkeypatch.setattr(controller, "microhost_pid", lambda: None)
    monkeypatch.setattr(controller, "agent_daemon_pids", list)
    monkeypatch.setattr(
        controller,
        "start_application",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(controller.AgentSeatError("launch failed")),
    )
    cleaned: list[bool] = []
    monkeypatch.setattr(controller, "_stop_unlocked", lambda: cleaned.append(True) or {"stopped": True})

    with pytest.raises(controller.AgentSeatError, match="launch failed"):
        controller.start_safely(["/usr/bin/broken"], mode="wayland", cwd=str(tmp_path))

    assert cleaned == [True]


def test_lifecycle_lock_rejects_symlinks(
    controller: ModuleType, tmp_path: Path
) -> None:
    target = tmp_path / "unrelated"
    target.write_text("keep", encoding="utf-8")
    controller.RUNTIME = tmp_path
    controller.LIFECYCLE_LOCK = tmp_path / "lifecycle.lock"
    controller.LIFECYCLE_LOCK.symlink_to(target)

    with pytest.raises(controller.AgentSeatError, match="lifecycle lock"), controller.lifecycle_lock():
        pytest.fail("symlinked lifecycle lock was accepted")

    assert target.read_text(encoding="utf-8") == "keep"


def test_paths_follow_xdg_and_keep_chat_ephemeral(tmp_path: Path) -> None:
    environment = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    result = subprocess.run(
        [str(CONTROLLER_PATH), "paths"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    paths = json.loads(result.stdout)

    assert paths["config"] == str(tmp_path / "config/agentseat")
    assert paths["data"] == str(tmp_path / "data/agentseat")
    assert paths["state"] == str(tmp_path / "state/agentseat")
    assert paths["captures"] == str(tmp_path / "state/agentseat/captures")
    assert paths["cache"] == str(tmp_path / "cache/agentseat")
    assert paths["runtime"] == str(tmp_path / "runtime/agentseat")
    assert paths["privacy"]["chat"].startswith("memory-only")
    assert not (tmp_path / "runtime/agentseat").exists()


def test_named_instance_reexec_selects_isolated_runtime_and_state(tmp_path: Path) -> None:
    environment = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
    }
    for name in ("AGENTSEAT_INSTANCE", "AGENTSEAT_RUNTIME_DIR", "AGENTSEAT_STATE_HOME"):
        environment.pop(name, None)

    result = subprocess.run(
        [str(CONTROLLER_PATH), "--instance", "mousepad-a1b2c3d4", "paths"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    paths = json.loads(result.stdout)
    directory_name = "app-e41c4811025b7120bf7d6dfb"

    assert paths["instance"] == "mousepad-a1b2c3d4"
    assert paths["runtime"] == str(tmp_path / f"runtime/agentseat-apps/{directory_name}")
    assert paths["state"] == str(tmp_path / f"state/agentseat/apps/{directory_name}")
    assert not (tmp_path / f"runtime/agentseat-apps/{directory_name}").exists()


def test_instance_identifiers_and_auto_allocation_are_bounded(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(controller, "INSTANCE_RUNTIME_ROOT", tmp_path / "runtime")
    monkeypatch.setattr(controller, "INSTANCE_STATE_ROOT", tmp_path / "state")

    assert controller.normalize_instance_id("code-a1b2c3d4") == "code-a1b2c3d4"
    with pytest.raises(Exception, match="1-48"):
        controller.normalize_instance_id("../escape")
    instance_id = controller.allocate_instance_id(["/usr/bin/Very Fancy GUI!"])
    assert instance_id.startswith("very-fancy-gui-")
    assert len(instance_id) <= 33
    wrapped_id = controller.allocate_instance_id([
        "/usr/bin/env",
        "XDG_CONFIG_HOME=/tmp/private-config",
        "/usr/bin/kdenlive",
        "--no-welcome",
    ])
    assert wrapped_id.startswith("kdenlive-")


def test_stop_keeps_verified_microhost_pid_after_runtime_pid_file_disappears(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller.RUNTIME = tmp_path
    controller.LIFECYCLE_LOCK = tmp_path / "lifecycle.lock"
    controller.CONTROL_SOCKET = tmp_path / "missing-control.sock"
    controller.ACTIVITY_SOCKET = tmp_path / "missing-activity.sock"
    controller.STATE = tmp_path / "state.json"
    controller.LAUNCH_SPEC = tmp_path / "launch.json"
    controller.APPLICATION_ENV = tmp_path / "application-env.json"
    controller.MICROHOST_PID = tmp_path / "microhost.pid"
    controller.MICROHOST_READY = tmp_path / "microhost.ready"
    controller.EYE_PID = tmp_path / "eye.pid"
    monkeypatch.setattr(controller, "load_state", dict)
    monkeypatch.setattr(controller, "agent_daemon_pids", list)

    pid_file_present = True
    monkeypatch.setattr(controller, "microhost_pid", lambda: 33 if pid_file_present else None)

    def fake_stop_hud() -> dict[str, bool]:
        nonlocal pid_file_present
        pid_file_present = False
        return {"running": False}

    monkeypatch.setattr(controller, "stop_hud", fake_stop_hud)
    alive = {33}
    monkeypatch.setattr(
        controller,
        "proc_exe",
        lambda pid: str(controller.MICROHOST_EXE) if pid in alive else "",
    )
    monkeypatch.setattr(controller, "microhost_belongs_to_runtime", lambda pid: pid in alive)
    signals: list[tuple[int, int]] = []

    def fake_kill(pid: int, sent_signal: int) -> None:
        signals.append((pid, sent_signal))
        alive.discard(pid)

    monkeypatch.setattr(controller.os, "kill", fake_kill)

    assert controller.stop() == {"stopped": True}
    assert signals == [(33, controller.signal.SIGTERM)]


def test_stop_uses_verified_state_pid_when_runtime_pid_file_is_already_missing(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller.RUNTIME = tmp_path
    controller.LIFECYCLE_LOCK = tmp_path / "lifecycle.lock"
    controller.CONTROL_SOCKET = tmp_path / "missing-control.sock"
    controller.ACTIVITY_SOCKET = tmp_path / "missing-activity.sock"
    controller.STATE = tmp_path / "state.json"
    controller.LAUNCH_SPEC = tmp_path / "launch.json"
    controller.APPLICATION_ENV = tmp_path / "application-env.json"
    controller.MICROHOST_PID = tmp_path / "missing-microhost.pid"
    controller.MICROHOST_READY = tmp_path / "microhost.ready"
    controller.EYE_PID = tmp_path / "eye.pid"
    monkeypatch.setattr(controller, "load_state", lambda: {"microhost_pid": 44})
    monkeypatch.setattr(controller, "microhost_pid", lambda: None)
    monkeypatch.setattr(controller, "stop_hud", lambda: {"running": False})
    monkeypatch.setattr(controller, "agent_daemon_pids", list)

    alive = {44}
    monkeypatch.setattr(
        controller,
        "proc_exe",
        lambda pid: str(controller.MICROHOST_EXE) if pid in alive else "",
    )
    monkeypatch.setattr(controller, "microhost_belongs_to_runtime", lambda pid: pid in alive)
    signals: list[tuple[int, int]] = []

    def fake_kill(pid: int, sent_signal: int) -> None:
        signals.append((pid, sent_signal))
        alive.discard(pid)

    monkeypatch.setattr(controller.os, "kill", fake_kill)

    assert controller.stop() == {"stopped": True}
    assert signals == [(44, controller.signal.SIGTERM)]


def test_instances_lists_only_valid_namespaced_sessions(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_root = tmp_path / "instances"
    instance_id = "mousepad-a1b2c3d4"
    valid = runtime_root / controller.instance_directory_name(instance_id)
    invalid = runtime_root / "invalid instance"
    valid.mkdir(parents=True)
    invalid.mkdir()
    (valid / "state.json").write_text(
        json.dumps({
            "instance": instance_id,
            "session_id": "session-a",
            "host_workspace": 8,
            "launch": {"argv": ["/usr/bin/mousepad"], "mode": "wayland"},
        }),
        encoding="utf-8",
    )
    (valid / "microhost.pid").write_text("123\n", encoding="utf-8")
    (valid / "control.sock").touch()
    (invalid / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(controller, "INSTANCE_RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(controller, "microhost_belongs_to_runtime", lambda pid, runtime=None: True)

    result = controller.list_instances()

    assert result == {
        "count": 1,
        "instances": [{
            "instance": "mousepad-a1b2c3d4",
            "running": True,
            "session_id": "session-a",
            "host_workspace": 8,
            "microhost_pid": 123,
            "launch": {"argv": ["/usr/bin/mousepad"], "mode": "wayland"},
        }],
    }


def test_daemon_ownership_is_scoped_to_the_selected_control_socket(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller.CONTROL_SOCKET = tmp_path / "app-a/control.sock"
    monkeypatch.setattr(controller, "proc_exe", lambda pid: str(controller.NATIVE_DAEMON))
    argv_by_pid = {
        10: [str(controller.NATIVE_DAEMON), "--socket", str(controller.CONTROL_SOCKET)],
        11: [str(controller.NATIVE_DAEMON), "--socket", str(tmp_path / "app-b/control.sock")],
    }
    monkeypatch.setattr(controller, "proc_argv", lambda pid: argv_by_pid[pid])

    assert controller.daemon_belongs_to_runtime(10) is True
    assert controller.daemon_belongs_to_runtime(11) is False


def test_config_reads_validated_xdg_preferences(tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    config_dir = config_home / "agentseat"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        "[general]\ndefault_workspace = 8\nhuman_priority_grace_ms = 900\neye_hud = false\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(CONTROLLER_PATH), "config"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "XDG_CONFIG_HOME": str(config_home)},
    )
    config = json.loads(result.stdout)

    assert config["exists"] is True
    assert config["effective"] == {
        "default_workspace": 8,
        "human_priority_grace_ms": 900,
        "eye_hud": False,
        "language": "auto",
    }


def test_config_keeps_collaboration_head_hidden_by_default(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(CONTROLLER_PATH), "config"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "missing-config")},
    )
    config = json.loads(result.stdout)

    assert config["exists"] is False
    assert config["effective"]["eye_hud"] is False
    assert config["effective"]["language"] == "auto"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("en_US.UTF-8", "en"),
        ("zh_CN.UTF-8", "zh_CN"),
        ("zh-Hans", "zh_CN"),
        ("zh_TW.UTF-8", "zh_TW"),
        ("zh-Hant", "zh_TW"),
        ("fr_FR.UTF-8", "en"),
    ],
)
def test_language_normalization_is_stable(
    controller: ModuleType, value: str, expected: str
) -> None:
    assert controller.normalize_language(value) == expected


def test_cli_and_config_select_complete_locales(tmp_path: Path) -> None:
    subprocess.run([str(CHECK_TRANSLATIONS)], check=True, capture_output=True, text=True)
    subprocess.run([str(BUILD_TRANSLATIONS)], check=True, capture_output=True, text=True)
    locale_dir = ROOT / "build/locale"

    expected_help = {
        "en": "wrap an arbitrary application command",
        "zh-CN": "封装任意应用命令",
        "zh-TW": "封裝任意應用程式命令",
    }
    for language, marker in expected_help.items():
        result = subprocess.run(
            [str(CONTROLLER_PATH), "--language", language, "--help"],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "AGENTSEAT_LOCALE_DIR": str(locale_dir)},
        )
        assert marker in result.stdout

    config_dir = tmp_path / "agentseat"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '[general]\nlanguage = "zh-CN"\n', encoding="utf-8"
    )
    environment = {
        **os.environ,
        "AGENTSEAT_CONFIG_HOME": str(config_dir),
        "AGENTSEAT_LOCALE_DIR": str(locale_dir),
    }
    environment.pop("AGENTSEAT_LANGUAGE", None)
    result = subprocess.run(
        [str(CONTROLLER_PATH), "config"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    config = json.loads(result.stdout)
    assert config["effective"]["language"] == "zh-CN"
    assert config["resolved_language"] == "zh-CN"


def test_config_rejects_unsafe_values(tmp_path: Path) -> None:
    config_dir = tmp_path / "agentseat"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "[general]\ndefault_workspace = 0\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(CONTROLLER_PATH), "config"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path)},
    )

    assert result.returncode == 1
    assert "positive integer" in result.stderr


def test_host_focus_relation_never_mistakes_human_activity_for_wrapper_focus(controller: ModuleType) -> None:
    before = {"workspace": 1, "window": "human-a", "title": "A"}
    assert controller.host_focus_relation(before, dict(before), "agentseat") == "unchanged"
    assert (
        controller.host_focus_relation(before, {"workspace": 1, "window": "agentseat", "title": "AgentSeat"}, "agentseat")
        == "outer-focused"
    )
    assert (
        controller.host_focus_relation(before, {"workspace": 2, "window": "human-b", "title": "B"}, "agentseat")
        == "concurrent-host-change"
    )


def test_launch_window_properties_places_only_inactive_target_workspace(controller: ModuleType) -> None:
    focus_guards = "no_initial_focus true; focus_on_activate false; suppress_event activate activatefocus"
    assert controller.launch_window_properties(2, 2) == f"[{focus_guards}]"
    assert controller.launch_window_properties(8, 2) == f"[workspace 8; {focus_guards}]"
    with pytest.raises(controller.AgentSeatError, match="positive integer"):
        controller.launch_window_properties(0, 2)


def test_launch_workspace_defaults_to_the_current_host_workspace(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller, "host_instance", lambda: {"instance": "host"})
    monkeypatch.setattr(controller, "host_snapshot", lambda host: {"workspace": 12})

    assert controller.launch_workspace("current") == 12
    assert controller.launch_workspace("28") == 28
    with pytest.raises(controller.AgentSeatError, match="current.*positive"):
        controller.launch_workspace("zero")


def test_microhost_launch_command_preserves_namespace_and_passes_compositor_scale(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime with spaces"
    state_home = tmp_path / "state with spaces"
    monkeypatch.setattr(controller, "RUNTIME", runtime)
    monkeypatch.setattr(controller, "STATE_HOME", state_home)

    command = controller.microhost_launch_command("[workspace 8]", 1.6)

    assert "AGENTSEAT_INSTANCE" not in command
    assert "AGENTSEAT_PHYSICAL_LAYOUT" not in command
    assert command == (
        "[workspace 8] env AGENTSEAT_HOST_SCALE=1.6 AGENTSEAT_APPLICATION_MODE=wayland AGENTSEAT_HUMAN_GRACE_MS=1500 "
        f"'AGENTSEAT_RUNTIME_DIR={runtime}' 'AGENTSEAT_STATE_HOME={state_home}' "
        f"{controller.RUN_MICROHOST}"
    )


def test_target_monitor_scale_drives_native_physical_buffer(controller: ModuleType) -> None:
    workspaces = [
        {"id": 8, "monitorID": 0, "monitor": "eDP-1"},
        {"id": 11, "monitorID": 1, "monitor": "DP-1"},
    ]
    monitors = [
        {"id": 0, "name": "eDP-1", "scale": 1.6, "focused": True, "activeWorkspace": {"id": 2}},
        {"id": 1, "name": "DP-1", "scale": 1.0, "focused": False, "activeWorkspace": {"id": 11}},
    ]

    assert controller.select_host_output_scale(workspaces, monitors, 8, 2) == 1.6
    assert controller.select_host_output_scale(workspaces, monitors, 11, 2) == 1.0
    assert controller.select_host_output_scale([], monitors, 99, 2) == 1.6
    assert controller.physical_size_for_host(1592, 1.0) == 1592
    assert controller.physical_size_for_host(1592, 1.6) == 2548
    assert controller.physical_size_for_host(960, 1.6) == 1536


def test_onlyoffice_preset_prefers_the_native_wayland_lane(controller: ModuleType) -> None:
    assert controller.legacy_command("onlyoffice") == (
        ["/usr/bin/onlyoffice-desktopeditors"],
        "wayland",
        "onlyoffice-native-wayland",
    )


def test_keyboard_key_stays_on_the_private_rpc_seat(controller: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(controller, "sync_window_tree", list)
    monkeypatch.setattr(
        controller,
        "rpc",
        lambda method, params=None: calls.append((method, params)) or {"delivered": True},
    )

    assert controller.keyboard_key(29, True) == {"delivered": True}
    assert calls == [("keyboard.key", {"keycode": 29, "pressed": True})]


def test_write_launch_spec_is_atomic_private_and_generic(controller: ModuleType, tmp_path: Path) -> None:
    controller.LAUNCH_SPEC = tmp_path / "launch.json"
    controller.write_launch_spec(["/usr/bin/zenity", "--info", "--text=generic"], "wayland", str(tmp_path))

    spec = json.loads(controller.LAUNCH_SPEC.read_text(encoding="utf-8"))
    assert spec.pop("input_environment") == controller.capture_input_environment()
    assert spec == {
        "version": 1,
        "argv": ["/usr/bin/zenity", "--info", "--text=generic"],
        "mode": "wayland",
        "cwd": str(tmp_path),
        "private_dbus": False,
    }
    assert stat.S_IMODE(controller.LAUNCH_SPEC.stat().st_mode) == 0o600
    assert not controller.LAUNCH_SPEC.with_suffix(".tmp").exists()


def test_outer_geometry_refresh_reads_compositor_without_host_writes(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller, "host_client_for_pid", lambda *args: pytest.fail("queried host"))
    monkeypatch.setattr(controller, "save_state", lambda value: pytest.fail("wrote state"))
    calls = []
    def rpc(method, params=None):
        calls.append(method)
        return {"output": {"width": 1592, "height": 960, "pixel_width": 2548,
                           "pixel_height": 1536, "scale": 1.6, "geometry_version": 7}}
    monkeypatch.setattr(controller, "rpc", rpc)
    state = controller.refresh_outer_geometry({"viewport_logical_size": [400, 200]})
    assert state["viewport_logical_size"] == [1592, 960]
    assert state["physical_buffer_size"] == [2548, 1536]
    assert state["geometry_version"] == 7
    assert calls == ["session.heartbeat"]


def test_outer_geometry_refresh_keeps_preupgrade_geometry(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller, "rpc", lambda method: {})
    original = {"viewport_logical_size": [400, 300], "physical_buffer_size": [400, 300]}
    assert controller.refresh_outer_geometry(original) == original


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("wayland", {"wayland": "wayland-test", "gdk": "wayland", "display": ":42"}),
        ("x11", {"wayland": None, "gdk": "x11", "display": ":42"}),
    ],
)
def test_launcher_executes_arbitrary_argv_with_generic_display_mode(
    tmp_path: Path,
    mode: str,
    expected: dict[str, str | None],
) -> None:
    spec_path = tmp_path / "launch.json"
    probe = (
        "import json,os; print(json.dumps({'wayland':os.environ.get('WAYLAND_DISPLAY'),"
        "'gdk':os.environ.get('GDK_BACKEND'),'display':os.environ.get('DISPLAY')}))"
    )
    spec_path.write_text(
        json.dumps(
            {
                "version": 1,
                "argv": [os.fspath(Path(os.sys.executable)), "-c", probe],
                "mode": mode,
                "cwd": str(tmp_path),
                "x_display": ":42",
            }
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "AGENTSEAT_LAUNCH_SPEC": str(spec_path),
        "AGENTSEAT_ENV_RECEIPT": str(tmp_path / "application-env.json"),
        "AGENTSEAT_SKIP_X11_READY": "1",
        "WAYLAND_DISPLAY": "wayland-test",
    }

    result = subprocess.run(
        [str(LAUNCHER_PATH)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert json.loads(result.stdout) == expected
    assert not spec_path.exists()


@pytest.mark.parametrize("mode", ["wayland", "x11"])
def test_launcher_preserves_caller_scale_environment(tmp_path: Path, mode: str) -> None:
    spec_path = tmp_path / "launch.json"
    probe = (
        "import json,os; print(json.dumps({'scale':os.environ.get('QT_SCALE_FACTOR'),"
        "'auto':os.environ.get('QT_AUTO_SCREEN_SCALE_FACTOR'),'dpi':os.environ.get('QT_FONT_DPI'),"
        "'gdk':os.environ.get('GDK_SCALE'),'gdk_dpi':os.environ.get('GDK_DPI_SCALE')}))"
    )
    spec_path.write_text(
        json.dumps(
            {
                "version": 1,
                "argv": [os.fspath(Path(os.sys.executable)), "-c", probe],
                "mode": mode,
                "cwd": str(tmp_path),
                "x_display": ":42",
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(LAUNCHER_PATH)],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AGENTSEAT_LAUNCH_SPEC": str(spec_path),
            "AGENTSEAT_ENV_RECEIPT": str(tmp_path / "application-env.json"),
            "AGENTSEAT_PHYSICAL_LAYOUT": "1",
            "AGENTSEAT_HOST_SCALE": "1.6",
            "AGENTSEAT_SKIP_X11_READY": "1",
            "QT_SCALE_FACTOR": "1.25",
            "QT_AUTO_SCREEN_SCALE_FACTOR": "0",
            "QT_FONT_DPI": "120",
            "GDK_SCALE": "2",
            "GDK_DPI_SCALE": "0.75",
            "WAYLAND_DISPLAY": "wayland-test",
        },
    )
    assert json.loads(result.stdout) == {
        "scale": "1.25",
        "auto": "0",
        "dpi": "120",
        "gdk": "2",
        "gdk_dpi": "0.75",
    }


def test_launcher_can_create_a_private_dbus_session(tmp_path: Path) -> None:
    spec_path = tmp_path / "launch.json"
    probe = "import os; print(os.environ.get('DBUS_SESSION_BUS_ADDRESS', ''))"
    spec_path.write_text(
        json.dumps({
            "version": 1,
            "argv": [os.fspath(Path(os.sys.executable)), "-c", probe],
            "mode": "wayland",
            "cwd": str(tmp_path),
            "private_dbus": True,
        }),
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(LAUNCHER_PATH)],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "AGENTSEAT_LAUNCH_SPEC": str(spec_path),
            "AGENTSEAT_ENV_RECEIPT": str(tmp_path / "application-env.json"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/invalid-host-bus",
            "WAYLAND_DISPLAY": "wayland-test",
        },
    )

    assert result.stdout.strip().startswith("unix:")
    assert result.stdout.strip() != "unix:path=/invalid-host-bus"


def test_microhost_uses_native_hidpi_without_app_zoom_and_keeps_frame_fallback() -> None:
    cage_source = MICROHOST_SOURCE_PATH.read_text(encoding="utf-8")
    output_source = OUTPUT_SOURCE_PATH.read_text(encoding="utf-8")
    view_source = VIEW_SOURCE_PATH.read_text(encoding="utf-8")
    xdg_shell_source = XDG_SHELL_SOURCE_PATH.read_text(encoding="utf-8")
    launcher_source = LAUNCHER_PATH.read_text(encoding="utf-8")

    assert "wlr_fractional_scale_manager_v1_create" in cage_source
    assert "wlr_output_state_set_scale(&state, application_output_scale(scale));" in output_source
    assert "wp_viewport_set_destination" in output_source
    assert "AGENTSEAT_PHYSICAL_LAYOUT" not in output_source
    assert "Xft.dpi" not in launcher_source
    controller_source = "".join(path.read_text(encoding="utf-8") for path in CONTROLLER_PACKAGE.glob("*.py"))
    assert "resizewindowpixel" not in controller_source
    assert '"float true"' not in controller_source
    assert "view->impl->configure(view, layout_box.width, layout_box.height, true);" in view_source
    assert "view->server->primary_view = view;" in xdg_shell_source
    assert "wlr_xdg_toplevel_set_fullscreen(xdg_shell_view->xdg_toplevel, fill_output);" in xdg_shell_source
    assert "wlr_scene_output_needs_frame(output->scene_output)" in output_source
    assert "wlr_output->events.needs_frame" in output_source
    assert "AGENTSEAT_FALLBACK_FRAME_MS 17" in output_source


def test_hud_cursor_stays_native_size_after_gtk_text_override() -> None:
    source = EYE_SOURCE_PATH.read_text(encoding="utf-8")

    assert "#define PANEL_HEIGHT ((int)lround(232 * app_state.ui_scale))" in source
    assert "#define HUD_CURSOR_SIZE 20" in source
    assert "gdk_memory_texture_new(" in source
    assert "gtk_widget_set_cursor(app_state.stack, app_state.arrow_cursor);" in source
    assert 'g_signal_connect(app_state.entry, "notify::cursor", G_CALLBACK(entry_cursor_changed), NULL);' in source
    assert "queue_entry_arrow();" in source
    assert "gtk_widget_set_cursor(app_state.entry, app_state.arrow_cursor);" in source
    assert "gtk_widget_set_cursor_from_name" not in source


def test_hud_keeps_one_persistent_head_outside_the_animated_stack() -> None:
    source = EYE_SOURCE_PATH.read_text(encoding="utf-8")

    assert "GtkWidget* head_area;" in source
    assert "gtk_overlay_add_overlay(GTK_OVERLAY(hud_root), app_state.head_area);" in source
    assert source.count('build_head_button(AS_TR("Expand the AgentSeat AI head"))') == 1
    assert "collapsed_eye_area" not in source
    assert "expanded_eye_area" not in source


def test_microhost_exposes_clipboard_control_only_inside_its_private_socket() -> None:
    source = MICROHOST_SOURCE_PATH.read_text(encoding="utf-8")

    assert "wlr_ext_data_control_manager_v1_create(server.wl_display, 1)" in source
    assert "wlr_data_control_manager_v1_create(server.wl_display)" in source
    assert "host compositor clipboard" in source


def serve_once(path: Path, response_factory: Any) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(1)
            ready.set()
            connection, _ = server.accept()
            with connection:
                request = json.loads(connection.recv(65_536).split(b"\n", 1)[0])
                response = response_factory(request)
                connection.sendall((json.dumps(response) + "\n").encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    return thread


def test_rpc_calls_daemon_directly_without_ctl_subprocess(controller: ModuleType, tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"
    controller.CONTROL_SOCKET = socket_path

    def response(request: dict[str, Any]) -> dict[str, Any]:
        assert request["method"] == "window.list"
        assert request["params"] == {}
        return {"jsonrpc": "2.0", "id": request["id"], "result": [{"id": "window-1"}]}

    thread = serve_once(socket_path, response)
    assert controller.rpc("window.list") == [{"id": "window-1"}]
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_rpc_preserves_structured_daemon_errors(controller: ModuleType, tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"
    controller.CONTROL_SOCKET = socket_path

    def response(request: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "error": {"code": "SEAT_PAUSED", "message": "Agent seat is paused"},
        }

    thread = serve_once(socket_path, response)
    with pytest.raises(controller.AgentSeatError, match="SEAT_PAUSED: Agent seat is paused"):
        controller.rpc("seat.status")
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_version_is_available_before_daemon_start(controller: ModuleType, tmp_path: Path) -> None:
    controller.CONTROL_SOCKET = tmp_path / "missing.sock"
    assert controller.version() == {
        "controller": "agentseat",
        "version": "0.6.2",
        "protocol_version": 2,
        "daemon": None,
    }


def test_heartbeat_reconnects_without_changing_window_focus(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller.CONTROL_SOCKET = tmp_path / "control.sock"
    controller.CONTROL_SOCKET.touch()
    monkeypatch.setattr(
        controller,
        "load_state",
        lambda: {"session_id": "session-a", "daemon_pid": 44},
    )
    monkeypatch.setattr(controller, "microhost_pid", lambda: 33)
    calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_rpc(method: str, params: dict[str, Any] | None = None) -> Any:
        calls.append((method, params))
        if method == "session.heartbeat":
            return {
                "session_id": "session-a",
                "daemon_pid": 44,
                "seat_created": True,
                "uptime_ms": 1234,
            }
        if method == "window.list":
            return [{"id": "agentseat:root", "focused": False}]
        raise AssertionError(method)

    monkeypatch.setattr(controller, "refresh_outer_geometry", dict)
    monkeypatch.setattr(controller, "rpc", fake_rpc)

    heartbeat = controller.session_heartbeat("session-a")

    assert heartbeat["healthy"] is True
    assert heartbeat["session_id"] == "session-a"
    assert heartbeat["microhost_pid"] == 33
    assert heartbeat["application_windows"] == 1
    assert calls == [("session.heartbeat", None), ("window.list", None)]
    assert all(method not in {"window.focus", "window.lease"} for method, _ in calls)


def test_heartbeat_refuses_a_replaced_session(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller.CONTROL_SOCKET = tmp_path / "control.sock"
    controller.CONTROL_SOCKET.touch()
    monkeypatch.setattr(controller, "load_state", lambda: {"session_id": "session-new"})
    monkeypatch.setattr(controller, "microhost_pid", lambda: 33)
    monkeypatch.setattr(
        controller,
        "rpc",
        lambda method, params=None: {
            "session_id": "session-new",
            "daemon_pid": 44,
            "seat_created": True,
        },
    )

    with pytest.raises(controller.AgentSeatError, match="session changed"):
        controller.session_heartbeat("session-old")


def test_watch_pins_first_session_and_opens_a_fresh_check_each_time(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected_ids: list[str | None] = []

    def fake_heartbeat(expected_session_id: str | None = None) -> dict[str, Any]:
        expected_ids.append(expected_session_id)
        return {
            "session_id": "session-a",
            "microhost_pid": 33,
            "application_windows": 1,
            "daemon": {
                "session_id": "session-a",
                "daemon_pid": 44,
                "uptime_ms": len(expected_ids) * 100,
                "request_count": len(expected_ids),
                "seat_paused": False,
                "collaboration": {"agent_allowed": True, "human_active": False},
            },
        }

    monkeypatch.setattr(controller, "session_heartbeat", fake_heartbeat)
    monkeypatch.setattr(controller.time, "sleep", lambda _: None)

    final = controller.watch_session(interval=0.1, count=3)
    samples = [json.loads(line) for line in capsys.readouterr().out.splitlines()]

    assert expected_ids == [None, "session-a", "session-a"]
    assert [sample["sequence"] for sample in samples] == [1, 2, 3]
    assert all(sample["healthy"] is True for sample in samples)
    assert final == samples[-1]


def test_sync_window_tree_is_read_only(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = [{"id": "w-1", "leased": True}, {"id": "w-2", "leased": False}]
    calls = []
    def rpc(method):
        calls.append(method)
        return windows
    monkeypatch.setattr(controller, "rpc", rpc)
    assert controller.sync_window_tree() == windows
    assert calls == ["window.list"]


def test_unicode_paste_reports_transport_without_claiming_inserted_text(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    def rpc(method, params):
        calls.append((method, params))
        return {"operation_id": "session:1", "execution": "running" if method == "keyboard.paste" else "finished",
                "verification": "clipboard_transport_only", "clipboard_transfers": 2,
                "host_clipboard": "untouched"}
    def unexpected_process(*args, **kwargs):
        raise AssertionError("Paste must stay inside native authority")
    monkeypatch.setattr(controller, "rpc", rpc)
    monkeypatch.setattr(controller.subprocess, "Popen", unexpected_process)
    result = controller.private_unicode_paste("你好🌍")
    assert [call[0] for call in calls] == ["keyboard.paste", "operation.get"]
    assert result["verification"] == "clipboard_transport_only"
    assert "typed" not in result


def test_unicode_paste_unknown_is_never_replayed(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    def rpc(method, params):
        calls.append(method)
        return {"operation_id": "session:1", "execution": "outcome_unknown", "submitted": 1}
    monkeypatch.setattr(controller, "rpc", rpc)
    with pytest.raises(controller.AgentSeatError, match="observe before any retry"):
        controller.private_unicode_paste("你好")
    assert calls == ["keyboard.paste"]


def serve_requests(path: Path, responses: list[Any]) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(4)
            server.settimeout(3)
            ready.set()
            for response_factory in responses:
                connection, _ = server.accept()
                with connection:
                    request = json.loads(connection.recv(65_536).split(b"\n", 1)[0])
                    response = response_factory(request)
                    connection.sendall((json.dumps({"jsonrpc": "2.0", "id": request["id"], **response}) + "\n").encode())

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    return thread


def test_writes_keep_original_session_when_state_file_changes(controller, tmp_path, monkeypatch):
    controller.CONTROL_SOCKET = tmp_path / "control.sock"
    current = {"session_id": "original"}
    monkeypatch.setattr(controller, "load_state", lambda: current)
    received = []

    def write(request):
        received.append(request)
        current["session_id"] = "replacement"
        return {"result": {}}

    server = serve_requests(controller.CONTROL_SOCKET, [write, write])
    controller.rpc("seat.pause")
    controller.rpc("seat.resume")
    server.join(timeout=3)
    assert not server.is_alive()
    assert [r["params"]["session_id"] for r in received] == ["original", "original"]


def test_input_does_not_send_after_heartbeat_session_changes(controller, tmp_path, monkeypatch):
    controller.CONTROL_SOCKET = tmp_path / "control.sock"
    monkeypatch.setattr(controller, "load_state", lambda: {"session_id": "original"})
    received = []

    def heartbeat(request):
        received.append(request)
        return {"result": {"session_id": "replacement", "control_epoch": 5}}

    server = serve_requests(controller.CONTROL_SOCKET, [heartbeat])
    with pytest.raises(controller.AgentSeatError, match="SESSION_MISMATCH"):
        controller.rpc("keyboard.type", {"text": "must not send"})
    server.join(timeout=3)
    assert [r["method"] for r in received] == ["session.heartbeat"]


def test_rpc_v2_error_preserves_machine_kind(controller, tmp_path):
    controller.CONTROL_SOCKET = tmp_path / "control.sock"
    server = serve_requests(controller.CONTROL_SOCKET, [lambda _: {
        "error": {"code": -32000, "message": "Control changed", "data": {"kind": "STALE_CONTROL"}}
    }])
    with pytest.raises(controller.AgentSeatError, match="STALE_CONTROL: Control changed"):
        controller.rpc("seat.status")
    server.join(timeout=3)


def test_type_poll_failure_cancels_once_without_replaying_text(controller, monkeypatch):
    calls = []

    def rpc(method, params=None):
        calls.append((method, params))
        if method == "keyboard.type":
            return {"operation_id": "session:1", "execution": "running"}
        if method == "operation.get":
            raise controller.AgentSeatError("lost receipt")
        return {"execution": "cancelled"}

    monkeypatch.setattr(controller, "rpc", rpc)
    monkeypatch.setattr(controller.time, "sleep", lambda _: None)
    with pytest.raises(controller.AgentSeatError, match="lost receipt"):
        controller.type_text("hello")
    assert [c[0] for c in calls] == ["keyboard.type", "operation.get", "operation.cancel"]
    assert calls[-1][1] == {"operation_id": "session:1"}


def test_pause_waits_for_matching_compositor_epoch(controller, monkeypatch):
    controller.RPC_SESSION_ID = "session"
    heartbeats = iter([
        {"session_id": "session", "gate_allowed": False, "gate_epoch": 2, "control_epoch": 3},
        {"session_id": "session", "gate_allowed": False, "gate_epoch": 3, "control_epoch": 3},
    ])
    read_count = []

    def rpc(method):
        if method == "seat.pause":
            return {"paused": True}
        read_count.append(method)
        return next(heartbeats)

    monkeypatch.setattr(controller, "rpc", rpc)
    monkeypatch.setattr(controller.time, "sleep", lambda _: None)
    assert controller.pause_seat()["compositor_acknowledged"] is True
    assert len(read_count) == 2


def test_x11_geometry_keeps_host_viewport_and_full_pixel_coordinates(controller, monkeypatch):
    monkeypatch.setattr(controller, "rpc", lambda method: {"output": {
        "width": 2548, "height": 1536, "pixel_width": 2548, "pixel_height": 1536,
        "scale": 1.0, "geometry_version": 8}})
    state = controller.refresh_outer_geometry({"launch": {"mode": "x11"}, "host_output_scale": 1.6})
    assert state["viewport_logical_size"] == [1592, 960]
    assert state["application_coordinate_size"] == state["physical_buffer_size"] == [2548, 1536]
    assert state["render_scale"] == 1.0
    assert "AGENTSEAT_APPLICATION_MODE=x11" in controller.microhost_launch_command("", 1.6, "x11")


@pytest.mark.parametrize(('running','private_bus','expected'), [(True,False,'desktop'),(True,True,'fcitx5'),(False,False,'inherit')])
def test_auto_ime_reuses_desktop_and_respects_explicit_private_bus(controller, monkeypatch, running, private_bus, expected):
    for name in ('GTK_IM_MODULE', 'QT_IM_MODULE', 'QT_IM_MODULES', 'XMODIFIERS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('GTK_IM_MODULE', 'fcitx')
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/disposable-test-bus')
    monkeypatch.setattr(controller.Path, 'is_file', lambda _: True)
    calls=[]
    def probe(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0 if running else 1)
    monkeypatch.setattr(controller.subprocess, 'run', probe)
    assert controller.resolve_ime('auto',private_bus)==expected
    assert calls==[['/usr/bin/fcitx5-remote','--check']]
    assert controller.resolve_ime('simple')=='simple'
    assert len(calls)==1


@pytest.mark.parametrize('module', ['ibus', 'wayland', 'xim', 'gtk-im-context-simple', ''])
def test_auto_ime_respects_explicit_user_module(controller, monkeypatch, module):
    for name in ('QT_IM_MODULE', 'QT_IM_MODULES', 'XMODIFIERS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('GTK_IM_MODULE', module)
    monkeypatch.setattr(controller.subprocess, 'run', lambda *a, **k: pytest.fail('must not probe or choose another daemon'))
    assert controller.resolve_ime('auto') == 'inherit'


@pytest.mark.parametrize('mode', ['wayland', 'x11'])
def test_inherited_input_environment_survives_launch(tmp_path, mode):
    expected = {'GTK_IM_MODULE': 'ibus', 'QT_IM_MODULE': 'ibus', 'XMODIFIERS': '@im=ibus',
                'XCOMPOSEFILE': '/disposable/Compose', 'XKB_DEFAULT_LAYOUT': 'fr', 'IBUS_ADDRESS': 'unix:path=/disposable/ibus'}
    spec = tmp_path / 'launch.json'
    spec.write_text(json.dumps({'version': 1, 'mode': mode, 'ime': 'inherit',
        'argv': [os.sys.executable, '-c', 'import os,json; print(json.dumps({k:os.environ.get(k) for k in ' + repr(list(expected)) + '}))']}))
    result = subprocess.run([str(LAUNCHER_PATH)], env={**os.environ, **expected,
        'AGENTSEAT_LAUNCH_SPEC': str(spec), 'AGENTSEAT_ENV_RECEIPT': str(tmp_path/'env.json'),
        'AGENTSEAT_SKIP_X11_READY': '1'}, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == expected


def test_input_environment_preserves_ibus_endpoint_before_display_changes(controller, monkeypatch):
    monkeypatch.setenv('GTK_IM_MODULE', 'ibus')
    monkeypatch.delenv('IBUS_ADDRESS', raising=False)
    monkeypatch.setattr(controller.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps({'IBUS_ADDRESS':'unix:path=/disposable/ibus','IBUS_ADDRESS_FILE':'/disposable/ibus-address'})))
    assert controller.capture_input_environment()['IBUS_ADDRESS'] == 'unix:path=/disposable/ibus'


def test_hud_positions_follow_program_not_instance_or_document(controller, tmp_path, monkeypatch):
    monkeypatch.setattr(controller, "CONFIG_HOME", tmp_path)
    first = controller.application_hud_position_file(["/usr/bin/cat", "one.txt"])
    assert first == controller.application_hud_position_file(["/usr/bin/env", "FOO=bar", "cat", "two.txt"])
    assert first != controller.application_hud_position_file(["/usr/bin/ls"])
    assert controller.application_hud_position_file(["python3", "one.py"], str(tmp_path)) != controller.application_hud_position_file(["python3", "two.py"], str(tmp_path))
    command = controller.microhost_launch_command("[]", 1.6, "x11", first)
    assert "AGENTSEAT_HUD_POSITION_FILE=" in command


def test_background_heartbeat_keeps_resident_session_healthy(controller, monkeypatch, tmp_path):
    controller.CONTROL_SOCKET = tmp_path / "control.sock"
    controller.CONTROL_SOCKET.touch()
    monkeypatch.setattr(controller, "load_state", lambda: {"session_id": "resident", "daemon_pid": 44})
    monkeypatch.setattr(controller, "microhost_pid", lambda: 33)
    monkeypatch.setattr(controller, "refresh_outer_geometry", dict)
    def rpc(method, params=None):
        if method == "session.heartbeat":
            return {"session_id": "resident", "daemon_pid": 44, "seat_created": True}
        assert method == "window.list"
        return []
    monkeypatch.setattr(controller, "rpc", rpc)
    result = controller.session_heartbeat("resident")
    assert result["healthy"] and result["reconnectable"]
    assert result["application_state"] == "background"
    assert result["application_windows"] == 0 and not result["window_ready"]
