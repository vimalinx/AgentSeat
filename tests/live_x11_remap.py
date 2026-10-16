#!/usr/bin/env python3
"""Verify retained X11 dialogs can resize while unmapped and map again."""
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
instance = f'x11-remap-{os.getpid()}'
command = [str(a.cli.resolve()), '--instance', instance]
report = {'status': 'RUNNING', 'cli': str(a.cli.resolve())}

def cli(*argv):
    return json.loads(subprocess.check_output([*command, *argv], cwd=ROOT, timeout=30))

try:
    binary = out / 'x11-remap-fixture'
    subprocess.run(['cc', '-Wall', '-Wextra', str(ROOT / 'tests/x11_remap_fixture.c'),
                    '-lX11', '-o', str(binary)], check=True)
    result = out / (instance + '-fixture.json')
    report['launch'] = cli('run', '--x11', '--host-workspace', a.workspace, '--', str(binary), str(result))
    deadline = time.monotonic() + 20
    while not result.exists():
        assert cli('status')['running'], 'microhost exited during unmapped configure'
        if time.monotonic() > deadline:
            raise AssertionError('fixture did not finish 20 remaps')
        time.sleep(.2)
    report['fixture'] = json.loads(result.read_text())
    assert report['fixture'] == {'cycles': 20, 'width': 339, 'height': 219, 'mapped': True}
    report['final'] = cli('status')
    assert report['final']['running']
    assert any(w['width'] == 339 and w['height'] == 219 for w in report['final']['windows'])
    report['capture'] = cli('observe', str(out / 'after.png'))
    report['status'] = 'PASS'
except BaseException as error:
    report.update(status='FAIL', error=str(error))
    raise
finally:
    report['cleanup'] = cli('stop')
    (out / 'report.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'status': report['status'], 'cycles': report['fixture']['cycles']}))
