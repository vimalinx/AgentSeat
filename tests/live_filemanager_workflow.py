#!/usr/bin/env python3
"""Thunar rename/create/drag/cancel with independent file-system postconditions."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat')
p.add_argument('--report-dir',type=Path,required=True);a=p.parse_args()
out=a.report_dir.resolve();out.mkdir(parents=True,exist_ok=False)
files=out/'files';files.mkdir();content='AgentSeat 文件管理验收\n'
(files/'seed.txt').write_text(content)
sdk=a.cli.resolve().parents[1]/'share/python';sys.path.insert(0,str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session,AgentSeatError

instance='files-workflow-'+str(os.getpid());c=None;editor=None;editor_started=False
report={'status':'RUNNING','steps':[]}
env={**os.environ,'WLR_RENDERER':'pixman','GDK_GL':'disable','GSK_RENDERER':'cairo','LIBGL_ALWAYS_SOFTWARE':'1'}
def cli(*argv):
 r=subprocess.run([str(a.cli),'--instance',instance,*argv],cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
 if r.returncode:raise RuntimeError(r.stderr)
 return json.loads(r.stdout)
def editor_cli(*argv):
 r=subprocess.run([str(a.cli),'--instance',instance+'-editor',*argv],cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
 if r.returncode:raise RuntimeError(r.stderr)
 return json.loads(r.stdout)
def record(name,value):
 report['steps'].append({'name':name,'result':value})
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 return value
def eventually(fn,timeout=4):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  try:value=fn()
  except AgentSeatError as error:
   if error.kind!='STALE_NODE_REF':raise
   value=None  # This helper polls observations only, never repeats a write.
  if value:return value
  time.sleep(.04)
 raise AssertionError('application postcondition did not arrive')
def chord(*keys):
 for k in keys:c.rpc('keyboard.key',{'keycode':k,'pressed':True})
 for k in reversed(keys):c.rpc('keyboard.key',{'keycode':k,'pressed':False})
 time.sleep(.1)
def dialog_entry(title,text,button):
 ref=c.find(title,role='dialog');s=c.snapshot(root_ref=ref,limit=128);assert not s['truncated']
 entries=[n for n in s['nodes'] if 'org.a11y.atspi.EditableText' in n['interfaces'] and 'showing' in n['states']]
 assert len(entries)==1
 node=entries[0]['node_ref'];record(title+'-entry',c.set_text(node,text,expect={'kind':'text','node_ref':node,'equals':text}))
 record(title+'-button',c.activate(c.find(button,role='button',root_ref=ref)))
def inventory():return sorted(str(f.relative_to(files)) for f in files.rglob('*'))
def begin_drag(name):
 if not any(w['title']=='files - Thunar' for w in c.rpc('window.list')):
  # Thunar may spring-open a hovered folder before a hold timeout. Cancelled
  # delivery does not undo that independent application navigation.
  chord(29,38);record(name+'-return-location',c.paste(str(files)));chord(28)
  eventually(lambda:any(w['title']=='files - Thunar' for w in c.rpc('window.list')))
 frame=record(name+'-frame',cli('observe',str(out/(name+'-before.png'))))['capture'];g=frame['geometry_version']
 # Real icon positions inspected in files-1/created.png: folder first, file next.
 c.rpc('pointer.move_absolute',{'x':.236,'y':.115,'geometry_version':g})
 record(name+'-press',c.rpc('pointer.button',{'button':272,'pressed':True,'geometry_version':g}))
 time.sleep(.1)
 for i in range(1,11):
  c.rpc('pointer.move_absolute',{'x':.236+(.15-.236)*i/10,'y':.115,'geometry_version':g});time.sleep(.04)
  if i==3:record(name+'-in-progress',cli('observe',str(out/(name+'-in-progress.png'))))
 time.sleep(.2)
 return g
def release():
 try:c.release()
 except AgentSeatError as error:
  if error.kind!='NO_WRITE_LEASE':raise

try:
 report['launch']=cli('run','--a11y','--host-workspace','113','--','/usr/bin/env',
  *[f'XDG_{name}_HOME={out/name.lower()}' for name in ['CONFIG','DATA','CACHE']],'/usr/bin/thunar','--window',str(files))
 c=Session.from_instance(instance,controller='files-workflow')
 frame=record('before',cli('observe',str(out/'before.png')))['capture']
 record('select-seed',c.activate(point=(.15,.12),frame=frame));time.sleep(.1);chord(60)
 dialog_entry('Rename "seed.txt"','renamed-中文.txt','Rename')
 source=files/'renamed-中文.txt';target=files/'归档/renamed-中文.txt'
 eventually(source.is_file);assert not(files/'seed.txt').exists();assert source.read_text()==content
 record('renamed-files',inventory());chord(29,42,49)
 dialog_entry('Create New Folder','归档','Create');eventually((files/'归档').is_dir)
 record('created-files',inventory());record('created',cli('observe',str(out/'created.png')))
 for mode in ['pause','release','hold-timeout']:
  begin_drag('cancel-'+mode)
  if mode=='pause':record('pause',cli('pause'));release()
  elif mode=='release':record('release',c.release())
  else:
   eventually(lambda:not c.rpc('session.heartbeat')['gate_allowed'],timeout=3)
   release()
  time.sleep(.3)
  observed=record('cancel-'+mode+'-files',inventory())
  assert source.is_file() and not target.exists(),{'unexpected_drop':mode,'files':observed}
  assert source.read_text()==content
  record('cancel-'+mode+'-capture',cli('observe',str(out/('cancel-'+mode+'.png'))))
  if mode=='pause':record('resume',cli('resume'))
 g=begin_drag('complete-drag')
 record('drop',c.rpc('pointer.button',{'button':272,'pressed':False,'geometry_version':g}))
 eventually(target.is_file);assert not source.exists();assert target.read_text()==content
 record('moved-files',inventory())
 record('moved',cli('observe',str(out/'moved.png')))
 # Open the resulting folder through the application's location field.
 chord(29,38);record('location-paste',c.paste(str(target.parent)));chord(28)
 eventually(lambda:any(w['title']=='归档 - Thunar' for w in c.rpc('window.list')))
 record('result',cli('observe',str(out/'result.png')))
 release()
 record('editor-launch',editor_cli('run','--a11y','--host-workspace','114','--',
  '/usr/bin/env',*[f'XDG_{name}_HOME={out/("editor-"+name.lower())}' for name in ['CONFIG','DATA','CACHE']],
  '/usr/bin/mousepad','--disable-server',str(target)))
 editor_started=True
 editor=Session.from_instance(instance+'-editor',controller='files-editor-workflow')
 assert editor.session_id!=c.session_id
 def opened():
  node=editor.find('',role='text',interface='EditableText',states=['showing'])
  return node if editor.text(node)['text']==content else None
 node=eventually(opened)
 record('opened-in-editor',editor_cli('observe',str(out/'editor-opened.png')))
 updated=content+'跨应用编辑已保存。\n'
 record('editor-edit',editor.set_text(node,updated,expect={'kind':'text','node_ref':node,'equals':updated}))
 for k,d in [(29,True),(31,True),(31,False),(29,False)]:editor.rpc('keyboard.key',{'keycode':k,'pressed':d})
 eventually(lambda:target.read_text()==updated)
 record('cross-application-file',{'path':str(target),'content':target.read_text(),
  'file_manager_session':c.session_id,'editor_session':editor.session_id})
 record('editor-result',editor_cli('observe',str(out/'editor-result.png')))
 assert any(w['title']=='归档 - Thunar' for w in c.rpc('window.list'))
 report['status']='PASS'
except BaseException as error:
 report['status']='FAIL';report['error']=repr(error)
 if isinstance(error,AgentSeatError):report['error_receipt']=error.receipt
 raise
finally:
 if editor:
  try:editor.release()
  except Exception as error:report['editor_release_error']=repr(error)
 if editor_started:
  try:report['editor_stop']=editor_cli('stop')
  except Exception as error:report['editor_stop_error']=repr(error)
 if c:
  try:release()
  except Exception as error:report['release_error']=repr(error)
 try:report['stop']=cli('stop')
 except Exception as error:report['stop_error']=repr(error)
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
