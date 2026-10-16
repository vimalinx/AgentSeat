#!/usr/bin/env python3
"""Same single input field on independently implemented toolkit clients."""
import argparse
import json
import os
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--toolkit',choices=['gtk3','gtk4','qt6'],default='gtk4');a=p.parse_args()
def record(text):
    temp=a.state.with_suffix('.tmp');temp.write_text(json.dumps({'text':text,'pid':os.getpid()},ensure_ascii=False));temp.replace(a.state)
if a.toolkit=='qt6':
    from PySide6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLabel,QLineEdit
    app=QApplication([]);window=QWidget();window.setWindowTitle('Input compatibility probe')
    box=QVBoxLayout(window);box.addWidget(QLabel('Independent toolkit input probe'))
    entry=QLineEdit();box.addWidget(entry);entry.textChanged.connect(record)
    window.resize(640,320);window.show();entry.setFocus();record('');app.exec()
else:
    import gi
    gi.require_version('Gtk','3.0' if a.toolkit=='gtk3' else '4.0')
    from gi.repository import Gtk,Gio
    app=Gtk.Application(application_id='org.vimalinx.InputProbe',flags=Gio.ApplicationFlags.NON_UNIQUE)
    def activate(app):
        window=Gtk.ApplicationWindow(application=app,title='Input compatibility probe');window.set_default_size(640,320)
        box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=12)
        label=Gtk.Label(label='Independent toolkit input probe');entry=Gtk.Entry()
        if a.toolkit=='gtk3':
            window.add(box);box.pack_start(label,False,False,20);box.pack_start(entry,False,False,20)
            window.show_all()
        else:
            window.set_child(box);box.append(label);box.append(entry);window.present()
        entry.connect('changed',lambda entry:record(entry.get_text()));entry.grab_focus();record('')
    app.connect('activate',activate);app.run([])
