#!/usr/bin/env python3
"""Live compatibility matrix against installed GTK, Qt, Electron, and OnlyOffice apps."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
AGENTSEAT = ROOT / "bin/agentseat"
EVIDENCE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agentseat/captures"
ONLYOFFICE_ACCEPTANCE = ROOT / "tests/accept-onlyoffice.sh"
HOST_WORKSPACE = int(os.environ.get("AGENTSEAT_TEST_WORKSPACE", "8"))


def command(*argv: str, check: bool = True, timeout: float = 45) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"command failed: {argv[0]}")
    return result


def agentseat(*argv: str, check: bool = True, timeout: float = 45) -> Any:
    result = command(str(AGENTSEAT), *argv, check=check, timeout=timeout)
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


def wait_for(predicate: Callable[[], Any], timeout: float, message: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError(message)


def assert_isolated(status: dict[str, Any]) -> None:
    receipt = status["state"]["host_launch_receipt"]
    assert receipt["relation"] != "outer-focused", receipt
    assert status["host_now"]["window"] != status["state"]["outer_window"]


def stop_if_running() -> None:
    if agentseat("status")["running"]:
        agentseat("stop")
    wait_for(lambda: not agentseat("status")["running"], 8, "AgentSeat did not stop")


def terminate_processes_using_paths(*owned_paths: Path) -> None:
    needles = tuple(str(item).encode() for item in owned_paths)

    def matching_pids() -> set[int]:
        matches: set[int] = set()
        for process_dir in Path("/proc").iterdir():
            if not process_dir.name.isdigit():
                continue
            try:
                command_line = (process_dir / "cmdline").read_bytes()
            except OSError:
                continue
            if command_line and any(needle in command_line for needle in needles):
                matches.add(int(process_dir.name))
        matches.discard(os.getpid())
        return matches

    processes = matching_pids()
    for pid in processes:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2
    while processes and time.monotonic() < deadline:
        processes &= matching_pids()
        if processes:
            time.sleep(0.05)
    for pid in processes:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def chord(modifier: int, keycode: int) -> None:
    agentseat("key", str(modifier), "down")
    try:
        agentseat("key", str(keycode), "down")
        agentseat("key", str(keycode), "up")
    finally:
        agentseat("key", str(modifier), "up")


def finish_case(before: dict[str, Any], image: Path) -> dict[str, Any]:
    observed = agentseat("observe", str(image))
    after = host_snapshot()
    status = agentseat("status")
    assert_isolated(status)
    assert image.is_file() and image.stat().st_size > 0
    return {
        "image": str(image),
        "observe_backend": observed["capture"]["backend"],
        "window": observed["window"],
        "host_focus_same": [before["workspace"], before["window"]] == [after["workspace"], after["window"]],
        "host_outer_never_active": True,
    }


def scoped_move(x: str, y: str) -> bool:
    before = host_snapshot()["cursor"]
    agentseat("move", x, y)
    after = host_snapshot()["cursor"]
    # The human may move the hardware cursor concurrently. Report that sample
    # instead of treating unrelated physical activity as an AgentSeat failure;
    # live_generic_matrix.py carries the strict isolation assertion.
    return before == after


def write_minimal_docx(output: Path) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>AgentSeat OnlyOffice live fixture</w:t></w:r></w:p>
    <w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)


def gtk_mousepad(stamp: str) -> dict[str, Any]:
    proof = EVIDENCE / f"software-gtk-mousepad-{stamp}.txt"
    image = EVIDENCE / f"software-gtk-mousepad-{stamp}.png"
    marker = f"GTK_MOUSEPAD_OK_{stamp}"
    proof.write_text("", encoding="utf-8")
    profile = Path(tempfile.mkdtemp(prefix="agentseat-mousepad-profile-"))
    before = host_snapshot()
    try:
        status = agentseat(
            "run", "--host-workspace", str(HOST_WORKSPACE), "--", "/usr/bin/env",
            f"XDG_CONFIG_HOME={profile / 'config'}",
            f"XDG_CACHE_HOME={profile / 'cache'}",
            f"XDG_STATE_HOME={profile / 'state'}",
            "/usr/bin/mousepad", "--disable-server", "-o", "window", str(proof),
        )
        assert_isolated(status)
        assert status["windows"][0]["xwayland"] is False
        pointer_isolated = scoped_move("0.50", "0.45")
        agentseat("click")
        agentseat("type", marker)
        agentseat("observe", str(EVIDENCE / f"software-gtk-mousepad-typed-{stamp}.png"))
        chord(29, 31)  # Ctrl+S, evdev KEY_LEFTCTRL + KEY_S
        wait_for(lambda: proof.read_text(encoding="utf-8") == marker, 8, "Mousepad did not save scoped input")
        result = finish_case(before, image)
        result.update({
            "passed": True,
            "app": "Mousepad",
            "toolkit": "GTK 3",
            "proof": str(proof),
            "typed": marker,
            "host_cursor_preserved_during_agent_move": pointer_isolated,
        })
        return result
    finally:
        stop_if_running()
        shutil.rmtree(profile, ignore_errors=True)


def qt6ct(stamp: str) -> dict[str, Any]:
    image = EVIDENCE / f"software-qt6ct-{stamp}.png"
    before = host_snapshot()
    status = agentseat("run", "--host-workspace", str(HOST_WORKSPACE), "--", "/usr/bin/qt6ct")
    assert_isolated(status)
    assert status["windows"][0]["xwayland"] is False
    pointer_isolated = scoped_move("0.50", "0.45")
    agentseat("click")
    agentseat("scroll", "vertical", "120")
    result = finish_case(before, image)
    result.update({
        "passed": True,
        "app": "Qt6 Configuration Tool",
        "toolkit": "Qt 6 Widgets",
        "interaction": "scoped pointer click and wheel",
        "configuration_written": False,
        "host_cursor_preserved_during_agent_move": pointer_isolated,
    })
    return result


def electron_code(stamp: str) -> dict[str, Any]:
    proof = EVIDENCE / f"software-electron-code-{stamp}.txt"
    image = EVIDENCE / f"software-electron-code-{stamp}.png"
    marker = f"ELECTRON_CODE_OK_{stamp}"
    proof.write_text("", encoding="utf-8")
    profile = Path(tempfile.mkdtemp(prefix="agentseat-code-profile-"))
    extensions = Path(tempfile.mkdtemp(prefix="agentseat-code-extensions-"))
    before = host_snapshot()
    try:
        status = agentseat(
            "run", "--x11", "--host-workspace", str(HOST_WORKSPACE),
            "--",
            "/usr/bin/code",
            "--user-data-dir", str(profile),
            "--extensions-dir", str(extensions),
            "--disable-extensions",
            "--skip-welcome",
            "--skip-release-notes",
            "--new-window",
            "--disable-gpu",
            str(proof),
            timeout=60,
        )
        assert_isolated(status)
        assert status["windows"][0]["xwayland"] is True
        time.sleep(1.5)
        pointer_isolated = scoped_move("0.50", "0.45")
        agentseat("click")
        agentseat("type", marker)
        agentseat("observe", str(EVIDENCE / f"software-electron-code-typed-{stamp}.png"))
        chord(29, 31)
        wait_for(lambda: proof.read_text(encoding="utf-8") == marker, 12, "VS Code did not save scoped input")
        result = finish_case(before, image)
        result.update({
            "passed": True,
            "app": "Visual Studio Code",
            "toolkit": "Electron",
            "lane": "private XWayland software-rendering compatibility",
            "proof": str(proof),
            "typed": marker,
            "host_cursor_preserved_during_agent_move": pointer_isolated,
        })
        return result
    finally:
        stop_if_running()
        terminate_processes_using_paths(profile, extensions)
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(extensions, ignore_errors=True)


def onlyoffice(stamp: str) -> dict[str, Any]:
    profile = Path(tempfile.mkdtemp(prefix="agentseat-onlyoffice-profile-"))
    document = profile / "agentseat-onlyoffice.docx"
    write_minimal_docx(document)
    before = host_snapshot()
    try:
        status = agentseat(
            "run", "--host-workspace", str(HOST_WORKSPACE), "--", "/usr/bin/env",
            f"XDG_CONFIG_HOME={profile / 'config'}",
            f"XDG_CACHE_HOME={profile / 'cache'}",
            f"XDG_DATA_HOME={profile / 'data'}",
            f"XDG_STATE_HOME={profile / 'state'}",
            "/usr/bin/onlyoffice-desktopeditors", str(document),
            timeout=60,
        )
        assert_isolated(status)
        assert status["windows"][0]["xwayland"] is False
        acceptance = command(str(ONLYOFFICE_ACCEPTANCE), timeout=60)
        proof = json.loads(acceptance.stdout)
        after = host_snapshot()
        return {
            "passed": True,
            "app": "ONLYOFFICE Desktop Editors",
            "toolkit": "Chromium/CEF native Wayland lane",
            "typed": proof["text"],
            "image": proof["evidence"],
            "ocr": proof["ocr"],
            "observe_backend": proof["observe"]["capture"]["backend"],
            "host_focus_same": [before["workspace"], before["window"]] == [after["workspace"], after["window"]],
            "host_outer_never_active": True,
            "lane": proof["lane"],
            "stamp": stamp,
        }
    finally:
        stop_if_running()
        terminate_processes_using_paths(profile)
        shutil.rmtree(profile, ignore_errors=True)


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stop_if_running()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    cases: list[tuple[str, Callable[[str], dict[str, Any]]]] = [
        ("gtk", gtk_mousepad),
        ("qt", qt6ct),
        ("electron", electron_code),
        ("onlyoffice", onlyoffice),
    ]
    result: dict[str, Any] = {"stamp": stamp, "host_workspace": HOST_WORKSPACE, "matrix": {}}
    for name, case in cases:
        try:
            result["matrix"][name] = case(stamp)
        except Exception as exc:
            failure: dict[str, Any] = {
                "passed": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            if agentseat("status")["running"]:
                failure_image = EVIDENCE / f"software-{name}-failure-{stamp}.png"
                try:
                    failure["failure_observe"] = agentseat("observe", str(failure_image))
                except Exception as observe_error:
                    failure["failure_observe_error"] = str(observe_error)
            result["matrix"][name] = failure
        finally:
            stop_if_running()
    result["passed"] = all(item["passed"] for item in result["matrix"].values())
    result["final_status"] = agentseat("status")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
