#!/usr/bin/env python3
"""Verify application-specified managed dialog and menu positions."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--report-dir', type=Path, required=True)
p.add_argument('--workspace', default='124')
p.add_argument('--cli', type=Path, default=ROOT / 'bin/agentseat')
a = p.parse_args()
out = a.report_dir.resolve()
out.mkdir(parents=True, exist_ok=True)
instance = f'popup-position-{os.getpid()}'
command = [str(a.cli.resolve()), '--instance', instance]
report = {'status': 'RUNNING', 'cli': str(a.cli.resolve())}

def cli(*argv):
    return json.loads(subprocess.check_output([*command, *argv], cwd=ROOT, timeout=30))

try:
    binary = out / 'popup-position-fixture'
    subprocess.run(['cc', '-Wall', '-Wextra', str(ROOT / 'tests/x11_popup_position_fixture.c'),
                    '-lX11', '-o', str(binary)], check=True)
    result = out / (instance + '-fixture.json')
    report['launch'] = cli('run', '--ime', 'simple', '--x11', '--host-workspace', a.workspace, '--', str(binary), str(result))
    deadline = time.monotonic() + 20
    while not result.exists():
        assert cli('status')['running'], 'microhost exited during unmapped configure'
        if time.monotonic() > deadline:
            raise AssertionError('fixture did not finish positioning checks')
        time.sleep(.2)
    report['fixture'] = json.loads(result.read_text())
    assert report['fixture'] == {'status': 'PASS', 'checks': 7}
    report['final'] = cli('status')
    assert report['final']['running']
    report['capture'] = cli('observe', str(out / 'after.png'))
    report['status'] = 'PASS'
except BaseException as error:
    report.update(status='FAIL', error=str(error))
    raise
finally:
    report['cleanup'] = cli('stop')
    (out / 'report.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'status': report['status'], 'checks': report['fixture']['checks']}))
