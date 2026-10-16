#!/usr/bin/env python3
"""IBus engine through its real daemon, toolkit module and nested GTK.
All keys originate in a disposable parent; no host keyboard injection.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--report-dir', type=Path, required=True)
p.add_argument('--cli', type=Path, default=ROOT/'bin/agentseat')
p.add_argument('--x11', action='store_true')
p.add_argument('--a11y', action='store_true')
p.add_argument('--toolkit',choices=['gtk3','gtk4','qt6'],default='gtk4')
a = p.parse_args()
out = a.report_dir.resolve(); out.mkdir(parents=True, exist_ok=False)
instance = 'human-compat-' + str(os.getpid())
report = {'status': 'RUNNING', 'checks': {}, 'source': 'private parent keyboard only'}

def cli(*args):
    r = subprocess.run([str(a.cli), '--instance', instance, *args], capture_output=True, text=True, timeout=40)
    if r.returncode: raise RuntimeError(r.stderr or r.stdout)
    return json.loads(r.stdout)

def eventually(fn, timeout=12):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = fn()
        if value: return value
        time.sleep(.05)
    raise AssertionError('postcondition timeout')

def ui(): return json.loads((out/'ui.json').read_text())
def key(code):
    cli('key', str(code), 'down'); cli('key', str(code), 'up')
def rpc(method, params=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3); s.connect(inner['socket'])
        s.sendall((json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':{
            'session_id':inner['session_id'],'controller_id':'human-compat-inner',
            'request_id':time.monotonic_ns(),**(params or {})}})+'\n').encode())
        data=b''
        while b'\n' not in data: data+=s.recv(65536)
        v=json.loads(data.split(b'\n')[0]); assert 'error' not in v,v
        return v['result']
try:
    report['launch']=cli('run','--ime','simple','--host-workspace','126','--',
        '/usr/bin/dbus-run-session','--','/usr/bin/python3',str(ROOT/'tests/ibus_transport_fixture.py'),
        '--out',str(out),'--bin-dir',str(a.cli.resolve().parent),'--toolkit',a.toolkit,*(['--x11'] if a.x11 else []),*(['--a11y'] if a.a11y else []))
    eventually(lambda:(out/'nested.json').exists() and (out/'ui.json').exists() and (out/'ibus-ready.json').exists())
    inner=json.loads((out/'nested.json').read_text())
    cli('observe',str(out/'before.png'))
    key(64)
    eventually(lambda:(out/'ibus-events.json').exists() and 'preedit' in json.loads((out/'ibus-events.json').read_text()))
    cli('observe',str(out/'preedit.png'))
    key(65)
    eventually(lambda:ui()['text']=='IBus透传 日本語 🙂')
    report['checks']['ibus_engine_commit']=ui()['text']
    report['checks']['ibus_engine_events']=json.loads((out/'ibus-events.json').read_text())
    if a.a11y:
        receipt=json.loads((Path(inner['runtime'])/'a11y.json').read_text())
        assert receipt['ime']=='inherit' and receipt['a11y_enabled']
        report['checks']['private_a11y_and_inherited_ibus']='PASS'
    cli('observe',str(out/'after.png'))
    report['status']='PASS'
except BaseException as exc:
    report['status']='FAIL';report['error']=repr(exc)
    try:report['last_ui']=ui();cli('observe',str(out/'failure.png'))
    except Exception:pass
    raise
finally:
    report['cleanup']=cli('stop')
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
