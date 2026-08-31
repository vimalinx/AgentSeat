#!/usr/bin/env python3
"""Live resource, latency, boundary, and capture-safety checks for agentseatd."""

from __future__ import annotations

import json
import os
import socket
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AGENTSEAT = ROOT / "bin/agentseat"
AGENTSEATD = ROOT / "bin/agentseatd"
MICROHOST = ROOT / "bin/agentseat-microhost"
LEGACY_INPUT_PROCESS = ROOT / "bin/agentseat-input"
STATE = ROOT / "runtime/state.json"
EVIDENCE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agentseat/captures"
USER_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
SOCKET = USER_RUNTIME_DIR / "agentseat/control.sock"


def command(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result


def agentseat(*argv: str) -> Any:
    return json.loads(command(str(AGENTSEAT), *argv).stdout)


def rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    request = {"jsonrpc": "2.0", "id": time.monotonic_ns(), "method": method, "params": params or {}}
    data = (json.dumps(request, separators=(",", ":")) + "\n").encode()
    received = bytearray()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(SOCKET))
        client.sendall(data)
        while b"\n" not in received:
            block = client.recv(65536)
            if not block:
                break
            received.extend(block)
    return json.loads(bytes(received).split(b"\n", 1)[0])


def executable(pid: int) -> str:
    return os.readlink(f"/proc/{pid}/exe").removesuffix(" (deleted)")


def pids_for_executable(path: Path) -> list[int]:
    matches: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            current = os.readlink(entry / "exe").removesuffix(" (deleted)")
        except OSError:
            continue
        if current == str(path):
            matches.append(int(entry.name))
    return matches


def children(pid: int) -> list[int]:
    path = Path(f"/proc/{pid}/task/{pid}/children")
    return [int(value) for value in path.read_text().split()]


def descendants(pid: int) -> list[int]:
    result: list[int] = []
    pending = [pid]
    while pending:
        current = pending.pop()
        try:
            direct = children(current)
        except OSError:
            direct = []
        result.extend(direct)
        pending.extend(direct)
    return result


def rss_kib(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError(f"VmRSS missing for pid {pid}")


def process_env(pid: int) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            result[os.fsdecode(key)] = os.fsdecode(value)
    return result


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if agentseat("status")["running"]:
        agentseat("stop")

    started = False
    daemon_pid = 0
    microhost_pid = 0
    stamp = time.strftime("%Y%m%dT%H%M%S")
    capture = EVIDENCE / f"native-daemon-safety-{stamp}.png"
    target = EVIDENCE / f"native-daemon-symlink-target-{stamp}.png"
    symlink = EVIDENCE / f"native-daemon-symlink-{stamp}.png"
    try:
        status = agentseat(
            "run", "--", "/usr/bin/zenity", "--info",
            "--title=AgentSeat native daemon acceptance",
            "--text=Native daemon, GUI-only acceptance",
            "--timeout=30",
        )
        started = True
        state = json.loads(STATE.read_text())
        daemon_pid = int(state["daemon_pid"])
        microhost_pid = int(state["microhost_pid"])
        daemon_children = children(daemon_pid)
        assert daemon_children == []
        assert pids_for_executable(LEGACY_INPUT_PROCESS) == []
        assert executable(daemon_pid) == str(AGENTSEATD)
        assert executable(microhost_pid) == str(MICROHOST)
        assert state["backend"] == "cage-microhost"

        nested_display = str(state["wayland_display"])
        assert process_env(daemon_pid).get("WAYLAND_DISPLAY") == nested_display
        assert status["host_now"]["window"] != state["outer_window"]

        version = rpc("version")
        assert version["result"]["implementation"] == "native-c-event-loop"
        latencies_ms: list[float] = []
        for _ in range(300):
            began = time.perf_counter_ns()
            response = rpc("version")
            latencies_ms.append((time.perf_counter_ns() - began) / 1_000_000)
            assert "result" in response

        denied_relative = command(str(AGENTSEAT), "observe", "relative.png", check=False)
        assert denied_relative.returncode != 0 and "absolute .png path" in denied_relative.stderr

        target.write_bytes(b"not a capture")
        symlink.symlink_to(target)
        denied_symlink = command(str(AGENTSEAT), "observe", str(symlink), check=False)
        assert denied_symlink.returncode != 0 and "symbolic link" in denied_symlink.stderr

        captured = agentseat("observe", str(capture))
        assert captured["capture"]["backend"] == "native-wlr-screencopy"
        assert capture.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

        xwayland_pids = [pid for pid in descendants(microhost_pid) if Path(executable(pid)).name == "Xwayland"]
        assert len(xwayland_pids) <= 1
        xwayland_pid = xwayland_pids[0] if xwayland_pids else 0
        result = {
            "passed": True,
            "daemon": {
                "pid": daemon_pid,
                "exe": executable(daemon_pid),
                "rss_kib": rss_kib(daemon_pid),
            },
            "input": {"embedded_in_daemon": True, "legacy_processes": pids_for_executable(LEGACY_INPUT_PROCESS)},
            "microhost": {
                "pid": microhost_pid,
                "exe": executable(microhost_pid),
                "rss_kib": rss_kib(microhost_pid),
            },
            "xwayland": {"lazy": xwayland_pid == 0, "pid": xwayland_pid, "rss_kib": rss_kib(xwayland_pid) if xwayland_pid else 0},
            "resident_scaffold_rss_kib": rss_kib(daemon_pid) + rss_kib(microhost_pid) + (rss_kib(xwayland_pid) if xwayland_pid else 0),
            "rpc_version_300": {
                "median_ms": round(statistics.median(latencies_ms), 4),
                "p95_ms": round(percentile(latencies_ms, 0.95), 4),
                "max_ms": round(max(latencies_ms), 4),
            },
            "wayland_display": nested_display,
            "relative_capture_denied": True,
            "symlink_capture_denied": True,
            "capture": captured["capture"],
            "host_outer_window_never_active": True,
        }
    finally:
        if symlink.is_symlink():
            symlink.unlink()
        if target.exists():
            target.unlink()
        if started:
            agentseat("stop")

    assert not SOCKET.exists()
    assert daemon_pid == 0 or not Path(f"/proc/{daemon_pid}").exists()
    assert microhost_pid == 0 or not Path(f"/proc/{microhost_pid}").exists()
    result["cleanup"] = {"socket_removed": True, "daemon_stopped": True, "microhost_stopped": True}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
