#!/usr/bin/env python3
"""Real GTK/Qt semantic actions, postconditions and saved application results."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--report-dir',type=Path,required=True)
p.add_argument('--toolkit',choices=['gtk','qt','both'],default='both');p.add_argument('--workspace',default='109')
p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat');args=p.parse_args()
sdk=args.cli.resolve().parents[1]/'share/python'
sys.path.insert(0,str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session,AgentSeatError
out=args.report_dir.resolve();out.mkdir(parents=True,exist_ok=True)
report={'status':'RUNNING','tests':{},'cli':str(args.cli.resolve()),'sdk':str(Path(sys.modules['agentseat_client'].__file__).resolve())}
env={**os.environ,'WLR_RENDERER':'pixman','GSK_RENDERER':'cairo','LIBGL_ALWAYS_SOFTWARE':'1','QT_QUICK_BACKEND':'software'}

def eventually(predicate, timeout=3):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  result=predicate()
  if result:return result
  time.sleep(.02)
 raise AssertionError('application effect did not arrive')

def failure(fn, kind):
 try:fn()
 except AgentSeatError as error:
  assert error.kind==kind,(error.kind,error.receipt)
  return error.receipt
 raise AssertionError('expected '+kind)

try:
 for toolkit in (['gtk','qt'] if args.toolkit=='both' else [args.toolkit]):
  case={};report['tests'][toolkit]=case
  instance=f'semantic-{toolkit}-{os.getpid()}'
  folder=out/toolkit;folder.mkdir(exist_ok=True)
  command=[str(args.cli),'--instance',instance]
  def cli(*argv):
   r=subprocess.run([*command,*argv],cwd=ROOT,env=env,capture_output=True,text=True,timeout=25)
   if r.returncode:raise RuntimeError(r.stderr)
   return json.loads(r.stdout)
  def ui():return json.loads((folder/'ui.json').read_text())
  client=None
  try:
   fixture=ROOT/'tests'/('semantic_fixture.py' if toolkit=='gtk' else 'semantic_fixture_qt.py')
   case['launch']=cli('run','--a11y','--host-workspace',args.workspace,'--','/usr/bin/python3',str(fixture),'--state',str(folder/'ui.json'),'--output-dir',str(folder/'documents'))
   client=Session.from_instance(instance,controller='semantic-acceptance-'+toolkit)
   case['capabilities']=client.capabilities()
   snapshot=client.snapshot(limit=128)
   assert not snapshot['truncated'],snapshot
   assert 'PRIVATE_SENTINEL' not in json.dumps(snapshot)
   protected=next(n for n in snapshot['nodes'] if n['protected'])
   case['protected_read']=failure(lambda:client.text(protected['node_ref']),'PROTECTED_CONTENT')
   case['protected_write']=failure(lambda:client.set_text(protected['node_ref'],'forbidden'),'PROTECTED_CONTENT')
   assert case['protected_write']['submitted']==0
   count=client.find('Count',role='button',interface='Action')
   node=client.node(count)
   assert 'sensitive' in node['states'] and node['actions']
   case['count_node']=node
   cursor=client.subscribe()
   case['activate']=client.activate(count)
   assert case['activate']['route']=='semantic'
   eventually(lambda:ui()['count']==1)
   case['count_wait']=client.wait({'kind':'exists','selector':{'name':'Count 1','exact':True}})
   assert case['count_wait']['status']=='satisfied'
   title=client.find('Document title',interface='EditableText');text=toolkit+' semantic 中文 document'
   case['title']=client.set_text(title,text,expect={'kind':'text','node_ref':title,'equals':text})
   assert case['title']['verification']=='condition_satisfied'
   assert client.text(title,start=0,count=4096)['text']==text
   slider=client.find('Level',interface='Value')
   case['value']=client.set_value(slider,42,expect={'kind':'value','node_ref':slider,'equals':42})
   eventually(lambda:ui()['level']==42)
   try:case['focus']=client.act('a11y.focus',title,expect={'kind':'state','node_ref':title,'state':'focused'})
   except AgentSeatError as error:
    assert error.kind=='org.freedesktop.DBus.Error.NotSupported',error.receipt
    case['focus']={'support':'application_rejected', 'receipt':error.receipt}
   try:case['scroll']=client.act('a11y.scroll_to',count)
   except AgentSeatError as error:
    assert error.kind in {'org.freedesktop.DBus.Error.NotSupported','org.freedesktop.DBus.Error.UnknownObject','APPLICATION_REJECTED_ACTION'},error.receipt
    case['scroll']={'support':'application_rejected','receipt':error.receipt}
   selection=client.find('Choices',interface='Selection')
   case['select']=client.act('a11y.select',selection,index=1)
   eventually(lambda:ui()['selected']==1)
   disabled=client.find('Disabled action',role='button',interface='Action')
   case['disabled']=failure(lambda:client.perform(disabled,0),'NODE_DISABLED');assert case['disabled']['submitted']==0
   checkbox=client.find('Enable option',role='check-box',interface='Action')
   if client.node(checkbox)['actions']:
    case['check']=client.activate(checkbox);eventually(lambda:ui()['checked'])
   else:case['check']={'support':'no_semantic_action','result':'visual_target_required'}
   case['false_condition']=client.wait({'kind':'exists','selector':{'name':'Never exists','exact':True}},timeout_ms=200)
   assert case['false_condition']['status']=='deadline_exceeded'
   pending=client.read('wait.start',condition={'kind':'exists','selector':{'name':'Never exists','exact':True}},timeout_ms=10000)
   assert client.read('wait.cancel',wait_id=pending['wait_id'])['status']=='cancelled'
   case['cancel_wait']='PASS'
   save_as=client.find('Save as',role='button',interface='Action')
   case['open']=client.activate(save_as)
   case['dialog_wait']=client.wait({'kind':'exists','selector':{'name':'File name','exact':True}})
   assert case['dialog_wait']['status']=='satisfied'
   filename=client.find('File name',interface='EditableText')
   case['filename_node']=client.node(filename)
   case['filename']=client.set_text(filename,'semantic-saved.txt')
   case['dialog_capture']=cli('observe',str(folder/'dialog.png'))
   save=client.find('Save document',role='button',interface='Action')
   case['save']=client.activate(save)
   case['closed']=client.wait({'kind':'absent','selector':{'name':'File name','exact':True}})
   assert case['closed']['status']=='satisfied'
   case['stale_node']=failure(lambda:client.node(filename),'STALE_NODE_REF')
   target=folder/'documents/semantic-saved.txt'
   assert target.read_text()==text
   case['file']={'path':str(target),'text':target.read_text()}
   case['events'],_=client.events(cursor,limit=128)
   assert case['events']['events'] and all(not event['payload_read'] for event in case['events']['events'])
   assert text not in json.dumps(case['events'])
   count=client.find('Count',role='button')
   bounds=client.node(count)['bounds']
   assert bounds['mapping'] in {'compositor_verified','compositor_scaled'},bounds
   frame=cli('observe',str(folder/'coordinate-before.png'))['capture']
   output=client.rpc('session.heartbeat')['output']
   assert frame['geometry_version']==bounds['geometry_version']==output['geometry_version']
   before=ui()['count']
   point=((bounds['x']+bounds['width']/2)/output['width'],(bounds['y']+bounds['height']/2)/output['height'])
   case['coordinate_click']={'bounds':bounds,'point':point,'receipt':client.activate(point=point,frame=frame)}
   eventually(lambda:ui()['count']==before+1)
   case['coordinate_click']['actual_count']=ui()['count']
   case['after_capture']=cli('observe',str(folder/'after.png'))
   case['ui']=ui();case['status']='PASS'
  finally:
   if client:
    try:client.release()
    except AgentSeatError:pass
   case['stop']=cli('stop')
 report['status']='PASS'
except BaseException as error:
 report.update(status='FAIL',error=repr(error));raise
finally:
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'status':report['status'],'toolkits':list(report['tests'])}))
