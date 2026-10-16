#!/usr/bin/env python3
"""Parent keyboard layout through a real nested Wayland backend and GTK.
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
p.add_argument('--layout', choices=['de','fr','us'], required=True)
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
    os.environ['AGENTSEAT_XKB_LAYOUT'] = a.layout
    report['launch']=cli('run', '--ime','simple', '--host-workspace','126','--','/usr/bin/python3',str(ROOT/'tests/nested_control_fixture.py'),
        '--out',str(out),'--fixture','control','--bin-dir',str(a.cli.resolve().parent), *(['--x11'] if a.x11 else []))
    eventually(lambda:(out/'nested.json').exists() and (out/'ui.json').exists())
    inner=json.loads((out/'nested.json').read_text())
    cli('observe',str(out/'before.png'))
    for code in [21,16,30]: key(code)
    expected={'de':'zqa', 'fr':'yaq', 'us':'yqa'}[a.layout]
    eventually(lambda:ui()['text']==expected)
    report['checks']['parent_layout_preserved']={'layout':a.layout,'text':ui()['text']}
    eventually(lambda:not rpc('collaboration.status')['human_active'])
    lease=rpc('control.acquire')
    op=rpc('keyboard.type',{'text':'TEST','lease_id':lease['lease_id'],'control_epoch':lease['control_epoch']})
    eventually(lambda:rpc('operation.get',{'operation_id':op['operation_id']})['execution']=='finished')
    rpc('control.release',{'lease_id':lease['lease_id']})
    eventually(lambda:ui()['text']==expected+'TEST')
    key(21)
    eventually(lambda:ui()['text']==expected+'TEST'+expected[0])
    report['checks']['agent_and_parent_layouts_stay_independent']=ui()['text']
    cli('observe',str(out/'after.png'))
    report['status']='PASS'
except BaseException as exc:
    report['status']='FAIL';report['error']=repr(exc)
    try: report['last_ui']=ui();cli('observe',str(out/'failure.png'))
    except Exception: pass
    raise
finally:
    report['cleanup']=cli('stop')
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
