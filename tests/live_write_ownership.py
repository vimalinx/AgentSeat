#!/usr/bin/env python3
"""Actual GTK effects for write leases, receipt eviction and hold deadlines."""
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
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("application proof did not arrive")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=int, default=108)
    args = parser.parse_args()
    out = args.report_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    instance = f"ownership-{os.getpid()}"
    report = {"tests": {}}
    state = None

    def cli(*argv):
        r = subprocess.run([str(ROOT / "bin/agentseat"), "--instance", instance, *argv],
                           text=True, capture_output=True, timeout=35)
        if r.returncode:
            raise RuntimeError(r.stderr)
        return json.loads(r.stdout)

    def rpc(method, params=None, owner="A"):
        import hashlib
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "agentseat-apps" / ("app-" + hashlib.sha256(instance.encode()).hexdigest()[:24])
        payload = {"session_id": state["session_id"], "controller_id": owner,
                   "request_id": time.monotonic_ns(), **(params or {})}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(runtime / "control.sock"))
            client.sendall((json.dumps({"jsonrpc": "2.0", "id": time.monotonic_ns(), "method": method, "params": payload}) + "\n").encode())
            data = b""
            while b"\n" not in data:
                chunk = client.recv(65536)
                if not chunk:
                    raise RuntimeError("daemon closed without a receipt")
                data += chunk
            return json.loads(data.split(b"\n")[0])

    def ok(method, params=None, owner="A"):
        value = rpc(method, params, owner)
        assert "result" in value, value
        return value["result"]

    def kind(value):
        return value["error"]["data"]["kind"]

    def ui():
        return json.loads((out / "ui.json").read_text())

    try:
        launched = cli("run", "--host-workspace", str(args.workspace), "--", "/usr/bin/env",
                       "GSK_RENDERER=cairo", "GTK_A11Y=none", "/usr/bin/python3",
                       str(ROOT / "tests/control_fixture.py"), "--state", str(out / "ui.json"))
        state = launched["state"]
        report["launch"] = state
        assert state["host_launch_receipt"]["relation"] != "outer-focused"
        cli("observe", str(out / "before.png"))
        lease = ok("control.acquire")
        context = {"lease_id": lease["lease_id"], "control_epoch": lease["control_epoch"]}
        assert kind(rpc("control.acquire", owner="B")) == "WRITE_LEASE_BUSY"
        assert kind(rpc("keyboard.type", {**context, "text": "bad"}, owner="B")) == "NO_WRITE_LEASE"
        report["tests"]["two_controllers_cannot_interleave"] = "PASS"

        ok("pointer.move_absolute", {**context, "x": .5, "y": .078})
        request_id = time.monotonic_ns()
        click = {**context, "button": 272, "request_id": request_id}
        accepted = ok("pointer.click", click)
        wait_for(lambda: ui()["clicks"] == 1)
        # Parameter member ordering does not change the operation identity.
        assert ok("pointer.click", dict(reversed(list(click.items())))) == accepted
        assert kind(rpc("pointer.click", {**click, "button": 273})) == "REQUEST_ID_CONFLICT"
        recovered = ok("request.get", {"request_id": request_id})
        assert recovered["result"] == accepted
        time.sleep(.1)
        assert ui()["clicks"] == 1
        report["tests"]["replay_and_reconnect_click_once"] = "PASS"

        for index in range(140):
            ok("activity.set", {"summary": f"bounded receipt {index}"})
        assert kind(rpc("pointer.click", click)) == "REQUEST_EXPIRED"
        assert ui()["clicks"] == 1
        report["tests"]["eviction_does_not_reenable_old_click"] = "PASS"
        ok("control.release", {"lease_id": lease["lease_id"]})

        lease = ok("control.acquire", {"ttl_ms": 250}, owner="B")
        context = {"lease_id": lease["lease_id"], "control_epoch": lease["control_epoch"]}
        op = ok("keyboard.type", {**context, "text": "a" * 100, "interval_ms": 10}, owner="B")
        wait_for(lambda: ok("operation.get", {"operation_id": op["operation_id"]})["execution"] == "cancelled")
        count = len(ui()["text"])
        time.sleep(.15)
        assert 0 < count < 100 and len(ui()["text"]) == count
        report["tests"]["lease_expiry_cancels_input"] = {"actual_characters": count}

        lease = ok("control.acquire")
        context = {"lease_id": lease["lease_id"], "control_epoch": lease["control_epoch"]}
        releases = sum(k[0] == "release" and k[1] == 65507 for k in ui()["keys"])
        ok("keyboard.key", {**context, "keycode": 29, "pressed": True, "hold_ms": 300})
        wait_for(lambda: any(k[0] == "press" and k[1] == 65507 for k in ui()["keys"]))
        began = time.monotonic()
        os.kill(state["daemon_pid"], signal.SIGSTOP)
        try:
            wait_for(lambda: sum(k[0] == "release" and k[1] == 65507 for k in ui()["keys"]) > releases, 2)
            elapsed = time.monotonic() - began
            assert elapsed < 1
        finally:
            os.kill(state["daemon_pid"], signal.SIGCONT)
        wait_for(lambda: ok("session.heartbeat")["control_epoch"] > context["control_epoch"])
        assert kind(rpc("keyboard.key", {**context, "keycode": 29, "pressed": False})) == "NO_WRITE_LEASE"
        report["tests"]["frozen_daemon_hold_deadline"] = {"release_seconds": elapsed}
        before = ui()["text"]
        cli("type", "z")
        wait_for(lambda: ui()["text"] == before + "z")
        cli("observe", str(out / "after.png"))
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = repr(exc)
        raise
    finally:
        if state:
            try:
                os.kill(state["daemon_pid"], signal.SIGCONT)
            except ProcessLookupError:
                pass
        try:
            report["cleanup"] = cli("stop")
        finally:
            (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["tests"], indent=2))


if __name__ == "__main__":
    main()
