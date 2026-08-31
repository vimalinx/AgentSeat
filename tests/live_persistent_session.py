#!/usr/bin/env python3
"""Live soak for reconnecting to one long-running AgentSeat GUI session."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AGENTSEAT = ROOT / "bin/agentseat"
GUI_FIXTURE = ROOT / "tests/gui_fixture.py"
CAPTURES = Path.home() / ".local/state/agentseat/captures"


def command(*argv: str, check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"command failed: {argv[0]}")
    return result


def agentseat(*argv: str) -> Any:
    return json.loads(command(str(AGENTSEAT), *argv).stdout)


def host_signature() -> str:
    instances = json.loads(command("hyprctl", "instances", "-j").stdout)
    if not instances:
        raise RuntimeError("host Hyprland instance not found")
    return str(instances[0]["instance"])


def host_snapshot() -> dict[str, Any]:
    signature = host_signature()
    workspace = json.loads(command("hyprctl", "--instance", signature, "activeworkspace", "-j").stdout)
    window = json.loads(command("hyprctl", "--instance", signature, "activewindow", "-j").stdout)
    cursor = json.loads(command("hyprctl", "--instance", signature, "cursorpos", "-j").stdout)
    return {
        "workspace": workspace.get("id"),
        "window": window.get("address"),
        "title": window.get("title"),
        "cursor": [cursor.get("x"), cursor.get("y")],
    }


def wait_for_text(path: Path, timeout: float = 5.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8")
        time.sleep(0.05)
    raise RuntimeError(f"application proof did not appear: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=60.0, help="minimum soak duration in seconds")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between reconnect checks")
    parser.add_argument("--workspace", type=int, default=8, help="inactive host workspace for the outer window")
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration < 1 or args.duration > 86_400:
        parser.error("duration must be between 1 and 86400 seconds")
    if not math.isfinite(args.interval) or args.interval < 0.1 or args.interval > 3600:
        parser.error("interval must be between 0.1 and 3600 seconds")
    if args.workspace <= 0:
        parser.error("workspace must be a positive integer")
    if agentseat("status")["running"]:
        raise RuntimeError("refusing to replace an existing AgentSeat session")

    CAPTURES.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    proof = CAPTURES / f"persistent-session-input-{stamp}.txt"
    pointer_proof = CAPTURES / f"persistent-session-pointer-{stamp}.txt"
    before_image = CAPTURES / f"persistent-session-before-{stamp}.png"
    after_image = CAPTURES / f"persistent-session-after-{stamp}.png"
    marker = f"PERSISTENT_OK_{stamp}"
    host_before = host_snapshot()
    started = False
    result: dict[str, Any] = {}
    try:
        status = agentseat(
            "run",
            "--host-workspace",
            str(args.workspace),
            "--",
            "/usr/bin/python",
            str(GUI_FIXTURE),
            "--proof",
            str(proof),
            "--pointer-proof",
            str(pointer_proof),
            "--title",
            "AgentSeat persistent reconnect soak",
        )
        started = True
        session_id = str(status["state"]["session_id"])
        daemon_pid = int(status["state"]["daemon_pid"])
        microhost_pid = int(status["state"]["microhost_pid"])
        outer_window = str(status["state"]["outer_window"])
        assert status["host_now"]["window"] != outer_window

        initial = agentseat("heartbeat", "--expect-session", session_id)
        # The first mapped buffer can precede the toolkit's fully painted
        # frame. Let that initial frame settle before retaining visual proof.
        time.sleep(0.5)
        agentseat("observe", str(before_image))
        sample_count = math.ceil(args.duration / args.interval) + 1
        began = time.monotonic()
        watch = command(
            str(AGENTSEAT),
            "watch",
            "--interval",
            str(args.interval),
            "--count",
            str(sample_count),
            "--expect-session",
            session_id,
            timeout=args.duration + args.interval + 30,
        )
        elapsed = time.monotonic() - began
        samples = [json.loads(line) for line in watch.stdout.splitlines() if line.strip()]
        assert len(samples) == sample_count
        assert elapsed >= args.duration
        assert all(sample["healthy"] and sample["session_id"] == session_id for sample in samples)
        assert all(int(sample["daemon_pid"]) == daemon_pid for sample in samples)
        assert all(int(sample["microhost_pid"]) == microhost_pid for sample in samples)

        reconnected = agentseat("heartbeat", "--expect-session", session_id)
        agentseat("observe", str(after_image))
        # The fitted 640x320 output places the expanding entry in its lower
        # half, centered at about 65% of the output height.
        agentseat("move", "0.50", "0.65")
        agentseat("click")
        typed = agentseat("type", marker)
        agentseat("type", "\n")
        assert wait_for_text(proof).strip() == marker
        host_after = host_snapshot()
        assert host_after["window"] != outer_window
        result = {
            "passed": True,
            "session_id": session_id,
            "duration_seconds": round(elapsed, 3),
            "heartbeat_samples": len(samples),
            "same_daemon_pid": int(reconnected["daemon"]["daemon_pid"]) == daemon_pid,
            "same_microhost_pid": int(reconnected["microhost_pid"]) == microhost_pid,
            "fresh_connection_per_heartbeat": initial["connection"] == "fresh-unix-socket-per-request",
            "usable_after_soak": typed.get("typed") == len(marker),
            "host_outer_window_never_active": True,
            "host_before": host_before,
            "host_after": host_after,
            "before_image": str(before_image),
            "after_image": str(after_image),
            "proof": str(proof),
        }
    finally:
        if started:
            agentseat("stop")
    result["final_status"] = agentseat("status")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
