#!/usr/bin/env python3
"""Independent browser hit counters validate geometry across fractional scale/zoom.

Coordinates come from the semantic bridge; counters come from actual button
clicks. The fixture makes missed and wrong-target clicks observable.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat');p.add_argument('--report-dir',type=Path,required=True);p.add_argument('--workspace',default='113');a=p.parse_args()
out=a.report_dir.resolve();out.mkdir(parents=True,exist_ok=False)
sdk=a.cli.resolve().parents[1]/'share/python';sys.path.insert(0,str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session
instance='geometry-workflow-'+str(os.getpid());c=None;report={'status':'RUNNING','clicks':[]}
env={**os.environ,'WLR_RENDERER':'pixman','GDK_GL':'disable','LIBGL_ALWAYS_SOFTWARE':'1'}
def cli(*argv):
 r=subprocess.run([str(a.cli),'--instance',instance,*argv],env=env,cwd=ROOT,capture_output=True,text=True,timeout=25)
 if r.returncode:raise RuntimeError(r.stderr)
 return json.loads(r.stdout)
def eventually(fn):
 end=time.monotonic()+5
 while time.monotonic()<end:
  r=fn()
  if r:return r
  time.sleep(.05)
 raise AssertionError('coordinate action did not produce its application postcondition')
def click(name,expected):
 ref=c.find(name,role='button',max_nodes=2048);bounds=c.node(ref)['bounds']
 assert bounds['mapping'] in {'compositor_verified','compositor_scaled'},bounds
 frame=cli('observe',str(out/('before-'+str(len(report['clicks']))+'.png')))['capture']
 output=c.rpc('session.heartbeat')['output']
 assert frame['geometry_version']==bounds['geometry_version']==output['geometry_version']
 point=((bounds['x']+bounds['width']/2)/output['width'],(bounds['y']+bounds['height']/2)/output['height'])
 receipt=c.activate(point=point,frame=frame)
 actual=eventually(lambda:(q if (q:=c.query(expected,role='static',max_nodes=2048))['total']==1 else None))
 report['clicks'].append({'target':name,'bounds':bounds,'point':point,'frame':frame,'receipt':receipt,'actual':actual})
try:
 report['launch']=cli('run','--a11y','--host-workspace',a.workspace,'--','/usr/bin/chromium','--disable-gpu','--ozone-platform=wayland','--force-renderer-accessibility','--no-first-run','--no-default-browser-check','--disable-background-networking','--user-data-dir='+str(out/'profile'),(ROOT/'tests/browser_geometry_fixture.html').as_uri())
 c=Session.from_instance(instance,controller='geometry-workflow')
 click('Alpha target','Alpha 1 Beta 0');click('Beta target','Alpha 1 Beta 1')
 # Real browser zoom shortcut. No DOM/CSS runtime injection changes the target.
 for key,down in [(29,True),(13,True),(13,False),(29,False)]:c.rpc('keyboard.key',{'keycode':key,'pressed':down})
 eventually(lambda:not any(w['popup'] for w in c.rpc('window.list')))
 click('Alpha target','Alpha 2 Beta 1');click('Beta target','Alpha 2 Beta 2')
 report['result_capture']=cli('observe',str(out/'result.png'))
 assert report['clicks'][2]['bounds']['width']>report['clicks'][0]['bounds']['width']
 report['status']='PASS'
except BaseException as error:
 report['status']='FAIL';report['error']=repr(error);raise
finally:
 if c:
  try:c.release()
  except Exception as error:report['release_error']=repr(error)
 try:report['stop']=cli('stop')
 except Exception as error:report['stop_error']=repr(error)
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
