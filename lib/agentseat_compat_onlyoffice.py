"""OnlyOffice compatibility lane for sessions started before 0.5.0.

Older releases launched OnlyOffice through a private X11 clipboard and pasted
through its context menu, located visually with Tesseract. Current sessions use
the generic text lanes; the controller loads this module only when a session's
runtime state still names that legacy compatibility type.
"""

from __future__ import annotations

import base64
import csv
import os
import subprocess
import time
from types import ModuleType
from typing import Any


def paste(ctl: ModuleType, text: str) -> dict[str, Any]:
    _ = ctl._
    state_value = ctl.load_state()
    display = state_value.get("x_display")
    if not display:
        raise ctl.AgentSeatError(_("private X display is unavailable"))
    seat = ctl.rpc("seat.status")["seat"]
    x = float(seat["pointer_x"])
    y = float(seat["pointer_y"])
    windows = ctl.rpc("window.list")
    focused_id = str(seat.get("focused_window"))
    window = next((item for item in windows if str(item["id"]) == focused_id), windows[0] if windows else None)
    if not window:
        raise ctl.AgentSeatError(_("no OnlyOffice window is available"))
    encoded = base64.b64encode(text.encode()).decode()
    owner = subprocess.Popen(
        [str(ctl.ROOT / "bin/x11-clipboard-owner"), "--display", str(display), encoded],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = owner.stdout.readline().strip() if owner.stdout else ""
        if ready != "READY":
            error = owner.stderr.read().strip() if owner.stderr else ""
            raise ctl.AgentSeatError(error or _("private X clipboard owner failed"))
        ctl.pointer_click(273)
        time.sleep(0.6)
        xenv = {**os.environ, "DISPLAY": str(display)}
        active = ctl.run(["xdotool", "getactivewindow"], env=xenv).stdout.strip()
        geometry = {}
        for line in ctl.run(["xdotool", "getwindowgeometry", "--shell", active], env=xenv).stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"X", "Y", "WIDTH", "HEIGHT"}:
                geometry[key] = int(value)
        menu_image = ctl.RUNTIME / "onlyoffice-context-menu.png"
        ctl.run(["import", "-window", active, str(menu_image)], env=xenv)
        tsv = ctl.run(["tesseract", str(menu_image), "stdout", "--psm", "11", "tsv"]).stdout
        paste_word: dict[str, str] | None = None
        for row in csv.DictReader(tsv.splitlines(), delimiter="\t"):
            if row.get("text", "").strip().casefold() == "paste":
                paste_word = row
                break
        if not paste_word:
            raise ctl.AgentSeatError(_("OnlyOffice Paste menu item was not visually detected"))
        paste_x = (
            geometry.get("X", 0) + int(paste_word["left"]) + int(paste_word["width"]) / 2.0
        ) / float(window["width"])
        paste_y = (
            geometry.get("Y", 0) + int(paste_word["top"]) + int(paste_word["height"]) / 2.0
        ) / float(window["height"])
        ctl.pointer_move(paste_x, paste_y)
        ctl.pointer_click(272)
        # OnlyOffice requests the X11 clipboard asynchronously after the menu
        # click. Keep the private owner alive until that transfer completes.
        time.sleep(3.0)
        ctl.pointer_move(x, y)
    finally:
        owner.terminate()
        try:
            owner.wait(timeout=2)
        except subprocess.TimeoutExpired:
            owner.terminate()
    return {"typed": len(text), "lane": "onlyoffice-private-x11-clipboard", "pointer_restored": [x, y]}
