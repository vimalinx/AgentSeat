#!/usr/bin/env python3
"""A launcher exits; its resident app hides or destroys windows but keeps running."""
import argparse,json,os,subprocess,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--worker',action='store_true');a=p.parse_args()
if not a.worker:
    subprocess.Popen([sys.executable,__file__,'--state',str(a.state),'--worker'],close_fds=True)
    raise SystemExit(0)
import gi
gi.require_version('Gtk','4.0')
from gi.repository import Gio,GLib,Gtk
app=Gtk.Application(application_id='org.vimalinx.AgentSeatBackgroundFixture',flags=Gio.ApplicationFlags.NON_UNIQUE)
window=None
state={'pid':os.getpid(),'visible':False,'closes':0,'generation':0,'ticks':0}
def record():
    temporary=a.state.with_suffix('.tmp');temporary.write_text(json.dumps(state));temporary.replace(a.state)
def show():
    global window
    if window is None:
        window=Gtk.ApplicationWindow(application=app,title='Background lifecycle fixture')
        window.set_default_size(640,480);window.set_child(Gtk.Label(label='Application controls close, hide, restore and exit'))
        window.connect('close-request',close)
        state['generation']+=1
    window.present();state['visible']=True;record()
def close(current):
    global window
    state['closes']+=1;state['visible']=False
    if state['closes']==1:
        current.set_visible(False)
    else:
        current.destroy();window=None
    record();return True
command=a.state.with_suffix('.command')
def poll():
    state['ticks']+=1
    if command.exists():
        value=command.read_text();command.unlink()
        if value=='show':show()
        elif value=='exit':
            subprocess.Popen([sys.executable,str(Path(__file__).with_name('background_worker_fixture.py')),str(a.state.with_suffix('.worker.json'))],close_fds=True)
            app.release();app.quit();return False
    record();return True
def activate(application):
    app.hold();show();GLib.timeout_add(100,poll)
app.connect('activate',activate)
raise SystemExit(app.run(None))
