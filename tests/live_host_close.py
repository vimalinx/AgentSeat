#!/usr/bin/env python3
"""Real host close, pinned to the disposable AgentSeat PID/address; no host keys."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--report-dir', type=Path, required=True)
p.add_argument('--cli', type=Path, default=ROOT/'bin/agentseat')
p.add_argument('--x11', action='store_true')
a = p.parse_args()
out = a.report_dir.resolve()
out.mkdir(parents=True, exist_ok=False)
instance = f'host-close-{os.getpid()}'
report = {'status': 'RUNNING', 'source': 'host closewindow for verified disposable PID/address', 'checks': []}


def cli(*args):
    return json.loads(subprocess.check_output([str(a.cli.resolve()), '--instance', instance, *args], timeout=35))


def eventually(fn):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('host close application postcondition timed out')


def app():
    return json.loads((out/'ui.json').read_text())


try:
    report['launch'] = cli('run', *(['--x11'] if a.x11 else []), '--host-workspace', '119',
        '--', '/usr/bin/python3', str(ROOT/'tests/close_fixture.py'), '--state', str(out/'ui.json'))
    launch = report['launch']['state']
    signature, address, pid = launch['host_instance'], launch['outer_window'], launch['microhost_pid']
    eventually(lambda: len(cli('windows')) == 2)
    child = next(w for w in cli('windows') if w['title'] == 'Close fixture child')
    cli('window', 'focus', child['id'])
    cli('pause')
    for kind, count in [('child', 1), ('child', 2), ('main', 1), ('main', 2)]:
        current = cli('status')
        assert current['running'] and current['state']['microhost_pid'] == pid
        host = json.loads(subprocess.check_output(['hyprctl', '--instance', signature, '-j', 'clients']))
        matches = [w for w in host if w.get('address') == address and w.get('pid') == pid]
        assert len(matches) == 1, 'refusing to close an unverified host window'
        assert Path(f'/proc/{pid}/exe').resolve() == a.cli.resolve().parent/'agentseat-microhost'
        result = subprocess.run(['hyprctl', '--instance', signature, 'dispatch', 'closewindow', 'address:'+address],
            text=True, capture_output=True, timeout=5, check=True)
        actual = eventually(lambda: (s if (s := app())[kind+'_closes'] == count else None))
        report['checks'].append({'target_pid': pid, 'target_address': address, 'kind': kind, 'count': count,
                                 'dispatch_stdout': result.stdout, 'application': actual})
        if kind == 'child':
            assert actual['main_open'] and actual['main_closes'] == 0
            assert actual['child_open'] == (count == 1)
        elif count == 1:
            assert actual['main_open']
        if actual['main_open']:
            assert cli('status')['running']
            assert cli('collaboration')['seat_paused']
            cli('observe', str(out/f'{kind}-{count}.png'))
    eventually(lambda: not cli('status')['running'])
    report['status'] = 'PASS'
except BaseException as error:
    report['status'] = 'FAIL'
    report['error'] = repr(error)
    raise
finally:
    report['cleanup'] = cli('stop')
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({'status': report['status'], 'report': str(out/'report.json')}))
