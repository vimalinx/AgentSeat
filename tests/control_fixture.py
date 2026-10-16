#!/usr/bin/env python3
"""Disposable GTK surface with independently observable input effects."""

import argparse
import base64
import hashlib
import json
import os
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, Gdk, GLib


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--tree-nodes", type=int, default=0)
    parser.add_argument("--clipboard-checks", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.tree_nodes <= 1000: parser.error("tree node count must be from 0 to 1000")
    app = Gtk.Application(application_id="org.vimalinx.AgentSeatControlFixture", flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(application):
        window = Gtk.ApplicationWindow(application=application, title="AgentSeat control regression")
        window.set_default_size(640, 320)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        entry = Gtk.Entry()
        entry.set_placeholder_text("Private input proof")
        button = Gtk.Button(label="Count click")
        count = 0
        keys = []
        clipboard_reads = {}

        def record(*_):
            value = {"text": entry.get_text(), "clicks": count, "keys": keys[-64:], "pid": os.getpid(), "at_ns": time.monotonic_ns()}
            if args.clipboard_checks:
                value["clipboard_reads"] = clipboard_reads
            temporary = args.state.with_suffix(".tmp")
            temporary.write_text(json.dumps(value) + "\n")
            temporary.replace(args.state)

        def clicked(*_):
            nonlocal count
            count += 1
            record()
            entry.grab_focus()

        key_events = Gtk.EventControllerKey()
        key_events.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def key_pressed(_controller, keyval, _keycode, state):
            keys.append(["press", keyval, int(state), time.monotonic_ns()])
            record()
            if args.clipboard_checks and keyval == Gdk.KEY_F6:
                # Public deterministic image and HTML, with enough bytes to
                # require streaming beyond a pipe's capacity.
                png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII=")
                html = ("<p>复制中文</p>" * 65536).encode()
                window.get_clipboard().set_content(Gdk.ContentProvider.new_union([
                    Gdk.ContentProvider.new_for_bytes("image/png", GLib.Bytes.new(png)),
                    Gdk.ContentProvider.new_for_bytes("text/html", GLib.Bytes.new(html)),
                ]))
                return True
            if args.clipboard_checks and keyval == Gdk.KEY_F7:
                def received(clipboard, result):
                    stream, mime = clipboard.read_finish(result)
                    digest, length = hashlib.sha256(), 0
                    def chunk_ready(source, reply):
                        nonlocal length
                        chunk = source.read_bytes_finish(reply).get_data()
                        if chunk:
                            digest.update(chunk); length += len(chunk)
                            source.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, chunk_ready)
                        else:
                            source.close(None)
                            clipboard_reads[mime] = {"sha256": digest.hexdigest(), "size": length}
                            record()
                    stream.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, chunk_ready)
                window.get_clipboard().read_async(["image/png"], GLib.PRIORITY_DEFAULT, None, received)
                return True
            return False

        def key_released(_controller, keyval, _keycode, state):
            keys.append(["release", keyval, int(state), time.monotonic_ns()])
            record()

        key_events.connect("key-pressed", key_pressed)
        key_events.connect("key-released", key_released)
        window.add_controller(key_events)
        entry.connect("changed", record)
        button.connect("clicked", clicked)
        box.append(Gtk.Label(label="AgentSeat P0 / P1 — disposable control fixture"))
        box.append(button)
        box.append(entry)
        if args.tree_nodes:
            scroller = Gtk.ScrolledWindow(vexpand=True)
            items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            for index in range(args.tree_nodes):
                items.append(Gtk.Label(label=f"Bounded tree item {index}"))
            scroller.set_child(items)
            box.append(scroller)
        window.set_child(box)
        window.present()
        entry.grab_focus()
        record()

    app.connect("activate", activate)
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
