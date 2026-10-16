#!/usr/bin/env python3
"""Writer GUI edits and dialogs, with AT-SPI and GUI-created ODT verification.

Uses a private profile. No UNO, document generation, host input or clipboard.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--cli', type=Path, default=ROOT/'bin/agentseat')
p.add_argument('--report-dir', type=Path, required=True)
p.add_argument('--splash', action='store_true', help='also verify startup with the ordinary splash screen')
a = p.parse_args()
out = a.report_dir.resolve(); out.mkdir(parents=True, exist_ok=False)
sdk = a.cli.resolve().parents[1]/'share/python'
sys.path.insert(0, str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session, AgentSeatError

instance = 'office-workflow-'+str(os.getpid())
env = {**os.environ, 'WLR_RENDERER':'pixman', 'GDK_GL':'disable',
       'GSK_RENDERER':'cairo', 'LIBGL_ALWAYS_SOFTWARE':'1', 'GDK_BACKEND':'wayland'}
report = {'status':'RUNNING', 'steps':[]}; c = None
initial = ['AgentSeat 办公验收', '第一行：中文与 emoji 🧭', 'Find OLD and keep café.']
replaced = [*initial[:2], initial[2].replace('OLD','NEW')]

def cli(*argv):
    result = subprocess.run([str(a.cli), '--instance', instance, *argv], env=env,
                            capture_output=True, text=True, timeout=30, cwd=ROOT)
    if result.returncode: raise RuntimeError(result.stderr)
    return json.loads(result.stdout)

def record(name, value):
    report['steps'].append({'name':name, 'result':value})
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return value

def eventually(fn, timeout=5):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            value = fn()
        except AgentSeatError as error:
            if error.kind not in {'STALE_NODE_REF','TEXT_STATE_CHANGED'}: raise
            value = None  # Read-only rediscovery; never replay a submitted action.
        if value: return value
        time.sleep(.04)
    raise AssertionError('application postcondition did not arrive')

def snapshot(**kwargs):
    s = c.snapshot(limit=128, **kwargs); nodes = s['nodes'][:]
    while s['next_offset'] is not None:
        s = c.snapshot(snapshot_id=s['snapshot_id'], offset=s['next_offset'], limit=128)
        nodes.extend(s['nodes'])
    return {**s, 'nodes':nodes}

def top(role, name=None):
    # Shallow discovery selects an observed native application child. It does
    # not infer global uniqueness from a depth-limited tree.
    nodes = snapshot(max_depth=1)['nodes']
    return next((n['node_ref'] for n in nodes if n['role_key']==role and (name is None or n['name']==name)), None)

def document():
    nodes = snapshot(max_depth=7, max_nodes=1024)['nodes']
    candidate = next(n for n in nodes if n['role_key']=='document-text')
    s = snapshot(root_ref=candidate['node_ref'])
    assert not s['truncated'], s
    return [n for n in s['nodes'] if 'org.a11y.atspi.Text' in n['interfaces']]

def paragraphs(): return [c.text(n['node_ref'])['text'] for n in document()]

def expect_text(expected):
    return eventually(lambda: (actual if (actual:=paragraphs())==expected else None))

def chord(*keys):
    receipts = []
    for key in keys: receipts.append(c.rpc('keyboard.key', {'keycode':key,'pressed':True}))
    for key in reversed(keys): receipts.append(c.rpc('keyboard.key', {'keycode':key,'pressed':False}))
    # Application processing is asynchronous. This settling interval is not
    # evidence of success; the caller separately verifies caret/text/UI state.
    time.sleep(.1)
    return receipts

def last_state(): return c.text_state(document()[-1]['node_ref'])

try:
    report['launch'] = cli('run','--a11y','--host-workspace','112','--', '/usr/bin/env',
        'SAL_USE_VCLPLUGIN=gtk3','SAL_DISABLESKIA=1','SAL_DISABLEGL=1','SAL_DISABLE_OPENCL=1',
        *[f'XDG_{name}_HOME={out/name.lower()}' for name in ['CONFIG','DATA','CACHE']],
        '/usr/bin/libreoffice','-env:UserInstallation='+(out/'profile').as_uri(),
        *([] if a.splash else ['--nologo']),'--norestore','--writer')
    c = Session.from_instance(instance, controller='office-workflow')
    record('capabilities', c.capabilities())
    if a.splash:
        welcome = eventually(lambda:top('dialog','Welcome to LibreOffice!'))
        record('welcome',cli('observe',str(out/'welcome.png')))
        for index in range(2):
            record('welcome-next-'+str(index),c.activate(c.find('Next',role='button',root_ref=welcome,states=['showing'])))
            time.sleep(.1)
        record('welcome-close',c.activate(c.find('Close',role='button',root_ref=welcome,states=['showing'])))
        eventually(lambda:not top('dialog','Welcome to LibreOffice!'))
    record('before', cli('observe', str(out/'before.png')))
    # The Writer page/caret was visually located in office-2/before.png.
    frame = cli('observe',str(out/'pre-edit.png'))['capture']
    record('page-click', c.activate(point=(.27,.24),frame=frame))
    record('unicode-paste', c.paste('\n'.join(initial)))
    record('unicode-actual', expect_text(initial))
    chord(29,35)
    dialog = eventually(lambda:top('dialog','Find and Replace'))
    tree = snapshot(root_ref=dialog); assert not tree['truncated']
    entries = [c.node(n['node_ref']) for n in tree['nodes']
               if 'org.a11y.atspi.EditableText' in n['interfaces'] and 'showing' in n['states']]
    assert len(entries)==2
    entries.sort(key=lambda n:n['bounds']['y'])
    for n,text in zip(entries,['OLD','NEW']):
        record('find-field-'+text,c.set_text(n['node_ref'],text,
               expect={'kind':'text','node_ref':n['node_ref'],'equals':text}))
    record('replace',c.activate(c.find('Replace All',role='button',root_ref=dialog)))
    record('replace-notification',c.find('Search key replaced 1 times.',root_ref=dialog))
    record('close-find',c.activate(c.find('Close',role='button',root_ref=dialog)))
    record('replaced-actual',expect_text(replaced))
    chord(29,44); record('undo-actual',expect_text(initial))
    chord(29,21); record('redo-actual',expect_text(replaced))
    chord(29,107)
    record('end-caret',eventually(lambda:(s if (s:=last_state())['caret_offset']==len(replaced[-1]) else None)))
    chord(102)
    record('home-caret',eventually(lambda:(s if (s:=last_state())['caret_offset']==0 else None)))
    chord(42,107)
    record('selected-line',eventually(lambda:(s if (s:=last_state())['selections']==[{'index':0,'start':0,'end':len(replaced[-1])}] else None)))
    record('selection-capture',cli('observe',str(out/'selection.png')))
    chord(29,46); chord(107); chord(28)
    record('new-paragraph',expect_text([*replaced,'']))
    chord(29,47)
    expected = [*replaced,replaced[-1]]
    record('copy-paste-actual',expect_text(expected))
    record('copied-capture',cli('observe',str(out/'copied.png')))
    chord(29,42,31)
    chooser = eventually(lambda:top('file-chooser','Save'))
    tree = snapshot(root_ref=chooser); assert not tree['truncated']
    entries = [n for n in tree['nodes'] if 'org.a11y.atspi.EditableText' in n['interfaces'] and 'showing' in n['states']]
    assert len(entries)==1
    ref = entries[0]['node_ref']; target = out/'办公验收.odt'
    record('save-name',c.set_text(ref,str(target),expect={'kind':'text','node_ref':ref,'equals':str(target)}))
    record('save-dialog',cli('observe',str(out/'save.png')))
    record('save',c.activate(c.find('Save',role='button',root_ref=chooser)))
    eventually(lambda:target.is_file())
    with zipfile.ZipFile(target) as z: xml = ET.fromstring(z.read('content.xml'))
    actual = [''.join(n.itertext()) for n in xml.findall('.//{urn:oasis:names:tc:opendocument:xmlns:text:1.0}p')]
    assert actual==expected, actual
    record('odt-file-actual',actual)
    record('saved-ui',expect_text(expected))
    record('result',cli('observe',str(out/'result.png')))
    report['status']='PASS'
except BaseException as error:
    report['status']='FAIL'; report['error']=repr(error)
    if isinstance(error,AgentSeatError): report['error_receipt']=error.receipt
    raise
finally:
    if c:
        try:c.release()
        except Exception as error:report['release_error']=repr(error)
    try:report['stop']=cli('stop')
    except Exception as error:report['stop_error']=repr(error)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
