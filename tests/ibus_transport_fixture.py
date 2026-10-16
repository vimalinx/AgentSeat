#!/usr/bin/env python3
"""Disposable IBus daemon and deterministic engine for the real GTK module.
Run under a disposable parent's dbus-run-session; never on the desktop bus.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import gi
gi.require_version('IBus','1.0')
from gi.repository import IBus, GLib
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--x11',action='store_true');p.add_argument('--a11y',action='store_true');p.add_argument('--bin-dir',type=Path,default=ROOT/'bin');p.add_argument('--toolkit',choices=['gtk3','gtk4','qt6'],default='gtk4');a=p.parse_args()
runtime=Path(os.environ['AGENTSEAT_RUNTIME_DIR'])/'ibus-probe';runtime.mkdir(mode=0o700)
os.environ.update({'IBUS_ADDRESS':'unix:path='+str(runtime/'bus'),
    'XDG_CONFIG_HOME':str(runtime/'config'),'XDG_CACHE_HOME':str(runtime/'cache'),
    'GTK_IM_MODULE':'ibus','QT_IM_MODULE':'ibus','QT_IM_MODULES':'ibus','XMODIFIERS':'@im=ibus'})
children=[];engine_events=[];loop=GLib.MainLoop()
def record(event):
    engine_events.append(event)
    (a.out/'ibus-events.json').write_text(json.dumps(engine_events,ensure_ascii=False))
class Engine(IBus.Engine):
    def do_process_key_event(self,keyval,keycode,state):
        if state & IBus.ModifierType.RELEASE_MASK:return False
        if keyval==IBus.KEY_F6:
            self.update_preedit_text(IBus.Text.new_from_string('候选'),2,True)
            record('preedit');return True
        if keyval==IBus.KEY_F7:
            self.hide_preedit_text()
            self.commit_text(IBus.Text.new_from_string('IBus透传 日本語 🙂'))
            record('commit');return True
        return False
try:
    log=(a.out/'ibus.log').open('w')
    daemon=subprocess.Popen(['/usr/bin/ibus-daemon','--single','--cache=none','--panel=disable','--emoji-extension=disable','--config=disable','--address='+os.environ['IBUS_ADDRESS']],stdout=log,stderr=log)
    children.append(daemon)
    deadline=time.monotonic()+8
    while not (runtime/'bus').exists():
        if daemon.poll() is not None or time.monotonic()>deadline:raise RuntimeError('private IBus did not start')
        time.sleep(.02)
    IBus.init();bus=IBus.Bus.new();assert bus.is_connected()
    component=IBus.Component.new('org.freedesktop.IBus.AgentSeatProbe','AgentSeat transport fixture','1','MIT','AgentSeat','','','')
    desc=IBus.EngineDesc.new('agentseat-probe','AgentSeat probe','deterministic transport fixture','zh','MIT','AgentSeat','','us')
    component.add_engine(desc)
    factory=IBus.Factory.new(bus.get_connection());factory.add_engine('agentseat-probe',Engine)
    assert bus.register_component(component)
    def launch():
        # Global-engine selection is limited to this newly spawned daemon.
        bus.set_global_engine_async('agentseat-probe',3000,None,engine_selected,None)
        return False
    def engine_selected(bus,result,data):
        assert bus.set_global_engine_async_finish(result)
        fixture=subprocess.Popen(['/usr/bin/python3',str(ROOT/'tests/nested_control_fixture.py'),
            '--out',str(a.out),'--bin-dir',str(a.bin_dir),'--fixture','input','--toolkit',a.toolkit,'--ime','inherit',
            *(['--x11'] if a.x11 else []),*(['--a11y'] if a.a11y else [])])
        children.append(fixture)
        (a.out/'ibus-ready.json').write_text(json.dumps({'pid':daemon.pid,'engine':'agentseat-probe'}))
        def check():
            if fixture.poll() is not None:loop.quit();return False
            return True
        GLib.timeout_add(100,check)
    GLib.idle_add(launch)
    def stop(*_):loop.quit()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    loop.run()
finally:
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait()
