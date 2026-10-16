#!/usr/bin/env python3
"""Real Chromium form, private Unicode paste, native file chooser and download.

No DOM, CDP, host input or network sites. Outputs are produced by browser UI.
The HTML fixture and input attachment are the only predetermined files.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--cli', type=Path, default=ROOT/'bin/agentseat')
p.add_argument('--report-dir', type=Path, required=True)
p.add_argument('--workspace', default='113')
p.add_argument('--location-input', choices=['paste','keyboard'], default='paste')
a = p.parse_args()
sdk = a.cli.resolve().parents[1]/'share/python'
sys.path.insert(0, str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session
out = a.report_dir.resolve()
out.mkdir(parents=True, exist_ok=False)
(out/'profile/Default').mkdir(parents=True)
(out/'downloads').mkdir()
(out/'profile/Default/Preferences').write_text(json.dumps({'download': {'default_directory': str(out/'downloads'), 'prompt_for_download': False}}))
attachment = '本地附件 attachment proof\n'
(out/'upload.txt').write_text(attachment)
instance = 'browser-workflow-' + str(os.getpid())
env = {**os.environ, 'WLR_RENDERER':'pixman', 'GDK_GL':'disable', 'LIBGL_ALWAYS_SOFTWARE':'1'}
report = {'status':'RUNNING', 'cli':str(a.cli.resolve()), 'sdk':str(Path(sys.modules['agentseat_client'].__file__).resolve()), 'steps':{}}
c = None

def cli(*args):
    r = subprocess.run([str(a.cli), '--instance', instance, *args], env=env, cwd=ROOT, capture_output=True, text=True, timeout=25)
    if r.returncode: raise RuntimeError(r.stderr)
    return json.loads(r.stdout)

def eventually(fn, timeout=5):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        value = fn()
        if value:return value
        time.sleep(.05)
    raise AssertionError('application postcondition did not arrive')

def step(name, fn):
    began=time.monotonic(); value=fn()
    report['steps'][name]={'elapsed_ms':round((time.monotonic()-began)*1000,2),'result':value}
    return value

def find(name, role):return c.find(name,role=role,max_nodes=2048)
def chord(*codes):
    receipts=[]
    for key,down in [(k,True) for k in codes]+[(k,False) for k in reversed(codes)]:
        receipts.append(c.rpc('keyboard.key',{'keycode':key,'pressed':down}))
    return receipts

def paste_field(name, text):
    ref=find(name,'entry')
    c.act('a11y.focus',ref)
    receipt=c.paste(text)
    actual=eventually(lambda:(r if (r:=c.text(ref))['text']==text else None))
    return {'transport':receipt,'application':actual}

try:
    report['launch']=cli('run','--a11y','--cwd',str(out),'--host-workspace',a.workspace,'--',
        '/usr/bin/chromium','--disable-gpu','--ozone-platform=wayland','--force-renderer-accessibility',
        '--no-first-run','--no-default-browser-check','--disable-background-networking',
        '--user-data-dir='+str(out/'profile'),(ROOT/'tests/browser_fixture.html').as_uri())
    c=Session.from_instance(instance,controller='browser-workflow')
    report['capabilities']=c.capabilities()
    expected={'title':'原生中文验收 😀 café e\u0301','notes':'多行内容第一行\n第二行 😀\n',
              'priority':'Urgent','confirmed':True,'attachment':attachment}
    step('title_unicode',lambda:paste_field('Document title',expected['title']))
    step('multiline_unicode',lambda:paste_field('Document notes',expected['notes']))
    step('open_select',lambda:c.activate(find('Priority','combo-box')))
    step('select_urgent',lambda:c.activate(find('Urgent','menu-item')))
    step('check_document',lambda:c.activate(find('Confirm document','check-box')))
    assert 'checked' in c.node(find('Confirm document','check-box'))['states']
    step('open_attachment',lambda:c.activate(find('Attachment: No file chosen','button')))
    report['file_window']=eventually(lambda:next((w for w in c.rpc('window.list') if w['focused'] and w['title']=='Open File'),None))
    # Chromium's GTK chooser need not be represented in its AT-SPI root.
    report['chooser_semantics']=c.read('a11y.query',selector={'role':'file-chooser'},max_nodes=2048,limit=128)
    report['file_dialog_capture']=cli('observe',str(out/'file-dialog.png'))
    step('location_shortcut',lambda:chord(29,38))
    step('location_input',lambda:c.paste(str(out/'upload.txt')) if a.location_input=='paste' else c.await_operation(c.rpc('keyboard.type',{'text':str(out/'upload.txt')})))
    # This delay permits a useful rendered diagnostic, not proof of insertion.
    # Browser file state below is the independent pass/fail condition.
    time.sleep(.2)
    report['location_capture']=cli('observe',str(out/'location.png'))
    # The screenshot's Open button, not Enter: Chromium intentionally makes
    # Cancel the default response. This coordinate is a fixture-specific visual
    # target observed in location.png, guarded by that capture's geometry.
    frame=report['location_capture']['capture']
    step('open_file',lambda:c.activate(point=(.84,.184),frame=frame))
    eventually(lambda:not any(w['title']=='Open File' for w in c.rpc('window.list')))
    def attachment_selected():
        q=c.read('a11y.query',selector={'name':'Attachment:','exact':False},max_nodes=2048,limit=128)
        return q if any(n['name']=='Attachment: upload.txt' for n in q['nodes']) else None
    report['attachment_state']=eventually(attachment_selected)
    target=find('Generate receipt','button')
    step('scroll_to_receipt',lambda:c.act('a11y.scroll_to',target))
    assert 'showing' in c.node(target)['states']
    step('generate_receipt',lambda:c.activate(target))
    step('download_receipt',lambda:c.activate(find('Download receipt','button')))
    file=out/'downloads/browser-receipt.txt'
    eventually(lambda:file.exists())
    report['downloaded']=json.loads(file.read_text())
    assert report['downloaded']==expected,report['downloaded']
    result_node=find(json.dumps(expected,ensure_ascii=False,indent=2),'static')
    step('show_generated_result',lambda:c.act('a11y.scroll_to',result_node))
    report['visible_result']=c.text(result_node,count=4096)
    assert json.loads(report['visible_result']['text'])==expected
    report['result_capture']=cli('observe',str(out/'result.png'))
    report['status']='PASS'
except BaseException as error:
    report['status']='FAIL';report['error']=repr(error)
    try:report['failure_capture']=cli('observe',str(out/'failure.png'))
    except Exception as capture_error:report['capture_error']=repr(capture_error)
    raise
finally:
    if c:
        try:c.release()
        except Exception as error:report['release_error']=repr(error)
    try:report['stop']=cli('stop')
    except Exception as error:report['stop_error']=repr(error)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
