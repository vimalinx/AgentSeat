#!/usr/bin/env python3
"""Real private GTK regression for transport, identity, cancellation and input.

Uses only a fresh named AgentSeat instance. The activity test here is explicit
pause, not physical human input; final compositor takeover has a separate test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import statistics
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
        time.sleep(0.01)
    raise AssertionError("condition did not become true before deadline")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=int, default=108)
    args = parser.parse_args()
    out = args.report_dir.resolve()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance = f"p1-control-{os.getpid()}"
    namespace = "app-" + hashlib.sha256(instance.encode()).hexdigest()[:24]
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "agentseat-apps" / namespace
    state = {}
    controller_id = f"control-test:{os.getpid()}"
    os.environ["AGENTSEAT_CONTROLLER_ID"] = controller_id
    report = {"instance": instance, "tests": {}, "scope": "private GTK fixture; explicit pause"}

    def cli(*argv):
        result = subprocess.run(
            [str(ROOT / "bin/agentseat"), "--instance", instance, *argv],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "AGENTSEAT_LANGUAGE": "en"},
        )
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return json.loads(result.stdout)

    def rpc(method, params=None, *, context=True):
        payload = dict(params or {})
        if context:
            payload.setdefault("session_id", state["session_id"])
            payload.setdefault("controller_id", controller_id)
            if method.startswith(("pointer.", "keyboard.")) or method in {"window.focus", "window.resize", "window.move"}:
                lease = rpc("control.acquire", {"ttl_ms": 10000})["result"]
                payload.setdefault("lease_id", lease["lease_id"])
                payload.setdefault("control_epoch", lease["control_epoch"])
            payload.setdefault("request_id", time.monotonic_ns())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(runtime / "control.sock"))
            client.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": payload}) + "\n").encode())
            data = bytearray()
            while b"\n" not in data:
                block = client.recv(65536)
                if not block:
                    break
                data.extend(block)
            return json.loads(bytes(data).split(b"\n")[0])

    def success(method, params=None):
        response = rpc(method, params)
        assert "result" in response, response
        return response["result"]

    def ui():
        return json.loads((out / "ui.json").read_text())

    def kind(response):
        assert isinstance(response["error"]["code"], int), response
        return response["error"]["data"]["kind"]

    try:
        launched = cli(
            "run", "--host-workspace", str(args.workspace), "--", "/usr/bin/env",
            "GSK_RENDERER=cairo", "GTK_A11Y=none", "/usr/bin/python3",
            str(ROOT / "tests/control_fixture.py"), "--state", str(out / "ui.json"),
        )
        state = launched["state"]
        report["launch"] = state
        assert state["host_launch_receipt"]["relation"] != "outer-focused"
        cli("observe", str(out / "before.png"))

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stalled:
            stalled.connect(str(runtime / "control.sock"))
            stalled.sendall(b'{"jsonrpc":')
            time.sleep(0.03)
            began = time.monotonic()
            success("session.heartbeat")
            latency = (time.monotonic() - began) * 1000
            assert latency < 200, latency
            report["tests"]["half_request_heartbeat_ms"] = latency

        # A well-formed object without its transport terminator must not execute.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as unfinished:
            unfinished.connect(str(runtime / "control.sock"))
            unfinished.sendall(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "seat.pause", "params": {"session_id": state["session_id"], "controller_id": controller_id, "request_id": time.monotonic_ns()}}).encode())
        assert not success("seat.status")["seat"]["paused"]

        assert kind(rpc("pointer.move_absolute", {"session_id": "old-session", "x": .5, "y": .13})) == "SESSION_MISMATCH"
        assert kind(rpc("seat.pause", context=False)) == "SESSION_MISMATCH"
        assert kind(rpc("window.resize", {"window_id": "agentseat:root", "width": 500, "height": 400})) == "UNSUPPORTED"
        assert kind(rpc("pointer.move_absolute", {"x": "0.5", "y": .13})) == "INVALID_PARAMS"
        assert kind(rpc("keyboard.type", {"text": "a\0b"})) == "INVALID_PARAMS"
        for method, params in [
            ("keyboard.key", {"keycode": "29", "pressed": True}),
            ("pointer.scroll", {"axis": "vertical", "value120": 4294967296}),
            ("pointer.scroll", {"axis": "vertical", "value120": "120"}),
        ]:
            assert kind(rpc(method, params)) == "INVALID_PARAMS"
        assert kind(rpc("seat.pause", {"session_id": state["session_id"] + "\0suffix"})) == "SESSION_MISMATCH"
        assert kind(rpc("seat.pause\0suffix")) == "INVALID_REQUEST"
        report["tests"]["identity_validation_and_unsupported"] = "PASS"

        old_epoch = success("session.heartbeat")["control_epoch"]
        operation = success("keyboard.type", {"text": "a" * 250, "interval_ms": 10})
        assert operation["execution"] == "running"
        wait_for(lambda: len(ui()["text"]) >= 3)
        began = time.monotonic()
        success("seat.pause")
        wait_for(lambda: (lambda h: not h["gate_allowed"] and h["gate_epoch"] >= h["control_epoch"])(success("session.heartbeat")))
        latency = (time.monotonic() - began) * 1000
        assert latency < 200, latency
        result = success("operation.get", {"operation_id": operation["operation_id"]})
        assert result["execution"] == "cancelled" and result["submitted"] < 250, result
        time.sleep(.1)
        stopped_length = len(ui()["text"])
        time.sleep(.15)
        assert len(ui()["text"]) == stopped_length < 250
        report["tests"]["pause_during_type_ms"] = latency
        report["tests"]["cancelled_operation"] = result
        report["tests"]["characters_after_pause"] = stopped_length
        success("seat.resume")
        assert kind(rpc("pointer.move_absolute", {"control_epoch": old_epoch, "x": .5, "y": .13})) == "STALE_CONTROL"
        before = ui()["text"]
        typed = cli("type", "new")
        assert typed["typed"] == 3
        wait_for(lambda: ui()["text"] == before + "new")
        report["tests"]["fresh_input_after_pause"] = "PASS"

        # Resetting a client with unread output must not kill the daemon.
        for _ in range(20):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as abandoned:
                abandoned.connect(str(runtime / "control.sock"))
                abandoned.sendall(b'{"jsonrpc":"2.0","id":2,"method":"seat.status"}\n')
        success("session.heartbeat")
        report["tests"]["abandoned_clients"] = "PASS"

        samples = {}
        for command in [("click",), ("type", "x"), ("observe", str(out / "after.png"))]:
            latencies = []
            for _ in range(5):
                began = time.monotonic()
                cli(*command)
                latencies.append((time.monotonic() - began) * 1000)
            samples[command[0]] = {"samples_ms": latencies, "median_ms": statistics.median(latencies)}
        report["cli_benchmark"] = samples
        report["resources"] = {}
        for role in ["daemon_pid", "microhost_pid"]:
            fields = {}
            for line in Path(f"/proc/{state[role]}/smaps_rollup").read_text().splitlines():
                if line.startswith(("Rss:", "Pss:")):
                    fields[line.split(":")[0] + "_kib"] = int(line.split()[1])
            report["resources"][role] = fields
        # Exceed the old 15-second whole-call timeout with actual GTK input.
        # The async submit and bounded polls must keep the CLI alive.
        before = ui()["text"]
        began = time.monotonic()
        typed = cli("type", "b" * 4000)
        elapsed = time.monotonic() - began
        assert typed["typed"] == 4000
        wait_for(lambda: ui()["text"] == before + "b" * 4000)
        report["tests"]["long_cli_type"] = {"seconds": elapsed, "actual_added_characters": 4000}
        report["status"] = "PASS"
    except Exception as error:
        report["status"] = "FAIL"
        report["error"] = repr(error)
        raise
    finally:
        try:
            report["cleanup"] = cli("stop")
        finally:
            (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
