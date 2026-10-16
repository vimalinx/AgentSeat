#!/usr/bin/env python3
"""Send real xdg_toplevel.close through a private parent; never host input."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--report-dir', type=Path, required=True)
p.add_argument('--cli', type=Path, default=ROOT/'bin/agentseat')
p.add_argument('--x11', action='store_true')
p.add_argument('--scale', default='1.6')
a = p.parse_args()
out = a.report_dir.resolve()
out.mkdir(parents=True, exist_ok=False)
sdk = a.cli.resolve().parents[1]/'share/python'
sys.path.insert(0, str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session

instance = f'parent-close-{os.getpid()}'
parent = None
inner = None
report = {'status': 'RUNNING', 'mode': 'x11' if a.x11 else 'wayland', 'scale': a.scale,
          'source': 'private parent sends xdg_toplevel.close; no host input', 'checks': {}}


def cli(*args):
    return json.loads(subprocess.check_output([str(a.cli.resolve()), '--instance', instance, *args], timeout=35))


def eventually(fn):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('close postcondition timed out')


def state():
    return json.loads((out/'ui.json').read_text())


def rpc(method, params=None, expected_error=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(inner['socket'])
        client.sendall((json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': {
            'session_id': inner['session_id'], 'controller_id': 'close-fixture',
            'request_id': time.monotonic_ns(), **(params or {})}}) + '\n').encode())
        data = b''
        while b'\n' not in data:
            chunk = client.recv(65536)
            if not chunk:
                raise RuntimeError('inner daemon disconnected')
            data += chunk
        reply = json.loads(data.split(b'\n')[0])
        if expected_error:
            assert reply.get('error', {}).get('data', {}).get('kind') == expected_error, reply
            return reply['error']
        assert 'error' not in reply, reply
        return reply['result']


def close_parent(name, kind, count):
    before = cli('observe', str(out/(name+'-before.png')))
    windows = parent.rpc('window.list')
    assert len(windows) == 1, windows
    result = cli('window', 'close', windows[0]['id'])
    assert result['execution'] == 'finished', result
    assert result['verification'] == 'close_request_delivered', result
    actual = eventually(lambda: (s if (s := state())[kind+'_closes'] == count else None))
    report['checks'][name] = {'receipt': result, 'application': actual}
    return actual


try:
    report['launch'] = cli('run', '--ime', 'simple', '--host-workspace', '119', '--', '/usr/bin/python3',
        str(ROOT/'tests/nested_control_fixture.py'), '--out', str(out), '--fixture', 'close',
        '--hud', '--scale', a.scale, '--bin-dir', str(a.cli.resolve().parent), *(['--x11'] if a.x11 else []))
    eventually(lambda: (out/'nested.json').exists() and (out/'ui.json').exists())
    inner = json.loads((out/'nested.json').read_text())
    parent = Session.from_instance(instance, controller='close-parent')
    cli('hud', 'stop')  # Only the inner HUD participates in this fixture.
    eventually(lambda: len(rpc('window.list')) == 2)
    windows = rpc('window.list')
    child = next(w for w in windows if w['title'] == 'Close fixture child')
    lease = rpc('control.acquire', {'ttl_ms': 10000})
    focus = rpc('window.focus', {'window_id': child['id'], 'lease_id': lease['lease_id'], 'control_epoch': lease['control_epoch']})
    eventually(lambda: rpc('operation.get', {'operation_id': focus['operation_id']})['execution'] == 'finished')
    rpc('seat.pause')
    report['checks']['paused_agent_close_rejected'] = rpc('window.close', {
        'window_id': child['id'], 'lease_id': lease['lease_id'], 'control_epoch': lease['control_epoch'],
    }, expected_error='NO_WRITE_LEASE')
    report['checks']['paused_acquire_rejected'] = rpc('control.acquire', expected_error='SEAT_PAUSED')
    assert state()['child_closes'] == 0
    geometry = rpc('session.heartbeat')['output']
    w, h = geometry['width'], geometry['height']
    hud_scale = float(a.scale) if a.x11 else 1
    head = ((w-16-23*hud_scale)/w, (h-16-23*hud_scale)/h)
    frame = cli('observe', str(out/'hud-before.png'))['capture']
    parent.activate(point=head, frame=frame)
    parent.release()
    eventually(lambda: not any(w['focused'] for w in rpc('window.list')))
    cli('observe', str(out/'hud-focused.png'))
    report['checks']['hud_focus_keeps_application_target'] = {'head': head, 'windows': rpc('window.list')}
    first = close_parent('child_cancel_preserves_all', 'child', 1)
    assert first['main_open'] and first['child_open'] and first['main_closes'] == 0
    assert cli('status')['running'] and rpc('collaboration.status')['seat_paused']
    assert len(rpc('window.list')) == 2
    second = close_parent('child_accept_preserves_main', 'child', 2)
    assert second['main_open'] and not second['child_open'] and second['main_closes'] == 0
    eventually(lambda: len(rpc('window.list')) == 1)
    assert cli('status')['running']
    third = close_parent('main_cancel_preserves_session', 'main', 1)
    assert third['main_open'] and cli('status')['running']
    cli('observe', str(out/'main-kept.png'))
    fourth = close_parent('main_accept_exits_normally', 'main', 2)
    assert not fourth['main_open']
    eventually(lambda: (out/'inner-exit.json').exists())
    report['inner_exit'] = json.loads((out/'inner-exit.json').read_text())
    assert report['inner_exit']['exit_code'] == 0, report['inner_exit']
    report['status'] = 'PASS'
except BaseException as error:
    report['error'] = repr(error)
    report['status'] = 'FAIL'
    raise
finally:
    if parent:
        try:
            parent.release()
        except Exception:
            pass
    report['cleanup'] = cli('stop')
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({'status': report['status'], 'report': str(out/'report.json')}))
