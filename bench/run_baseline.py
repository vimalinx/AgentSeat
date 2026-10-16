#!/usr/bin/env python3
"""Measure an AgentSeat performance baseline against a real GUI fixture.

Launches tests/control_fixture.py in a private instance on a host workspace
(default 20, away from the user's screens), measures latency and idle resource
use, writes baseline.json and stops the instance. Needs a Hyprland session.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLOCK_TICKS = os.sysconf("SC_CLK_TCK")


def summary(samples_ms):
    ordered = sorted(samples_ms)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]
    return {"n": len(ordered), "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(pick(0.95), 2), "max_ms": round(ordered[-1], 2)}


def cpu_seconds(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / CLOCK_TICKS


def rss_kib(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--workspace", type=int, default=20)
    parser.add_argument("--cli", type=Path, default=ROOT / "bin/agentseat")
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    out = args.report_dir.resolve()
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    instance = f"bench-{os.getpid()}"
    report = {"method_version": 1, "cli": str(args.cli), "host": platform.node(),
              "kernel": platform.release(), "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "metrics": {}}

    def cli(*argv):
        started = time.monotonic()
        result = subprocess.run([str(args.cli), "--instance", instance, "--controller", "bench", *argv],
                                capture_output=True, text=True, timeout=120)
        elapsed = (time.monotonic() - started) * 1000
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return json.loads(result.stdout), elapsed

    def ui_text():
        try:
            return json.loads((out / "ui.json").read_text())["text"]
        except (OSError, ValueError, KeyError):
            return None

    def wait_text(predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = ui_text()
            if text is not None and predicate(text):
                return
            time.sleep(0.002)
        raise RuntimeError("fixture text did not update")

    try:
        started = time.monotonic()
        launched, _ = cli("run", "--host-workspace", str(args.workspace), "--",
                          "/usr/bin/python3", str(ROOT / "tests/control_fixture.py"), "--state", str(out / "ui.json"))
        report["metrics"]["launch_ms"] = round((time.monotonic() - started) * 1000, 1)
        report["text_lane"] = launched["state"]["launch"].get("ime")
        heartbeat, _ = cli("heartbeat")
        report["output"] = {k: heartbeat["daemon"]["output"].get(k) for k in ("width", "height", "pixel_width", "pixel_height", "scale")}
        wait_text(lambda text: True)
        time.sleep(1.0)

        report["metrics"]["heartbeat_cli"] = summary([cli("heartbeat")[1] for _ in range(args.samples)])
        observe = []
        frame = []
        for index in range(args.samples):
            result, elapsed = cli("observe", str(out / f"frame-{index % 2}.png"))
            observe.append(elapsed)
            capture = result["capture"]
            if capture.get("presented_ns") and capture.get("submitted_ns"):
                frame.append((capture["presented_ns"] - capture["submitted_ns"]) / 1e6)
        report["metrics"]["observe_cli"] = summary(observe)
        if frame:
            report["metrics"]["capture_submit_to_present"] = summary(frame)

        # Text appears in the application: one short word per sample.
        typed = []
        for _ in range(args.samples):
            before = len(ui_text())
            started = time.monotonic()
            cli("type", "bench")
            wait_text(lambda text: len(text) >= before + 5)
            typed.append((time.monotonic() - started) * 1000)
        report["metrics"]["type_5_chars_to_app"] = summary(typed)
        long_text = "x" * 2048
        before = len(ui_text())
        started = time.monotonic()
        cli("type", long_text)
        wait_text(lambda text: len(text) >= before + len(long_text), timeout=60)
        report["metrics"]["type_2048_chars_to_app_ms"] = round((time.monotonic() - started) * 1000, 1)

        detect = []
        for _ in range(min(args.samples, 10)):
            proc = subprocess.Popen([str(args.cli), "--instance", instance, "--controller", "bench-wait",
                                     "wait-change", "--timeout-ms", "5000"], stdout=subprocess.PIPE, text=True)
            time.sleep(0.4)
            cli("type", "w")
            result = json.loads(proc.communicate(timeout=10)[0])
            if result["status"] == "changed":
                detect.append(result["elapsed_ms"] - 400)
        if detect:
            report["metrics"]["wait_change_after_type"] = summary(detect)

        # Idle cost with the fixture's caret blinking.
        heartbeat, _ = cli("heartbeat")
        pids = {"microhost": heartbeat["microhost_pid"], "daemon": heartbeat["daemon"]["daemon_pid"]}
        time.sleep(12)  # let GTK stop caret blinking after its timeout
        before = {name: cpu_seconds(pid) for name, pid in pids.items()}
        time.sleep(10)
        report["metrics"]["idle"] = {name: {
            "cpu_percent": round((cpu_seconds(pid) - before[name]) / 10 * 100, 2),
            "rss_kib": rss_kib(pid)} for name, pid in pids.items()}
        report["result"] = "COMPLETE"
    except Exception as exc:
        report["result"] = "FAILED"
        report["failure"] = repr(exc)
        raise
    finally:
        try:
            cli("stop")
        except Exception as exc:
            report["stop_error"] = str(exc)
        for index in range(2):
            (out / f"frame-{index}.png").unlink(missing_ok=True)
        (out / "baseline.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
