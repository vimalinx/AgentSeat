#!/usr/bin/env python3
"""Disposable real main/child windows; first close is vetoed like a save dialog."""
import argparse
import json
import os
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

p = argparse.ArgumentParser()
p.add_argument("--state", type=Path, required=True)
a = p.parse_args()
app = Gtk.Application(application_id="org.vimalinx.AgentSeatCloseFixture",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
state = {"pid": os.getpid(), "main_closes": 0, "child_closes": 0,
         "main_open": True, "child_open": True}


def record():
    temporary = a.state.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(a.state)


def activate(application):
    main = Gtk.ApplicationWindow(application=application, title="Close fixture main")
    main.set_default_size(640, 480)
    main.set_child(Gtk.Label(label="Main window stays open when its child closes"))
    child = Gtk.Window(application=application, title="Close fixture child", transient_for=main)
    child.set_default_size(420, 220)
    label = Gtk.Label(label="First close: cancel; second close: accept")
    child.set_child(label)

    def close(window, kind):
        state[kind + "_closes"] += 1
        veto = state[kind + "_closes"] == 1
        if veto:
            if kind == "child":
                label.set_text("Close cancelled — child is still usable")
        else:
            state[kind + "_open"] = False
        record()
        return veto

    main.connect("close-request", close, "main")
    child.connect("close-request", close, "child")
    main.present()
    GLib.timeout_add(200, lambda: (child.present(), False)[1])
    record()


app.connect("activate", activate)
raise SystemExit(app.run(None))
