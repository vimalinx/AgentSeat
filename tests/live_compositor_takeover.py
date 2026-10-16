#!/usr/bin/env python3
"""Exercise real Wayland backend takeover while the inner daemon is frozen.

Parent AgentSeat virtual input appears as physical backend input to the inner
test compositor. No event is injected into the user's host compositor.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("takeover condition timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=int, default=108)
    args = parser.parse_args()
    out = args.report_dir.resolve()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance = f"p1-takeover-{os.getpid()}"
    report = {"input_source": "parent private seat -> inner Wayland backend", "tests": {}}
    inner = None

    def cli(*argv):
        result = subprocess.run([str(ROOT / "bin/agentseat"), "--instance", instance, *argv], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return json.loads(result.stdout)

    def rpc(method, params=None):
        payload = {"session_id": inner["session_id"], **(params or {})}
        payload.setdefault("controller_id", "inner-test")
        if method.startswith(("keyboard.", "pointer.")):
            lease = rpc("control.acquire")
            payload.setdefault("lease_id", lease["lease_id"])
            payload.setdefault("control_epoch", lease["control_epoch"])
        payload.setdefault("request_id", time.monotonic_ns())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(inner["socket"])
            client.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": payload}) + "\n").encode())
            data = b""
            while b"\n" not in data:
                part = client.recv(65536)
                if not part:
                    raise RuntimeError("daemon disconnected")
                data += part
            response = json.loads(data.split(b"\n")[0])
            if "error" in response:
                raise RuntimeError(response)
            return response["result"]

    def ui():
        return json.loads((out / "ui.json").read_text())

    try:
        launched = cli("run", "--ime", "simple", "--host-workspace", str(args.workspace), "--", "/usr/bin/python3", str(ROOT / "tests/nested_control_fixture.py"), "--out", str(out))
        report["launch_receipt"] = launched["state"]["host_launch_receipt"]
        assert report["launch_receipt"]["relation"] != "outer-focused"
        wait_for(lambda: (out / "nested.json").exists())
        inner = json.loads((out / "nested.json").read_text())
        cli("observe", str(out / "before.png"))

        # Moving the backend pointer cannot cancel typing or revoke its epoch.
        hover_stream = rpc("keyboard.type", {"text": "v" * 100, "interval_ms": 12})
        wait_for(lambda: len(ui()["text"]) >= 3)
        before_hover = rpc("session.heartbeat")
        for x, y in [("0.2", "0.2"), ("0.8", "0.8"), ("0.4", "0.5")]:
            cli("move", x, y)
        after_hover = rpc("session.heartbeat")
        assert after_hover["control_epoch"] == before_hover["control_epoch"]
        assert not rpc("collaboration.status")["human_active"]
        completed = wait_for(lambda: (op if (op := rpc("operation.get", {"operation_id": hover_stream["operation_id"]}))["execution"] == "finished" else None))
        wait_for(lambda: ui()["text"] == "v" * 100)
        report["tests"]["backend_hover_keeps_typing"] = {"before": before_hover, "after": after_hover, "operation": completed}
        # A press at that pointer then takes over and cancels a new stream.
        click_stream = rpc("keyboard.type", {"text": "q" * 300, "interval_ms": 15})
        wait_for(lambda: len(ui()["text"]) >= 104)
        cli("move", "0.8", "0.8")
        cli("click")
        stopped_click = wait_for(lambda: (op if (op := rpc("operation.get", {"operation_id": click_stream["operation_id"]}))["execution"] == "cancelled" else None))
        report["tests"]["backend_click_cancels_typing"] = stopped_click
        wait_for(lambda: not rpc("collaboration.status")["human_active"])
        # Restore the entry and clear only this disposable fixture.
        cli("key", "29", "down"); cli("key", "30", "down"); cli("key", "30", "up"); cli("key", "29", "up")
        cli("key", "14", "down"); cli("key", "14", "up")
        wait_for(lambda: not rpc("collaboration.status")["human_active"])
        operation = rpc("keyboard.type", {"text": "a" * 300, "interval_ms": 15})
        wait_for(lambda: len(ui()["text"]) >= 4)
        cli("type", "H")
        wait_for(lambda: "H" in ui()["text"])
        time.sleep(.15)
        text = ui()["text"]
        assert text.endswith("H") and len(text) < 300, text
        stopped = rpc("operation.get", {"operation_id": operation["operation_id"]})
        assert stopped["execution"] == "cancelled", stopped
        report["tests"]["backend_input_cancels_typing"] = {"operation": stopped, "text": text}
        wait_for(lambda: not rpc("collaboration.status")["human_active"])

        # Agent clipboard must be revoked by the compositor even when its
        # daemon cannot process any event. The outer seat is a test input
        # apparatus, not the human's host compositor.
        paste = rpc("keyboard.paste", {"text": "private clipboard proof"})
        wait_for(lambda: ui()["text"].endswith("private clipboard proof"))
        private_env = {**os.environ, "WAYLAND_DISPLAY": inner["wayland_display"]}
        private_env.pop("DISPLAY", None)
        def clipboard_types():
            return subprocess.run(["/usr/bin/wl-paste", "--list-types"], env=private_env,
                capture_output=True, text=True, timeout=2)
        assert clipboard_types().returncode == 0
        os.kill(inner["daemon_pid"], signal.SIGSTOP)
        try:
            cli("type", "h")
            wait_for(lambda: clipboard_types().returncode != 0)
            report["tests"]["frozen_daemon_backend_takeover_clears_clipboard"] = "PASS"
        finally:
            os.kill(inner["daemon_pid"], signal.SIGCONT)
        wait_for(lambda: not rpc("collaboration.status")["human_active"])

        # Hold a virtual Ctrl, freeze its daemon, then type from the backend.
        # The compositor must release Ctrl without any daemon cleanup running.
        rpc("keyboard.key", {"keycode": 29, "pressed": True})
        wait_for(lambda: any(k[0] == "press" and k[1] == 65507 for k in ui()["keys"]))
        before = ui()["text"]
        os.kill(inner["daemon_pid"], signal.SIGSTOP)
        try:
            cli("type", "h")
            wait_for(lambda: ui()["text"] == before + "h")
            report["tests"]["frozen_daemon_ctrl_cleanup"] = "PASS"
        finally:
            os.kill(inner["daemon_pid"], signal.SIGCONT)
        wait_for(lambda: not rpc("collaboration.status")["human_active"])

        # The human's same Ctrl must remain held when Agent Ctrl is cleared.
        rpc("keyboard.key", {"keycode": 29, "pressed": True})
        time.sleep(.05)
        releases_before = sum(k[0] == "release" and k[1] == 65507 for k in ui()["keys"])
        os.kill(inner["daemon_pid"], signal.SIGSTOP)
        try:
            cli("key", "29", "down")
            cli("key", "45", "down")  # X with physical Ctrl still held.
            cli("key", "45", "up")
            current = ui()
            assert sum(k[0] == "release" and k[1] == 65507 for k in current["keys"]) == releases_before, current
            assert any(k[0] == "press" and k[1] in (ord("x"), ord("X")) and k[2] & 4 for k in current["keys"]), current
            cli("key", "29", "up")
            wait_for(lambda: sum(k[0] == "release" and k[1] == 65507 for k in ui()["keys"]) > releases_before)
            report["tests"]["same_modifier_preserves_backend_hold"] = "PASS"
        finally:
            os.kill(inner["daemon_pid"], signal.SIGCONT)
        wait_for(lambda: not rpc("collaboration.status")["human_active"])
        rpc("keyboard.key", {"keycode": 29, "pressed": True})
        time.sleep(.05)
        releases_before = sum(k[0] == "release" and k[1] == 65507 for k in ui()["keys"])
        before = ui()["text"]
        os.kill(inner["daemon_pid"], signal.SIGKILL)
        wait_for(lambda: sum(k[0] == "release" and k[1] == 65507 for k in ui()["keys"]) > releases_before)
        cli("type", "z")
        wait_for(lambda: ui()["text"] == before + "z")
        report["tests"]["daemon_crash_releases_ctrl"] = "PASS"
        cli("observe", str(out / "after.png"))
        report["status"] = "PASS"
    except Exception as error:
        report["status"] = "FAIL"
        report["error"] = repr(error)
        raise
    finally:
        if inner:
            try:
                os.kill(inner["daemon_pid"], signal.SIGCONT)
            except ProcessLookupError:
                pass
        try:
            report["cleanup"] = cli("stop")
        finally:
            (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
