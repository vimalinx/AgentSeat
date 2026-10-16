#!/usr/bin/env python3
"""Real GTK 3 modal workflow; all document writes happen through application UI.

The test never retries a submitted action. A stale target with an explicit zero
submission receipt permits fresh lookup; unknown outcomes stop the workflow.
Read-only polling checks effects independently of AT-SPI acknowledgements.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--report-dir', type=Path, required=True)
parser.add_argument('--cli', type=Path, default=ROOT / 'bin/agentseat')
parser.add_argument('--workspace', default='110')
args = parser.parse_args()
sdk = args.cli.resolve().parents[1] / 'share/python'
sys.path.insert(0, str(sdk if sdk.is_dir() else ROOT / 'python'))
from agentseat_client import Session, AgentSeatError

out = args.report_dir.resolve()
out.mkdir(parents=True, exist_ok=True)
target = out / '中文 保存.txt'
cancelled = out / '不应生成.txt'
assert not target.exists() and not cancelled.exists(), 'use a fresh report directory'
instance = 'mousepad-workflow-' + str(os.getpid())
env = {**os.environ, 'WLR_RENDERER': 'pixman', 'GDK_GL': 'disable',
       'GSK_RENDERER': 'cairo', 'LIBGL_ALWAYS_SOFTWARE': '1'}
report = {'status': 'RUNNING', 'cli': str(args.cli.resolve()),
          'sdk': str(Path(sys.modules['agentseat_client'].__file__).resolve()), 'steps': {}}
client = None


def cli(*argv):
    result = subprocess.run([str(args.cli), '--instance', instance, *argv],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def step(name, fn, *, refresh_stale=False):
    started = time.monotonic()
    rejected = []
    # A freshly launched real application may still reparent widgets. Refuse
    # stale references, then re-observe only when the daemon proves no dispatch.
    while True:
        try:
            result = fn()
            break
        except AgentSeatError as error:
            if not refresh_stale or error.kind != 'STALE_NODE_REF' or not isinstance(error.receipt, dict) or error.receipt.get('submitted') != 0 or len(rejected) >= 3:
                raise
            rejected.append(error.receipt)
    report['steps'][name] = {'elapsed_ms': round((time.monotonic() - started) * 1000, 2),
                            'zero_submission_recovery': rejected, 'result': result}
    return result


def eventually(fn, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('application postcondition did not arrive')


def find(name, **params):
    return client.find(name, max_nodes=2048, **params)


def editor():
    return find('', role='text', interface='EditableText', states=['showing'])


def save_as():
    menu = find('File', role='menu', states=['showing'])
    client.activate(menu)
    eventually(lambda: client.query('Save As...      ', role='menu-item', root_ref=menu,
                                    states=['showing'])['total'] == 1)
    receipt = client.activate(find('Save As...      ', role='menu-item', root_ref=menu, states=['showing']))
    eventually(lambda: client.query('Save As', role='file-chooser', max_nodes=2048)['total'] == 1)
    return receipt


def dialog():
    return find('Save As', role='file-chooser')


def filename():
    return find('', role='text', interface='EditableText', states=['showing', 'single-line'], root_ref=dialog())


def dialog_action(name):
    return client.activate(find(name, role='button', root_ref=dialog(), states=['showing']))


def closed():
    # Absence requires a complete traversal, not an empty truncated page.
    result = client.query('Save As', role='file-chooser', max_nodes=2048)
    return not result['truncated'] and result['total'] == 0


def expect_stale(ref):
    try:
        client.node(ref)
    except AgentSeatError as error:
        assert error.kind == 'STALE_NODE_REF', error.kind
        return {'kind': error.kind}
    raise AssertionError('destroyed file dialog reference remained usable')


try:
    report['launch'] = cli('run', '--a11y', '--cwd', str(out), '--host-workspace', args.workspace, '--',
                          '/usr/bin/env', 'LC_ALL=C.UTF-8', 'XDG_CONFIG_HOME=' + str(out / 'config'),
                          'XDG_DATA_HOME=' + str(out / 'data'), 'XDG_CACHE_HOME=' + str(out / 'cache'),
                          '/usr/bin/mousepad', '--disable-server')
    client = Session.from_instance(instance, controller='mousepad-workflow')
    report['capabilities'] = client.capabilities()
    assert report['capabilities']['a11y']['atk_dispatch'] == 'deferred-outside-dbus'
    body = 'AgentSeat 真实编辑验收\n中文、emoji 😀、café、组合 e\u0301\n第二行 initial\n'
    step('write_unicode', lambda: client.set_text(editor(), body), refresh_stale=True)
    assert client.text(editor(), count=4096)['text'] == body
    step('open_save_as', save_as)
    field = filename()
    # Key events inside a semantic-opened modal triggered the original deadlock.
    step('modal_keyboard', lambda: client.await_operation(client.rpc('keyboard.type', {'text': 'KEYBOARD-PROBE.txt'})))
    eventually(lambda: client.text(field)['text'] == 'KEYBOARD-PROBE.txt')
    step('set_filename', lambda: client.set_text(field, str(target)))
    report['dialog_capture'] = cli('observe', str(out / 'dialog.png'))
    step('save_new', lambda: dialog_action('Save'))
    eventually(lambda: target.exists() and target.read_text() == body)
    eventually(closed)
    step('old_dialog_ref', lambda: expect_stale(field))
    step('open_cancel', save_as)
    step('set_cancelled_filename', lambda: client.set_text(filename(), str(cancelled)))
    step('cancel_dialog', lambda: dialog_action('Cancel'))
    eventually(closed)
    assert not cancelled.exists() and target.read_text() == body
    updated = body + '追加 overwrite\n'
    step('edit_before_overwrite', lambda: client.set_text(editor(), updated), refresh_stale=True)
    step('open_overwrite', save_as)
    step('request_overwrite', lambda: dialog_action('Save'))
    eventually(lambda: client.query('Question', role='alert', max_nodes=2048)['total'] == 1)
    question = find('Question', role='alert')
    assert target.read_text() == body, 'overwrite happened before confirmation'
    report['confirmation_capture'] = cli('observe', str(out / 'confirmation.png'))
    step('cancel_overwrite', lambda: client.activate(find('Cancel', role='button', root_ref=question)))
    eventually(lambda: client.query('Question', role='alert', max_nodes=2048)['total'] == 0)
    assert target.read_text() == body
    step('request_overwrite_again', lambda: dialog_action('Save'))
    eventually(lambda: client.query('Question', role='alert', max_nodes=2048)['total'] == 1)
    question = find('Question', role='alert')
    step('confirm_replace', lambda: client.activate(find('Replace', role='button', root_ref=question)))
    eventually(lambda: target.read_text() == updated)
    eventually(closed)
    assert client.text(editor(), count=4096)['text'] == updated
    report['file'] = {'path': str(target), 'text': target.read_text(), 'cancelled_path_absent': not cancelled.exists()}
    report['after_capture'] = cli('observe', str(out / 'after.png'))
    report['final_status'] = cli('status')
    report['status'] = 'PASS'
except BaseException as error:
    report.update(status='FAIL', error=repr(error))
    if isinstance(error, AgentSeatError):
        report['error_receipt'] = error.receipt
    try:
        report['failure_capture'] = cli('observe', str(out / 'failure.png'))
    except Exception as capture_error:
        report['capture_error'] = repr(capture_error)
    raise
finally:
    if client:
        try:
            client.release()
        except AgentSeatError:
            pass
    report['stop'] = cli('stop')
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({'status': report['status'], 'steps': len(report['steps']), 'report': str(out / 'report.json')}))
