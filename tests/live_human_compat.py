#!/usr/bin/env python3
"""Human IME and parent clipboard through an owned nested Wayland backend.
The outer clipboard is disposable; no real desktop clipboard/input is used.
"""
import argparse
import base64
import hashlib
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
p.add_argument('--ime', action='store_true')
p.add_argument('--desktop-ime', action='store_true')
p.add_argument('--review-gate', action='store_true')
a = p.parse_args()
out = a.report_dir.resolve(); out.mkdir(parents=True, exist_ok=False)
instance = 'human-compat-' + str(os.getpid())
report = {'status': 'RUNNING', 'checks': {}, 'source': 'private parent keyboard and clipboard only'}
children = []

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
def chord(code):
    cli('key', '29', 'down')
    try: key(code)
    finally: cli('key', '29', 'up')
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
def publish(value, mime='text/plain;charset=utf-8'):
    child=subprocess.Popen(['/usr/bin/wl-copy','--foreground','--type',mime],env=parent_env,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    children.append(child); child.stdin.write(value); child.stdin.close(); time.sleep(.3)
    assert child.poll() is None
    return child
def read_clip(env, mime='text/plain;charset=utf-8'):
    return subprocess.run(['/usr/bin/wl-paste','--no-newline','--type',mime],env=env,capture_output=True,timeout=3)
try:
    report['launch']=cli('run', *(['--ime','fcitx5'] if a.desktop_ime else ['--ime','simple']), '--host-workspace','126','--','/usr/bin/python3',str(ROOT/'tests/nested_control_fixture.py'),
        '--out',str(out),'--fixture','control','--clipboard-checks','--scale','1.6',
        '--bin-dir',str(a.cli.resolve().parent), *(['--x11'] if a.x11 else []), *(['--ime', 'desktop' if a.desktop_ime else 'fcitx5'] if a.ime or a.desktop_ime else []))
    cli('hud','stop')
    eventually(lambda:(out/'nested.json').exists() and (out/'ui.json').exists())
    inner=json.loads((out/'nested.json').read_text())
    parent_env={**os.environ,'WAYLAND_DISPLAY':report['launch']['state']['wayland_display']}
    parent_env.pop('DISPLAY',None)
    inner_env={**parent_env,'WAYLAND_DISPLAY':inner['wayland_display']}
    cli('observe',str(out/'before.png'))
    (out/'ready.json').write_text(json.dumps({'instance':instance,'inner':inner}))
    if a.review_gate: eventually(lambda:(out/'continue').exists(),timeout=180)
    key(30); eventually(lambda:ui()['text']=='a')
    chord(30); key(14)
    if a.ime or a.desktop_ime:
        chord(57)  # Fcitx trigger, same raw keys as a physical keyboard.
        key(49); key(23); key(35); key(46)  # ni hc = 你好 in Xiaohe double pinyin
        cli('observe',str(out/'candidates.png'))
        key(57)
        eventually(lambda:ui()['text']=='你好')
        report['checks']['rime_xiaohe_raw_keys_commit']='你好'
        chord(57)
        chord(30); key(14)
    incoming='从外部复制的中文 ✅'.encode()
    owner=publish(incoming)
    chord(47)
    eventually(lambda:ui()['text']==incoming.decode())
    report['checks']['parent_to_application']=ui()['text']
    # Ordinary application Ctrl+C must publish outward, without inspecting host data.
    chord(30); chord(46)
    eventually(lambda:read_clip(parent_env).stdout==incoming)
    report['checks']['application_to_parent']=read_clip(parent_env).stdout.decode()
    # Agent paste stays private and must not replace what the human copied out.
    eventually(lambda:not rpc('collaboration.status')['human_active'])
    lease=rpc('control.acquire')
    op=rpc('keyboard.paste',{'text':'AGENT-PRIVATE','lease_id':lease['lease_id'],'control_epoch':lease['control_epoch']})
    eventually(lambda:rpc('operation.get',{'operation_id':op['operation_id']})['execution']=='finished')
    rpc('control.release',{'lease_id':lease['lease_id']})
    assert read_clip(parent_env).stdout==incoming, 'Agent paste changed or invalidated human clipboard'
    report['checks']['agent_paste_keeps_human_clipboard']='PASS'
    # A host import must not be readable by an idle Agent after human grace.
    secret=b'disposable-parent-offer'
    publish(secret)
    eventually(lambda:not rpc('collaboration.status')['human_active'])
    assert read_clip(inner_env).stdout!=secret
    report['checks']['idle_agent_cannot_receive_parent_data']='PASS'
    chord(30); chord(47)
    eventually(lambda:ui()['text']==secret.decode())
    report['checks']['human_paste_restored_after_agent']='PASS'
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII=')
    publish(png, 'image/png'); key(65)
    eventually(lambda:ui()['clipboard_reads'].get('image/png',{}).get('sha256')==hashlib.sha256(png).hexdigest())
    report['checks']['image_parent_to_application']=ui()['clipboard_reads']['image/png']
    key(64)
    eventually(lambda:read_clip(parent_env,'image/png').stdout==png)
    html=('<p>复制中文</p>'*65536).encode()
    copied=read_clip(parent_env,'text/html')
    assert copied.returncode==0 and copied.stdout==html
    report['checks']['image_and_large_html_application_to_parent']={'image_sha256':hashlib.sha256(png).hexdigest(),'html_bytes':len(html)}
    cli('observe',str(out/'after.png'))
    if a.desktop_ime:
        cli('window','close',cli('windows')[0]['id'])
        eventually(lambda:(out/'parent-ime-survived.json').exists())
        survived=json.loads((out/'parent-ime-survived.json').read_text())
        assert survived['same_pid_alive'] and survived['ready'], survived
        report['checks']['closing_private_display_preserves_desktop_ime']=survived
    report['status']='PASS'
except BaseException as exc:
    report['status']='FAIL';report['error']=repr(exc)
    try: report['last_ui']=ui();cli('observe',str(out/'failure.png'))
    except Exception: pass
    raise
finally:
    for child in children:
        if child.poll() is None: child.terminate();child.wait(timeout=3)
    report['cleanup']=cli('stop')
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
