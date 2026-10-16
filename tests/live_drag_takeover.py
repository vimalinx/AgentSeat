#!/usr/bin/env python3
"""Nested backend verifies drag ownership; no physical host input is injected."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--report-dir',type=Path,required=True)
p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat');a=p.parse_args()
out=a.report_dir.resolve();out.mkdir(parents=True,exist_ok=False)
sdk=a.cli.resolve().parents[1]/'share/python';sys.path.insert(0,str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session
instance='drag-takeover-'+str(os.getpid());outer=None;inner=None
report={'status':'RUNNING','input_source':'outer private seat -> inner Wayland backend; not physical hardware','tests':{}}
env={**os.environ,'WLR_RENDERER':'pixman','GDK_GL':'disable','GSK_RENDERER':'cairo','LIBGL_ALWAYS_SOFTWARE':'1'}
def cli(*argv):
 r=subprocess.run([str(a.cli),'--instance',instance,*argv],cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
 if r.returncode:raise RuntimeError(r.stderr)
 return json.loads(r.stdout)
def ui():return json.loads((out/'ui.json').read_text())
def eventually(fn,timeout=5):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  r=fn()
  if r:return r
  time.sleep(.02)
 raise AssertionError('drag event postcondition did not arrive')
def rpc(method,params=None):
 params={'session_id':inner['session_id'],'controller_id':'drag-test',**(params or {})}
 if method.startswith('pointer.'):
  lease=rpc('control.acquire',{'ttl_ms':10000})
  params.update(lease_id=lease['lease_id'],control_epoch=lease['control_epoch'])
 params['request_id']=time.monotonic_ns()
 with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
  sock.settimeout(3);sock.connect(inner['socket'])
  pid,uid,_=struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
  assert pid==inner['daemon_pid'] and uid==os.getuid()
  sock.sendall((json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params})+'\n').encode())
  data=b''
  while b'\n' not in data:
   chunk=sock.recv(16384)
   if not chunk or len(data)>1048576:raise RuntimeError('invalid daemon response')
   data+=chunk
 r=json.loads(data.split(b'\n')[0])
 if 'error' in r:raise RuntimeError(r)
 return r['result']
def point(name):
 s=ui();x,y,w,h=s[name];return ((x+w/2)/s['size'][0],(y+h/2)/s['size'][1])
def begin_drag(sender,count):
 source=point('source');target=point('target')
 sender('pointer.move_absolute',{'x':source[0],'y':source[1]})
 time.sleep(.1)
 sender('pointer.button',{'button':272,'pressed':True,'hold_ms':5000})
 for i in range(1,13):
  sender('pointer.move_absolute',{'x':source[0]+(target[0]-source[0])*i/12,'y':source[1]+(target[1]-source[1])*i/12});time.sleep(.03)
 eventually(lambda:ui()['starts']==count)
 time.sleep(.1)
def end_drag(sender,drops):
 sender('pointer.button',{'button':272,'pressed':False})
 eventually(lambda:ui()['drops']==drops and ui()['ends']==ui()['starts'])
 return ui()
try:
 report['launch']=cli('run','--host-workspace','115','--','/usr/bin/python3',str(ROOT/'tests/nested_control_fixture.py'),
  '--out',str(out),'--fixture','drag','--bin-dir',str(a.cli.resolve().parent))
 eventually(lambda:(out/'nested.json').exists());inner=json.loads((out/'nested.json').read_text())
 outer=Session.from_instance(instance,controller='outer-drag-test')
 report['outer_geometry']=outer.rpc('session.heartbeat')['output']
 report['before']=cli('observe',str(out/'before.png'))
 begin_drag(rpc,1);report['tests']['ordinary_agent_drop']=end_drag(rpc,1)
 begin_drag(rpc,2);report['before_takeover']=rpc('session.heartbeat');os.kill(inner['daemon_pid'],signal.SIGSTOP)
 try:
  report['backend_move']=outer.rpc('pointer.move_absolute',{'x':.9,'y':.8})
  report['backend_click']=outer.rpc('pointer.click',{'button':272})
  eventually(lambda:ui()['ends']==2)
  assert ui()['drops']==1 and ui()['failures']>=1,ui()
  report['tests']['frozen_daemon_takeover_cancels_agent_drag']=ui()
 finally:
  os.kill(inner['daemon_pid'],signal.SIGCONT)
  report['after_takeover']=rpc('session.heartbeat')
 eventually(lambda:not rpc('collaboration.status')['human_active'])
 report['before_backend_drag']=cli('observe',str(out/'before-backend-drag.png'))
 begin_drag(outer.rpc,3)
 report['physical_pause']=rpc('seat.pause')
 time.sleep(.1);assert ui()['ends']==2 and ui()['drops']==1,ui()
 report['tests']['backend_drag_survives_agent_pause']=end_drag(outer.rpc,2)
 report['after']=cli('observe',str(out/'after.png'))
 report['status']='PASS'
except BaseException as error:
 report['status']='FAIL';report['error']=repr(error)
 try:report['last_ui']=ui()
 except Exception:pass
 raise
finally:
 if inner:
  try:os.kill(inner['daemon_pid'],signal.SIGCONT)
  except ProcessLookupError:pass
 if outer:
  try:outer.release()
  except Exception as error:report['release_error']=repr(error)
 try:report['stop']=cli('stop')
 except Exception as error:report['stop_error']=repr(error)
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
