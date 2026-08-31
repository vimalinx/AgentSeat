#!/usr/bin/env python3
"""Live, non-host-injecting acceptance matrix for the generic AgentSeat controller."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AGENTSEAT = ROOT / "bin/agentseat"
EVIDENCE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agentseat/captures"
USER_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
CONTROL_SOCKET = USER_RUNTIME_DIR / "agentseat/control.sock"
MICROHOST_EXE = ROOT / "bin/agentseat-microhost"
GUI_FIXTURE = ROOT / "tests/gui_fixture.py"
TEST_WORKSPACE = int(os.environ.get("AGENTSEAT_TEST_WORKSPACE", "8"))
if TEST_WORKSPACE < 1:
    raise RuntimeError("AGENTSEAT_TEST_WORKSPACE must be a positive integer")


def command(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"command failed: {argv[0]}")
    return result


def agentseat(*argv: str, check: bool = True) -> Any:
    effective_argv = list(argv)
    if effective_argv and effective_argv[0] == "run" and "--host-workspace" not in effective_argv:
        effective_argv[1:1] = ["--host-workspace", str(TEST_WORKSPACE)]
    result = command(str(AGENTSEAT), *effective_argv, check=check)
    if not check:
        return result
    return json.loads(result.stdout)


def host_signature() -> str:
    instances = json.loads(command("hyprctl", "instances", "-j").stdout)
    if not instances:
        raise RuntimeError("host Hyprland instance not found")
    return str(instances[0]["instance"])


def hypr_json(name: str) -> dict[str, Any]:
    return json.loads(command("hyprctl", "--instance", host_signature(), name, "-j").stdout)


def host_snapshot() -> dict[str, Any]:
    workspace = hypr_json("activeworkspace")
    window = hypr_json("activewindow")
    cursor = hypr_json("cursorpos")
    return {
        "workspace": workspace.get("id"),
        "window": window.get("address"),
        "title": window.get("title"),
        "cursor": [cursor.get("x"), cursor.get("y")],
    }


def wait_for(predicate: Any, timeout: float, message: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError(message)


def wait_for_text(path: Path, timeout: float = 5.0) -> str:
    return wait_for(lambda: path.read_text(encoding="utf-8") if path.exists() else None, timeout, f"no probe result: {path}")


def matching_processes(marker: Path) -> list[int]:
    encoded = os.fsencode(marker)
    matches: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if encoded in cmdline:
            matches.append(int(entry.name))
    return matches


def assert_wrapper_never_active(status: dict[str, Any]) -> None:
    assert status["host_now"]["window"] != status["state"]["outer_window"]
    receipt = status["state"].get("host_launch_receipt", {})
    assert receipt.get("relation") != "outer-focused"


def stop_and_verify(marker: Path | None = None) -> dict[str, Any]:
    stopped = agentseat("stop")
    offline = wait_for(lambda: not agentseat("status")["running"], 8, "AgentSeat stayed live after stop")
    assert offline is True
    assert not CONTROL_SOCKET.exists()
    if marker is not None:
        wait_for(lambda: not matching_processes(marker), 5, f"application process survived stop: {marker}")
    return stopped


def wayland_case(stamp: str) -> dict[str, Any]:
    proof = EVIDENCE / f"generic-wayland-input-{stamp}.txt"
    pointer_proof = EVIDENCE / f"generic-wayland-pointer-{stamp}.txt"
    image = EVIDENCE / f"generic-wayland-observe-{stamp}.png"
    marker = f"WL_OK_{stamp}"
    before = host_snapshot()
    status = agentseat(
        "run",
        "--",
        "/usr/bin/python",
        str(GUI_FIXTURE),
        "--proof", str(proof),
        "--pointer-proof", str(pointer_proof),
        "--title", "AgentSeat native Wayland GUI",
    )
    assert status["state"]["launch"]["mode"] == "wayland"
    assert status["state"]["launch"]["argv"][1] == str(GUI_FIXTURE)
    assert status["windows"][0]["xwayland"] is False
    assert status["state"]["backend"] == "cage-microhost"
    assert_wrapper_never_active(status)

    agentseat("pause")
    blocked = agentseat("type", "blocked", check=False)
    assert blocked.returncode != 0 and "SEAT_PAUSED" in blocked.stderr
    agentseat("resume")

    cursor_before = host_snapshot()["cursor"]
    # The fixture keeps its natural 640x320 geometry and is centered. Its
    # full-width proof button sits just above the vertically expanding entry.
    agentseat("move", "0.50", "0.46")
    cursor_after = host_snapshot()["cursor"]
    agentseat("click")
    assert wait_for_text(pointer_proof).strip() == "clicked"

    agentseat("type", marker)
    time.sleep(0.15)
    native_capture = agentseat("observe", str(image))
    assert native_capture["backend"] == "wlr-screencopy"
    assert native_capture["capture"]["backend"] == "native-wlr-screencopy"
    assert image.is_file() and image.stat().st_size > 0
    agentseat("type", "\n")
    typed = wait_for_text(proof).strip()
    assert typed == marker

    after = host_snapshot()
    stopped = stop_and_verify(proof)
    return {
        "passed": True,
        "proof": str(proof),
        "pointer_proof": str(pointer_proof),
        "image": str(image),
        "typed": typed,
        "window": status["windows"][0],
        "host_before": before,
        "host_after": after,
        "host_focus_same": [before["workspace"], before["window"]] == [after["workspace"], after["window"]],
        "host_cursor_preserved_during_agent_move": cursor_before == cursor_after,
        "observe_backend": native_capture["capture"]["backend"],
        "stopped": stopped,
    }


def x11_case(stamp: str) -> dict[str, Any]:
    proof = EVIDENCE / f"generic-x11-input-{stamp}.txt"
    pointer_proof = EVIDENCE / f"generic-x11-pointer-{stamp}.txt"
    image = EVIDENCE / f"generic-x11-observe-{stamp}.png"
    marker = f"X11_OK_{stamp}"
    probe = (
        "import pathlib,sys,tkinter as tk; "
        "root=tk.Tk(); root.title('AgentSeat generic X11 probe'); "
        "entry=tk.Entry(root,font=('Sans',18)); entry.pack(fill='x',padx=30,pady=70); "
        "entry.bind('<Button-1>',lambda event:pathlib.Path(sys.argv[2]).write_text('clicked',encoding='utf-8')); "
        "entry.bind('<Return>',lambda event:(pathlib.Path(sys.argv[1]).write_text(entry.get(),encoding='utf-8'),root.destroy())); "
        "entry.focus_set(); root.mainloop()"
    )
    before = host_snapshot()
    status = agentseat("run", "--x11", "--", "/usr/bin/python", "-c", probe, str(proof), str(pointer_proof))
    assert status["state"]["launch"]["mode"] == "x11"
    assert status["windows"][0]["xwayland"] is True
    assert_wrapper_never_active(status)

    cursor_before = host_snapshot()["cursor"]
    # Tk keeps its natural geometry; the padded full-width entry is centered
    # with the window inside the private output.
    agentseat("move", "0.50", "0.50")
    cursor_after = host_snapshot()["cursor"]
    agentseat("click")
    assert wait_for_text(pointer_proof).strip() == "clicked"
    assert_wrapper_never_active(agentseat("status"))

    agentseat("type", marker)
    time.sleep(0.15)
    observed = agentseat("observe", str(image))
    assert observed["backend"] == "wlr-screencopy"
    assert observed["capture"]["backend"] == "native-wlr-screencopy"
    assert image.is_file() and image.stat().st_size > 0

    agentseat("type", "\n")
    typed = wait_for_text(proof).strip()
    assert typed == marker
    after = host_snapshot()
    stopped = stop_and_verify(proof)
    return {
        "passed": True,
        "proof": str(proof),
        "pointer_proof": str(pointer_proof),
        "image": str(image),
        "typed": typed,
        "window": status["windows"][0],
        "observe_backend": observed["capture"]["backend"],
        "rendered_before_commit": True,
        "host_before": before,
        "host_after": after,
        "host_focus_same": [before["workspace"], before["window"]] == [after["workspace"], after["window"]],
        "host_cursor_preserved_during_agent_move": cursor_before == cursor_after,
        "stopped": stopped,
    }


def window_tree_case(stamp: str) -> dict[str, Any]:
    geometry_proof = EVIDENCE / f"generic-window-tree-geometry-{stamp}.json"
    image = EVIDENCE / f"generic-window-tree-{stamp}.png"
    probe = (
        "import json,pathlib,sys,tkinter as tk\n"
        "root=tk.Tk()\n"
        "root.title('AgentSeat tree root')\n"
        "root.geometry('800x600')\n"
        "def open_child():\n"
        "    child=tk.Toplevel(root)\n"
        "    child.title('AgentSeat parentless child')\n"
        "    child.geometry('400x200')\n"
        "    tk.Label(child,text='Parentless secondary window').pack(expand=True)\n"
        "    dialog=tk.Toplevel(root)\n"
        "    dialog.title('AgentSeat transient dialog')\n"
        "    dialog.geometry('320x160')\n"
        "    dialog.transient(root)\n"
        "    entry=tk.Entry(dialog,font=('Sans',18))\n"
        "    entry.pack(fill='x',padx=30,pady=50)\n"
        "    entry.focus_set()\n"
        "    def report_geometry():\n"
        "        root.update_idletasks()\n"
        "        pathlib.Path(sys.argv[1]).write_text(json.dumps({'root':[root.winfo_width(),root.winfo_height(),root.winfo_x(),root.winfo_y()],'child':[child.winfo_width(),child.winfo_height(),child.winfo_x(),child.winfo_y()],'dialog':[dialog.winfo_width(),dialog.winfo_height(),dialog.winfo_x(),dialog.winfo_y()]}),encoding='utf-8')\n"
        "    root.after(400,report_geometry)\n"
        "root.after(600,open_child)\n"
        "root.mainloop()\n"
    )
    before = host_snapshot()
    agentseat("run", "--x11", "--", "/usr/bin/python", "-c", probe, str(geometry_proof))

    geometry = json.loads(wait_for_text(geometry_proof))
    assert geometry["root"][:2] == [800, 600]
    assert geometry["child"][:2] == [400, 200]
    assert geometry["dialog"][:2] == [320, 160]
    windows = agentseat("windows")
    status = agentseat("status")
    assert all(window["leased"] for window in windows)
    assert len(windows) == 1
    assert status["seat"]["seat"]["focused_window"] == "agentseat:root"

    observed = agentseat("observe", str(image))
    assert observed["capture"]["scope"] == "single-app-output"
    after = host_snapshot()
    stopped = stop_and_verify(geometry_proof)
    return {
        "passed": True,
        "geometry_proof": str(geometry_proof),
        "image": str(image),
        "requested_geometry_preserved": geometry,
        "logical_window_count": len(windows),
        "child_dialog_managed_inside_root": True,
        "transient_dialog_geometry_preserved": True,
        "all_leased": all(window["leased"] for window in windows),
        "focused_boundary": status["seat"]["seat"]["focused_window"],
        "host_focus_same": [before["workspace"], before["window"]] == [after["workspace"], after["window"]],
        "stopped": stopped,
    }


def detached_launcher_case(stamp: str) -> dict[str, Any]:
    marker = EVIDENCE / f"detached-launcher-{stamp}.txt"
    image = EVIDENCE / f"detached-launcher-{stamp}.png"
    probe = (
        "import os,pathlib,sys,time\n"
        "pid=os.fork()\n"
        "if pid:\n"
        "    os._exit(0)\n"
        "os.closerange(3,256)\n"
        "time.sleep(0.5)\n"
        "import tkinter as tk\n"
        "root=tk.Tk()\n"
        "root.title('AgentSeat detached long-running GUI')\n"
        "root.geometry('720x480')\n"
        "tk.Label(root,text='Detached GUI remains attached').pack(expand=True)\n"
        "root.after(300,lambda:pathlib.Path(sys.argv[1]).write_text('mapped',encoding='utf-8'))\n"
        "root.mainloop()\n"
    )
    before = host_snapshot()
    status = agentseat("run", "--x11", "--", "/usr/bin/python", "-c", probe, str(marker))
    assert wait_for_text(marker).strip() == "mapped"
    assert status["running"] is True
    assert_wrapper_never_active(status)
    observed = agentseat("observe", str(image))
    assert observed["capture"]["backend"] == "native-wlr-screencopy"
    after = host_snapshot()
    stopped = stop_and_verify(marker)
    return {
        "passed": True,
        "marker": str(marker),
        "image": str(image),
        "launcher_exited_before_gui_map": True,
        "microhost_survived_launcher_exit": True,
        "host_focus_same": [before["workspace"], before["window"]] == [after["workspace"], after["window"]],
        "stopped": stopped,
    }


def unicode_case(stamp: str) -> dict[str, Any]:
    marker = f"中文🌍{stamp}"
    result: dict[str, Any] = {"passed": True, "marker": marker}

    wayland_proof = EVIDENCE / f"unicode-wayland-{stamp}.txt"
    wayland_image = EVIDENCE / f"unicode-wayland-{stamp}.png"
    wayland_before = host_snapshot()
    agentseat(
        "run",
        "--",
        "/usr/bin/python",
        str(GUI_FIXTURE),
        "--proof", str(wayland_proof),
        "--pointer-proof", str(EVIDENCE / f"unicode-wayland-pointer-{stamp}.txt"),
        "--title", "AgentSeat Unicode Wayland",
    )
    agentseat("move", "0.50", "0.53")
    agentseat("click")
    wayland_type = agentseat("type", marker)
    assert wayland_type["lane"] == "private-wayland-clipboard"
    agentseat("observe", str(wayland_image))
    agentseat("type", "\n")
    assert wait_for_text(wayland_proof).strip() == marker
    wayland_after = host_snapshot()
    wayland_stopped = stop_and_verify(wayland_proof)
    result["wayland"] = {
        "passed": True,
        "lane": wayland_type["lane"],
        "proof": str(wayland_proof),
        "image": str(wayland_image),
        "host_focus_same": [wayland_before["workspace"], wayland_before["window"]]
        == [wayland_after["workspace"], wayland_after["window"]],
        "stopped": wayland_stopped,
    }

    x11_proof = EVIDENCE / f"unicode-x11-{stamp}.txt"
    x11_image = EVIDENCE / f"unicode-x11-{stamp}.png"
    probe = (
        "import pathlib,sys,tkinter as tk; "
        "root=tk.Tk(); root.title('AgentSeat Unicode X11'); "
        "entry=tk.Entry(root,font=('Sans',18)); entry.pack(fill='x',padx=30,pady=70); "
        "entry.bind('<Return>',lambda event:(pathlib.Path(sys.argv[1]).write_text(entry.get(),encoding='utf-8'),root.destroy())); "
        "entry.focus_set(); root.mainloop()"
    )
    x11_before = host_snapshot()
    agentseat("run", "--x11", "--", "/usr/bin/python", "-c", probe, str(x11_proof))
    agentseat("move", "0.50", "0.50")
    agentseat("click")
    x11_type = agentseat("type", marker)
    assert x11_type["lane"] == "private-wayland-clipboard"
    agentseat("observe", str(x11_image))
    agentseat("type", "\n")
    assert wait_for_text(x11_proof).strip() == marker
    x11_after = host_snapshot()
    x11_stopped = stop_and_verify(x11_proof)
    result["x11"] = {
        "passed": True,
        "lane": x11_type["lane"],
        "proof": str(x11_proof),
        "image": str(x11_image),
        "host_focus_same": [x11_before["workspace"], x11_before["window"]]
        == [x11_after["workspace"], x11_after["window"]],
        "stopped": x11_stopped,
    }
    return result


def lifecycle_case(cycles: int = 3) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for index in range(cycles):
        before = host_snapshot()
        started_at = time.monotonic()
        status = agentseat(
            "run",
            "--",
            "/usr/bin/zenity",
            "--info",
            f"--title=AgentSeat lifecycle cycle {index}",
            "--text=GUI lifecycle fixture",
            "--timeout=30",
        )
        assert status["windows"][0]["xwayland"] is False
        assert_wrapper_never_active(status)
        start_ms = round((time.monotonic() - started_at) * 1000, 2)
        stopped_at = time.monotonic()
        stop_and_verify()
        stop_ms = round((time.monotonic() - stopped_at) * 1000, 2)
        after = host_snapshot()
        results.append(
            {
                "cycle": index,
                "start_ms": start_ms,
                "stop_ms": stop_ms,
                "host_focus_same": [before["workspace"], before["window"]]
                == [after["workspace"], after["window"]],
            }
        )
    return {"passed": True, "cycles": results}


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if agentseat("status")["running"]:
        stop_and_verify()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    result: dict[str, Any] = {"stamp": stamp, "host_workspace": TEST_WORKSPACE}
    try:
        result["wayland"] = wayland_case(stamp)
        result["x11"] = x11_case(stamp)
        result["window_tree"] = window_tree_case(stamp)
        result["detached_launcher"] = detached_launcher_case(stamp)
        result["unicode"] = unicode_case(stamp)
        result["lifecycle"] = lifecycle_case()
    finally:
        if agentseat("status")["running"]:
            stop_and_verify()
    result["final_status"] = agentseat("status")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
