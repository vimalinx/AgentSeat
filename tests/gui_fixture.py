#!/usr/bin/env python3
"""Small native GTK4 GUI used only by AgentSeat's live acceptance matrix."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--pointer-proof", type=Path, required=True)
    parser.add_argument("--title", default="AgentSeat native Wayland GUI")
    parser.add_argument("--protocol-log", type=Path)
    args = parser.parse_args()

    if args.protocol_log:
        log_fd = os.open(args.protocol_log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(log_fd, 2)
        os.close(log_fd)

    application = Gtk.Application(application_id="org.vimalinx.AgentSeatFixture")

    def activate(app: Gtk.Application) -> None:
        window = Gtk.ApplicationWindow(application=app, title=args.title)
        window.set_default_size(640, 320)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.set_margin_top(36)
        box.set_margin_bottom(36)
        box.set_margin_start(48)
        box.set_margin_end(48)
        label = Gtk.Label(label="Native Wayland input and per-window capture")
        entry = Gtk.Entry()
        entry.set_placeholder_text("Agent types here with its private seat")
        entry.set_hexpand(True)
        entry.set_vexpand(True)
        button = Gtk.Button(label="Agent click proof")

        def clicked(*_: object) -> None:
            args.pointer_proof.write_text("clicked", encoding="utf-8")
            entry.grab_focus()

        button.connect("clicked", clicked)

        def commit(*_: object) -> None:
            args.proof.write_text(entry.get_text(), encoding="utf-8")
            app.quit()

        entry.connect("activate", commit)
        box.append(label)
        box.append(button)
        box.append(entry)
        window.set_child(box)
        window.present()
        entry.grab_focus()

    application.connect("activate", activate)
    return application.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
