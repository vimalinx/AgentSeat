#!/usr/bin/env python3
"""Verify Chromium frame callbacks progress without observe on an inactive workspace."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AGENTSEAT = ROOT / "bin/agentseat"
INSTANCE = f"frame-clock-{os.getpid()}"
FIXTURE = ROOT / "tests/frame_clock.html"
CHROMIUM = Path("/usr/bin/chromium")


def command(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"command failed: {argv[0]}")
    return result


def agentseat(*argv: str) -> Any:
    return json.loads(command(str(AGENTSEAT), "--instance", INSTANCE, *argv).stdout)


def host_snapshot() -> dict[str, Any]:
    instances = json.loads(command("hyprctl", "instances", "-j").stdout)
    if not instances:
        raise RuntimeError("host Hyprland instance not found")
    signature = str(instances[0]["instance"])
    workspace = json.loads(command("hyprctl", "--instance", signature, "activeworkspace", "-j").stdout)
    window = json.loads(command("hyprctl", "--instance", signature, "activewindow", "-j").stdout)
    return {"workspace": workspace.get("id"), "window": window.get("address")}


def raf_count(profile: Path) -> int:
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not port_file.is_file():
        time.sleep(0.05)
    if not port_file.is_file():
        raise RuntimeError("Chromium DevTools port did not appear")
    port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
        targets = json.load(response)
    title = next((str(target.get("title", "")) for target in targets if target.get("type") == "page"), "")
    match = re.search(r"AgentSeat RAF (\d+)", title)
    return int(match.group(1)) if match else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=int, default=8)
    parser.add_argument("--wait", type=float, default=2.0)
    args = parser.parse_args()
    if args.workspace <= 0:
        parser.error("workspace must be positive")
    if args.wait < 0.5 or args.wait > 30:
        parser.error("wait must be between 0.5 and 30 seconds")
    if agentseat("status")["running"]:
        raise RuntimeError("refusing to replace an existing AgentSeat session")
    if not CHROMIUM.is_file():
        raise RuntimeError(f"Chromium is unavailable: {CHROMIUM}")

    before = host_snapshot()
    target_workspace = args.workspace if args.workspace != before["workspace"] else args.workspace + 1
    started = False
    result: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="agentseat-frame-clock-") as profile_name:
        profile = Path(profile_name)
        try:
            status = agentseat(
                "run",
                "--x11",
                "--host-workspace",
                str(target_workspace),
                "--",
                str(CHROMIUM),
                f"--user-data-dir={profile}",
                "--remote-debugging-port=0",
                "--no-first-run",
                "--disable-sync",
                "--disable-gpu",
                "--ozone-platform=x11",
                FIXTURE.as_uri(),
            )
            started = True
            time.sleep(args.wait)
            first_count = raf_count(profile)
            time.sleep(0.5)
            count_before_observe = raf_count(profile)
            if first_count < 10 or count_before_observe <= first_count:
                raise RuntimeError(
                    f"background frame callbacks stalled at {first_count} -> {count_before_observe}"
                )
            capture = Path.home() / ".local/state/agentseat/captures/frame-clock.png"
            observed = agentseat("observe", str(capture))
            after = host_snapshot()
            result = {
                "passed": True,
                "target_workspace": target_workspace,
                "host_workspace_unchanged": before["workspace"] == after["workspace"],
                "host_focus_unchanged": before["window"] == after["window"],
                "frames_first_sample": first_count,
                "frames_before_observe": count_before_observe,
                "capture": observed["path"],
                "physical_pixel_layout": status["state"]["physical_pixel_layout"],
            }
        finally:
            if started:
                agentseat("stop")
    result["final_status"] = agentseat("status")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
