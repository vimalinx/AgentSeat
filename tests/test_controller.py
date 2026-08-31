from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import socket
import stat
import subprocess
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = ROOT / "bin/agentseat"
LAUNCHER_PATH = ROOT / "bin/launch-app"
EYE_SOURCE_PATH = ROOT / "native/agentseat-eye.c"
MICROHOST_SOURCE_PATH = ROOT / "vendor/cage/cage.c"
BUILD_TRANSLATIONS = ROOT / "scripts/build-translations.sh"
CHECK_TRANSLATIONS = ROOT / "scripts/check-translations.sh"


def load_script(name: str, path: Path) -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def controller() -> ModuleType:
    return load_script("agentseat_controller_test", CONTROLLER_PATH)


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
    focus_guards = "float true; no_initial_focus true; focus_on_activate false; suppress_event activate activatefocus"
    assert controller.launch_window_properties(2, 2) == f"[{focus_guards}]"
    assert controller.launch_window_properties(8, 2) == f"[workspace 8; {focus_guards}]"
    with pytest.raises(controller.AgentSeatError, match="positive integer"):
        controller.launch_window_properties(0, 2)


def test_render_scale_tracks_target_monitor_without_integer_resampling(controller: ModuleType) -> None:
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
    assert controller.physical_size_for_host(1592, 1.6) == 2547
    assert controller.physical_size_for_host(960, 1.6) == 1536


def test_onlyoffice_preset_prefers_the_native_wayland_lane(controller: ModuleType) -> None:
    assert controller.legacy_command("onlyoffice") == (
        ["/usr/bin/onlyoffice-desktopeditors"],
        "wayland",
        "onlyoffice-native-wayland",
    )


