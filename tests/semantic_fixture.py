#!/usr/bin/env python3
"""Disposable real GTK controls for the scoped semantic protocol."""
import argparse
import json
import os
import subprocess
from pathlib import Path
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio, GLib

p = argparse.ArgumentParser()
p.add_argument('--state', required=True, type=Path)
p.add_argument('--output-dir', required=True, type=Path)
p.add_argument('--fault-state', type=Path)
a = p.parse_args()
a.output_dir.mkdir(parents=True, exist_ok=True)
state = {'count': 0, 'saved': None, 'pid': os.getpid()}
fault = subprocess.Popen(['/usr/bin/python3',str(Path(__file__).with_name('atspi_fault_fixture.py')),'--state',str(a.fault_state)]) if a.fault_state else None

def write():
    temp = a.state.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False))
    temp.replace(a.state)

def label(widget, name):
    widget.update_property([Gtk.AccessibleProperty.LABEL], [name])
    return widget

app = Gtk.Application(application_id='org.vimalinx.AgentSeatSemanticFixture', flags=Gio.ApplicationFlags.NON_UNIQUE)

def activate(app):
    window = Gtk.ApplicationWindow(application=app, title='AgentSeat semantic controls')
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    for edge in ['top', 'bottom', 'start', 'end']:getattr(box, 'set_margin_'+edge)(24)
    heading = Gtk.Label(label='AgentSeat — semantic action acceptance')
    box.append(heading)
    if fault:
        late_label = Gtk.Label(label='Delayed calls completed: 0')
        box.append(late_label)
        def refresh_fault():
            if a.fault_state.exists():
                data = json.loads(a.fault_state.read_text())
                late_label.set_label('Delayed calls completed: ' + str(data['completed']))
            return True
        GLib.timeout_add(100, refresh_fault)
    entry = label(Gtk.Entry(text='Draft'), 'Document title')
    entry.connect('changed', lambda w: (state.update(title=w.get_text()), write()))
    box.append(entry)
    count_label = Gtk.Label(label='Count 0')
    count = Gtk.Button(label='Count')
    def count_clicked(_):
        state['count'] += 1
        count_label.set_label('Count '+str(state['count']))
        write()
    count.connect('clicked', count_clicked)
    box.append(count); box.append(count_label)
    check = Gtk.CheckButton(label='Enable option')
    check.connect('toggled', lambda w: (state.update(checked=w.get_active()), write()))
    box.append(check)
    scale = label(Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1), 'Level')
    scale.set_value(20)
    scale.connect('value-changed', lambda w: (state.update(level=w.get_value()), write()))
    box.append(scale)
    choices = label(Gtk.ListBox(), 'Choices')
    for text in ['Alpha', 'Beta', 'Gamma']: choices.append(Gtk.Label(label=text))
    choices.connect('row-selected', lambda w, row: (state.update(selected=row.get_index() if row else -1), write()))
    box.append(choices)
    disabled = Gtk.Button(label='Disabled action'); disabled.set_sensitive(False); box.append(disabled)
    password = label(Gtk.PasswordEntry(), 'Protected field')
    password.set_text('PRIVATE_SENTINEL_do_not_read')
    box.append(password)
    save_as = Gtk.Button(label='Save as')
    def open_dialog(_):
        dialog = Gtk.Window(title='Save document', transient_for=window, modal=True)
        dialog.set_default_size(480, 200)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for edge in ['top', 'bottom', 'start', 'end']:getattr(content, 'set_margin_'+edge)(20)
        filename = label(Gtk.Entry(text='draft.txt'), 'File name')
        content.append(filename)
        save = Gtk.Button(label='Save document')
        def commit(_):
            name = filename.get_text()
            if not name or Path(name).name != name:return
            target = a.output_dir / name
            target.write_text(entry.get_text(), encoding='utf-8')
            state['saved'] = str(target); write(); dialog.destroy()
        save.connect('clicked', commit); content.append(save)
        dialog.set_child(content); dialog.present(); filename.grab_focus()
    save_as.connect('clicked', open_dialog);box.append(save_as)
    window.set_child(box);window.set_default_size(800, 760)
    window.present()
    state.update(title=entry.get_text(), checked=False, level=20, selected=-1)
    write()

app.connect('activate', activate)
try:app.run([])
finally:
    if fault and fault.poll() is None:
        fault.terminate();fault.wait(timeout=3)
