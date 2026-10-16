#!/usr/bin/env python3
"""Test-only second microhost: its Wayland-backend input is scoped to a parent
AgentSeat, allowing physical-source takeover tests without host input injection.
This nesting is a test apparatus, not an installed runtime configuration.
"""

import argparse
import importlib.machinery
import json
import os
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise RuntimeError("nested fixture did not become ready")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--fixture", choices=["control", "drag", "pixels", "close", "input", "background"], default="control")
    parser.add_argument("--toolkit", choices=["gtk3", "gtk4", "qt6"], default="gtk4")
    parser.add_argument("--x11", action="store_true")
    parser.add_argument("--ime", choices=["simple", "fcitx5", "desktop", "inherit"], default="simple")
    parser.add_argument("--scale", default=None)
    parser.add_argument("--a11y", action="store_true")
    parser.add_argument("--hud", action="store_true")
    parser.add_argument("--keep-parent-window", action="store_true")
    parser.add_argument("--hud-position-file", type=Path)
    parser.add_argument("--clipboard-checks", action="store_true")
    parser.add_argument("--bin-dir", type=Path, default=ROOT / "bin")
    args = parser.parse_args()
    os.umask(0o077)
    runtime = Path(os.environ["AGENTSEAT_RUNTIME_DIR"]) / "inner"
    runtime.mkdir(mode=0o700)
    args.out.mkdir(parents=True, exist_ok=True)
    spec = runtime / "launch.json"
    pixel_fixture = args.fixture == "pixels"
    x11 = pixel_fixture or args.x11
    scale = args.scale or ("1.6" if pixel_fixture else "1")
    if pixel_fixture:
        binary = args.out / "pixel-fixture"
        subprocess.run(["cc", str(ROOT / "tests/x11_pixels_fixture.c"), "-lX11", "-o", str(binary)], check=True)
    argv = [str(binary), str(args.out / "ui.json")] if pixel_fixture else [
        "/usr/bin/python3", str(ROOT / "tests" / (args.fixture + "_fixture.py")), "--state", str(args.out / "ui.json")]
    if args.fixture == "input":
        argv.extend(["--toolkit", args.toolkit])
    if args.clipboard_checks:
        argv.append("--clipboard-checks")
    controller = importlib.machinery.SourceFileLoader("fixture_controller", str(args.bin_dir / "agentseat")).load_module()
    spec.write_text(json.dumps({
        "version": 1, "mode": "x11" if x11 else "wayland", "private_dbus": False,
        "ime": args.ime, "a11y": args.a11y,
        "input_environment": controller.capture_input_environment(),
        "argv": argv,
    }))
    env = {**os.environ, "WLR_BACKENDS": "wayland", "WLR_RENDERER": "pixman",
           "GSK_RENDERER": "cairo", "GTK_A11Y": "none", "AGENTSEAT_HOST_SCALE": scale,
           "AGENTSEAT_APPLICATION_MODE": "x11" if x11 else "wayland",
           "AGENTSEAT_RUNTIME_DIR": str(runtime), "AGENTSEAT_LAUNCH_SPEC": str(spec),
           "AGENTSEAT_ENV_RECEIPT": str(runtime / "application-env.json"),
           "AGENTSEAT_READY_FILE": str(runtime / "ready"),
           "AGENTSEAT_ACTIVITY_SOCKET": str(runtime / "human.sock")}
    if args.hud_position_file:
        env["AGENTSEAT_HUD_POSITION_FILE"] = str(args.hud_position_file)
    host = daemon = eye = guard = None

    def terminate(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    try:
        if args.keep_parent_window:
            guard = subprocess.Popen(["/usr/bin/python3", "-c",
                "import gi;gi.require_version('Gtk','4.0');from gi.repository import Gtk,Gio;"
                "app=Gtk.Application(application_id='org.vimalinx.AgentSeatParentGuard',flags=Gio.ApplicationFlags.NON_UNIQUE);"
                "app.connect('activate',lambda a:(lambda w:(w.set_child(Gtk.Label(label='Private lifecycle test parent')),w.present()))(Gtk.ApplicationWindow(application=a,title='AgentSeat lifecycle parent guard')));app.run(None)"],
                env={**os.environ, "GTK_A11Y": "none", "GSK_RENDERER": "cairo"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(.3)
        with (args.out / "inner-host.log").open("w") as log:
            host = subprocess.Popen([str(args.bin_dir / "agentseat-microhost"), "-d", "--", str(args.bin_dir / "launch-app")], env=env, stdout=log, stderr=log)
        wait_for(lambda: (runtime / "ready").exists())
        app_env = json.loads((runtime / "application-env.json").read_text())
        session_id = uuid.uuid4().hex
        control = runtime / "control.sock"
        with (args.out / "inner-daemon.log").open("w") as log:
            daemon = subprocess.Popen([
                str(args.bin_dir / "agentseatd"), "--socket", str(control),
                "--activity-socket", str(runtime / "human.sock"), "--session-id", session_id,
            ], env={**env, "WAYLAND_DISPLAY": app_env["wayland_display"]}, stdout=log, stderr=log)
        wait_for(control.exists)

        def rpc(method, params):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                client.connect(str(control))
                client.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {"session_id": session_id, "controller_id": "inner-bootstrap", "request_id": time.monotonic_ns(), **params}}) + "\n").encode())
                data = b""
                while b"\n" not in data:
                    data += client.recv(65536)
                response = json.loads(data.split(b"\n")[0])
                if "error" in response:
                    raise RuntimeError(response)
                return response["result"]

        rpc("seat.create", {})
        rpc("window.lease", {"window_id": "agentseat:root"})
        lease = rpc("control.acquire", {})
        focus = rpc("window.focus", {"window_id": "agentseat:root", "lease_id": lease["lease_id"], "control_epoch": lease["control_epoch"]})
        if "operation_id" in focus:
            wait_for(lambda: rpc("operation.get", {"operation_id": focus["operation_id"]})["execution"] == "finished")
        rpc("control.release", {"lease_id": lease["lease_id"]})
        if args.hud:
            with (args.out / "inner-hud.log").open("w") as log:
                eye = subprocess.Popen([str(args.bin_dir / "agentseat-eye"), "--embedded", "--socket", str(control)],
                    env={**env, "WAYLAND_DISPLAY": app_env["wayland_display"], "AGENTSEAT_SESSION_ID": session_id,
                         "AGENTSEAT_HUD_SCALE": scale if x11 else "1", "AGENTSEAT_LANGUAGE": "zh-CN",
                         "AGENTSEAT_LOCALE_DIR": str(ROOT / "build/locale")}, stdout=log, stderr=log)
        (args.out / "nested.json").write_text(json.dumps({
            "session_id": session_id, "socket": str(control), "daemon_pid": daemon.pid,
            "microhost_pid": host.pid, "wayland_display": app_env["wayland_display"],
            "runtime": str(runtime), "x_display": app_env.get("display"),
        }))
        code = host.wait()
        (args.out / "inner-exit.json").write_text(json.dumps({"exit_code": code}))
        if args.ime == "desktop":
            # Here the "desktop" service belongs to our disposable parent.
            # Closing its secondary X display must not stop the parent IME.
            parent_ime = json.loads((runtime.parent / "a11y.json").read_text())["ime_pid"]
            probe = subprocess.run(["/usr/bin/fcitx5-remote", "--check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            (args.out / "parent-ime-survived.json").write_text(json.dumps({
                "same_pid_alive": Path(f"/proc/{parent_ime}").exists(), "ready": probe.returncode == 0}))
        return code
    finally:
        for child in [eye, daemon, host, guard]:
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGCONT)
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