def test_keyboard_key_stays_on_the_private_rpc_seat(controller: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(controller, "sync_window_tree", lambda: [])
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
    assert spec == {
        "version": 1,
        "argv": ["/usr/bin/zenity", "--info", "--text=generic"],
        "mode": "wayland",
        "cwd": str(tmp_path),
    }
    assert stat.S_IMODE(controller.LAUNCH_SPEC.stat().st_mode) == 0o600
    assert not controller.LAUNCH_SPEC.with_suffix(".tmp").exists()


def test_application_fit_receipt_requires_positive_integer_dimensions(
    controller: ModuleType, tmp_path: Path
) -> None:
    controller.APPLICATION_FIT = tmp_path / "application-fit.json"
    controller.APPLICATION_FIT.write_text(
        json.dumps(
            {
                "logical_width": 400,
                "logical_height": 200,
                "buffer_width": 640,
                "buffer_height": 320,
            }
        ),
        encoding="utf-8",
    )
    assert controller.load_application_fit() == {
        "logical_width": 400,
        "logical_height": 200,
        "buffer_width": 640,
        "buffer_height": 320,
    }

    controller.APPLICATION_FIT.write_text('{"logical_width": 0}', encoding="utf-8")
    assert controller.load_application_fit() is None


def test_outer_resize_is_scoped_to_the_agentseat_window(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients = iter(
        [
            {"address": "0xseat", "size": [1280, 720]},
            {"address": "0xseat", "size": [400, 200]},
        ]
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(controller, "host_client_for_pid", lambda signature, pid: next(clients))
    monkeypatch.setattr(
        controller,
        "run",
        lambda argv, **_: commands.append(argv) or subprocess.CompletedProcess(argv, 0, "ok", ""),
    )

    outer = controller.resize_outer_to_fit(
        "host-signature",
        123,
        "0xseat",
        {
            "logical_width": 400,
            "logical_height": 200,
            "buffer_width": 640,
            "buffer_height": 320,
        },
    )

    assert outer["size"] == [400, 200]
    assert commands == [
        [
            "hyprctl",
            "--instance",
            "host-signature",
            "dispatch",
            "resizewindowpixel",
            "exact 400 200,address:0xseat",
        ]
    ]


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


def test_physical_wayland_launch_uses_dpi_without_inheriting_host_surface_scale(tmp_path: Path) -> None:
    spec_path = tmp_path / "launch.json"
    probe = (
        "import json,os; print(json.dumps({'scale':os.environ.get('QT_SCALE_FACTOR'),"
        "'auto':os.environ.get('QT_AUTO_SCREEN_SCALE_FACTOR'),'dpi':os.environ.get('QT_FONT_DPI')}))"
    )
    spec_path.write_text(
        json.dumps(
            {
                "version": 1,
                "argv": [os.fspath(Path(os.sys.executable)), "-c", probe],
                "mode": "wayland",
                "cwd": str(tmp_path),
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
            "QT_SCALE_FACTOR": "1",
            "WAYLAND_DISPLAY": "wayland-test",
        },
    )
    assert json.loads(result.stdout) == {"scale": None, "auto": "1", "dpi": "154"}


def test_hud_cursor_stays_native_size_after_gtk_text_override() -> None:
    source = EYE_SOURCE_PATH.read_text(encoding="utf-8")

    assert "#define PANEL_HEIGHT 232" in source
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
        "version": "0.5.0",
        "protocol_version": 1,
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


def test_sync_window_tree_leases_only_new_windows_and_focuses_newest(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = [
        {"id": "root", "leased": True},
        {"id": "dialog-a", "leased": False},
        {"id": "dialog-b", "leased": False},
    ]
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_rpc(method: str, params: dict[str, Any] | None = None) -> Any:
        payload = params or {}
        calls.append((method, payload))
        if method == "window.list":
            return [dict(window) for window in windows]
        if method == "window.lease":
            target = next(window for window in windows if window["id"] == payload["window_id"])
            target["leased"] = True
            return {"leased": True}
        if method == "window.focus":
            return {"focused": payload["window_id"]}
        raise AssertionError(method)

    monkeypatch.setattr(controller, "rpc", fake_rpc)

    assert controller.sync_window_tree() == [
        {"id": "root", "leased": True},
        {"id": "dialog-a", "leased": True},
        {"id": "dialog-b", "leased": True},
    ]
    assert calls == [
        ("window.list", {}),
        ("window.lease", {"window_id": "dialog-a"}),
        ("window.lease", {"window_id": "dialog-b"}),
        ("window.focus", {"window_id": "dialog-b"}),
        ("window.list", {}),
    ]


def test_unicode_paste_uses_only_private_wayland_clipboard(
    controller: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wl_copy = tmp_path / "wl-copy"
    wl_paste = tmp_path / "wl-paste"
    for helper in (wl_copy, wl_paste):
        helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        helper.chmod(0o700)
    controller.WL_COPY = wl_copy
    controller.WL_PASTE = wl_paste
    monkeypatch.setattr(controller, "load_state", lambda: {"wayland_display": "wayland-private"})
    monkeypatch.setenv("DISPLAY", ":host")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-host")

    class InputPipe:
        def __init__(self) -> None:
            self.content = ""

        def write(self, value: str) -> None:
            self.content += value

        def close(self) -> None:
            pass

    class Owner:
        def __init__(self) -> None:
            self.stdin = InputPipe()
            self.stderr = None
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    owner = Owner()
    popen_receipt: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kwargs: Any) -> Owner:
        popen_receipt.update({"argv": argv, "env": kwargs["env"]})
        return owner

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert argv == [str(wl_paste), "--list-types"]
        return subprocess.CompletedProcess(argv, 0, "text/plain;charset=utf-8\n", "")

    key_calls: list[tuple[str, dict[str, Any]]] = []

    def fake_rpc(method: str, params: dict[str, Any]) -> dict[str, bool]:
        key_calls.append((method, params))
        return {"delivered": True}

    monkeypatch.setattr(controller.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(controller, "run", fake_run)
    monkeypatch.setattr(controller, "rpc", fake_rpc)

    result = controller.private_unicode_paste("你好🌍")

    assert result == {"typed": 3, "lane": "private-wayland-clipboard", "host_clipboard": "untouched"}
    assert owner.stdin.content == "你好🌍"
    assert "你好🌍" not in popen_receipt["argv"]
    assert popen_receipt["env"]["WAYLAND_DISPLAY"] == "wayland-private"
    assert "DISPLAY" not in popen_receipt["env"]
    assert key_calls[:4] == [
        ("keyboard.key", {"keycode": 29, "pressed": True}),
        ("keyboard.key", {"keycode": 47, "pressed": True}),
        ("keyboard.key", {"keycode": 47, "pressed": False}),
        ("keyboard.key", {"keycode": 29, "pressed": False}),
    ]
