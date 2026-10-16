#!/usr/bin/env python3
"""Bounded native clipboard transport, pause, lease and private ownership tests.

wl-paste reads only this test microhost. No host clipboard content is inspected.
"""
import argparse
import json
import os
import signal
import shlex
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat');p.add_argument('--report-dir',type=Path,required=True)
a=p.parse_args();out=a.report_dir.resolve();out.mkdir(parents=True,exist_ok=False)
protocol=Path('/usr/share/wayland-protocols/staging/ext-data-control/ext-data-control-v1.xml')
subprocess.run(['wayland-scanner','client-header',str(protocol),str(out/'ext-data-control-client.h')],check=True)
subprocess.run(['wayland-scanner','private-code',str(protocol),str(out/'ext-data-control-code.c')],check=True)
flags=shlex.split(subprocess.check_output(['pkg-config','--cflags','--libs','wayland-client'],text=True))
receiver=out/'clipboard-receiver'
subprocess.run(['cc','-Wall','-Wextra','-I'+str(out),str(ROOT/'tests/clipboard_receiver.c'),str(out/'ext-data-control-code.c'),'-o',str(receiver),*flags],check=True)

sdk=a.cli.resolve().parents[1]/'share/python';sys.path.insert(0,str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session,AgentSeatError
instance='clipboard-faults-'+str(os.getpid());c=None;owner=None;readers=[];frozen=set()
report={'status':'RUNNING','tests':{}}
env={**os.environ,'WLR_RENDERER':'pixman','GSK_RENDERER':'cairo','GDK_GL':'disable','LIBGL_ALWAYS_SOFTWARE':'1'}

def cli(*argv):
 r=subprocess.run([str(a.cli),'--instance',instance,*argv],env=env,cwd=ROOT,capture_output=True,text=True,timeout=25)
 if r.returncode:raise RuntimeError(r.stderr)
 return json.loads(r.stdout)
def eventually(fn,seconds=4):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  result=fn()
  if result:return result
  time.sleep(.02)
 raise AssertionError('condition timed out')
def ui():return json.loads((out/'ui.json').read_text())
def clip(types=False):
 r=subprocess.run(['/usr/bin/wl-paste',*(['--list-types'] if types else ['--no-newline'])],env=private,capture_output=True,text=True,timeout=2)
 return r.returncode,r.stdout
def raw(method,params=None,controller=None):
 data={'session_id':c.session_id,'controller_id':controller or c.controller,'request_id':time.monotonic_ns(),**(params or {})}
 return c._exchange(c.runtime/'control.sock',{'jsonrpc':'2.0','id':1,'method':method,'params':data},c.daemon_pid)
def context():return {'lease_id':c.lease['lease_id'],'control_epoch':c.lease['control_epoch']}
def reset():
 ref=c.find('Document title',role='text')
 c.set_text(ref,'')
 node=c.node(ref);b=node['bounds'];w=c.rpc('window.list');scene=c.rpc('seat.status')
 assert b['mapping']=='compositor_verified',b
 # GTK 4 GrabFocus is unsupported: use the actual scoped entry bounds.
 capture=cli('observe',str(out/'focus.png'))['capture']
 width=capture['width'];height=capture['height']
 # The fixture's compositor reports output logical dimensions via windows.
 root=next(item for item in w if item['primary'])
 x=(b['x']+b['width']/2)/root['width'];y=(b['y']+b['height']/2)/root['height']
 c.rpc('pointer.move_absolute',{'x':x,'y':y,'geometry_version':b['geometry_version']})
 c.rpc('pointer.click',{'button':272,'geometry_version':b['geometry_version']})
def expect_error(fn,kind):
 try:fn()
 except AgentSeatError as error:
  assert error.kind==kind,(kind,error.kind);return error.receipt
 raise AssertionError('expected '+kind)
try:
 report['launch']=cli('run','--ime','simple','--a11y','--host-workspace','114','--','/usr/bin/python3',str(ROOT/'tests/semantic_fixture.py'),'--state',str(out/'ui.json'),'--output-dir',str(out/'documents'))
 c=Session.from_instance(instance,controller='clipboard-faults')
 private={**env,'WAYLAND_DISPLAY':report['launch']['state']['wayland_display']};private.pop('DISPLAY',None)
 eventually(lambda:(out/'ui.json').exists())
 reset()
 payload='私有剪贴板 😀 café e\u0301'
 r=c.paste(payload);eventually(lambda:ui()['title']==payload)
 assert clip()==(0,payload);assert clip()==(0,payload)
 report['tests']['multiple_reads_and_ui']={'receipt':r,'actual':ui()['title']}
 # Content must not be republished when another controller tries to paste.
 denied=raw('keyboard.paste',{**context(),'text':'must-not-replace'},'other')
 assert denied['error']['data']['kind']=='NO_WRITE_LEASE',denied
 assert clip()==(0,payload)
 report['tests']['other_writer_cannot_publish']='PASS'
 # Invalid text and held modifiers fail before selection changes.
 for value in ['', 'x'*32769, 'a\x00b']:
  denied=raw('keyboard.paste',{**context(),'text':value})
  assert denied['error']['data']['kind']=='INVALID_PARAMS',denied
 assert clip()==(0,payload)
 c.rpc('keyboard.key',{'keycode':29,'pressed':True})
 denied=raw('keyboard.paste',{**context(),'text':'must-not-replace'})
 assert denied['error']['data']['kind']=='KEYBOARD_PASTE_FAILED',denied
 c.rpc('keyboard.key',{'keycode':29,'pressed':False})
 assert clip()==(0,payload)
 report['tests']['invalid_and_held_keys_do_not_replace']='PASS'
 # Release and pause clear the source through compositor authority.
 c.release();eventually(lambda:clip(True)[0]!=0)
 reset();c.paste('pause proof');eventually(lambda:ui()['title']=='pause proof')
 began=time.perf_counter();c.rpc('seat.pause');elapsed=(time.perf_counter()-began)*1000
 eventually(lambda:clip(True)[0]!=0)
 assert elapsed<100,elapsed
 report['tests']['pause_clears_selection']={'pause_ms':elapsed}
 c.lease=None;c.rpc('seat.resume');reset()
 # The same request ID may be read/replayed, but Ctrl+V happens once.
 params={**context(),'text':'exactly-once','request_id':time.monotonic_ns()}
 first=raw('keyboard.paste',params);assert 'result' in first,first
 assert raw('keyboard.paste',params)['result']==first['result']
 c.await_operation(first['result']);eventually(lambda:ui()['title']=='exactly-once')
 report['tests']['dedup_does_not_paste_twice']={'actual':ui()['title']}
 # Private application-owned replacement survives Agent control revocation.
 owner=subprocess.Popen(['/usr/bin/wl-copy','--foreground','--type','text/plain;charset=utf-8'],env=private,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
 owner.stdin.write('application clipboard');owner.stdin.close()
 eventually(lambda:clip()==(0,'application clipboard'))
 c.release();assert clip()==(0,'application clipboard')
 report['tests']['release_preserves_replacement_source']='PASS'
 owner.terminate();owner.wait(timeout=2);owner=None
 reset();c.paste('short lease');eventually(lambda:ui()['title']=='short lease')
 lease=raw('control.renew',{'lease_id':c.lease['lease_id'],'ttl_ms':150});assert 'result' in lease
 eventually(lambda:clip(True)[0]!=0)
 denied=raw('keyboard.paste',{**context(),'text':'expired'})
 assert denied['error']['data']['kind']=='NO_WRITE_LEASE',denied
 report['tests']['lease_expiry_clears_selection']='PASS'
 c.lease=None
 # A full-size UTF-8 payload uses asynchronous writes, then the read cap
 # bounds repeated consumers. Caller reads never renew the lease implicitly.
 reset()
 large='汉'*10922+'ab';assert len(large.encode())==32768
 large_receipt=c.paste(large);eventually(lambda:ui()['title']==large)
 successes=0
 for _ in range(65):
  result=clip()
  successes+=result==(0,large)
 assert 0<successes<=63,successes
 assert clip()!=(0,large)
 report['tests']['maximum_payload_and_read_cap']={'bytes':32768,'successful_probe_reads':successes,'receipt':large_receipt}
 # Slow receivers keep their pipe unread. The compositor must still answer
 # heartbeats and pause, which closes all its pending writers.
 reset();c.paste(large);eventually(lambda:ui()['title']==large)
 host=c.microhost_pid
 def pipe_count():
  # Wayland event sources duplicate their watched fd; count distinct pipes.
  targets=set()
  for fd in Path(f'/proc/{host}/fd').iterdir():
   try:target=os.readlink(fd)
   except FileNotFoundError:continue  # normal cleanup may close it mid-scan
   if target.startswith('pipe:'):targets.add(target)
  return len(targets)
 before_fds=len(list(Path(f'/proc/{host}/fd').iterdir()));before_pipes=pipe_count()
 readers.append(subprocess.Popen([str(receiver),str(out/'slow-reader.json')],env=private,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE))
 eventually(lambda:(out/'slow-reader.json').exists() and json.loads((out/'slow-reader.json').read_text())['ready'])
 time.sleep(.05)
 latencies=[]
 for _ in range(30):
  began=time.perf_counter();c.rpc('session.heartbeat');latencies.append((time.perf_counter()-began)*1000)
 during_fds=len(list(Path(f'/proc/{host}/fd').iterdir()))
 assert max(latencies)<100,latencies
 during_pipes=pipe_count()
 assert during_pipes-before_pipes==16,(before_pipes,during_pipes)
 began=time.perf_counter();c.rpc('seat.pause');pause_ms=(time.perf_counter()-began)*1000
 assert pause_ms<100,pause_ms
 eventually(lambda:pipe_count()<=before_pipes)
 eventually(lambda:json.loads((out/'slow-reader.json').read_text())['cleared'])
 for reader in readers:
  reader.terminate();reader.wait(timeout=2)
 readers.clear()
 eventually(lambda:len(list(Path(f'/proc/{host}/fd').iterdir()))<=before_fds)
 c.lease=None;c.rpc('seat.resume')
 report['tests']['slow_receivers_bounded_and_interruptible']={'fd_before':before_fds,'fd_during':during_fds,'pipes_before':before_pipes,'pipes_during':during_pipes,'heartbeat_p95_ms':sorted(latencies)[28],'pause_ms':pause_ms}
 # Recipients that close their output cannot terminate the compositor.
 reset();c.paste(large);eventually(lambda:ui()['title']==large)
 reader=subprocess.Popen([str(receiver),str(out/'closed-reader.json'),'closed'],env=private,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 readers.append(reader)
 eventually(lambda:(out/'closed-reader.json').exists() and json.loads((out/'closed-reader.json').read_text())['ready'])
 reader.terminate();reader.wait(timeout=2);readers.clear()
 assert c.rpc('session.heartbeat')['daemon_pid']==c.daemon_pid
 assert Path(f'/proc/{host}').exists()
 report['tests']['closed_receivers_do_not_kill_compositor']='PASS'
 # Absolute source lifetime is separate from the continually renewed lease.
 reset();c.paste('absolute lifetime');eventually(lambda:ui()['title']=='absolute lifetime')
 started=time.monotonic();renew_at=started+2
 while clip(True)[0]==0:
  now=time.monotonic()
  assert now-started<12,'source outlived its absolute bound'
  if now>=renew_at:c.renew();renew_at=now+2
  time.sleep(.05)
 elapsed=time.monotonic()-started
 assert 9<elapsed<12,elapsed
 assert c.rpc('control.status')['lease_id']==c.lease['lease_id']
 report['tests']['absolute_lifetime_with_live_lease']={'seconds':elapsed}
 # A focused button does not consume Ctrl+V. A timeout must remain unknown,
 # not claim insertion or silently repeat the shortcut.
 reset();button=c.node(c.find('Count',role='button'));b=button['bounds']
 root=next(w for w in c.rpc('window.list') if w['primary'])
 c.rpc('pointer.move_absolute',{'x':(b['x']+b['width']/2)/root['width'],'y':(b['y']+b['height']/2)/root['height'],'geometry_version':b['geometry_version']})
 c.rpc('pointer.click',{'button':272,'geometry_version':b['geometry_version']})
 assert 'focused' in c.node(c.find('Count',role='button'))['states']
 pending=c.rpc('keyboard.paste',{'text':'no receiver'})
 result=c.await_operation(pending)
 assert result['execution']=='outcome_unknown' and result['clipboard_transfers']==0,result
 assert ui()['title']==''
 report['tests']['no_receiver_is_unknown']={'receipt':result,'actual':ui()['title']}
 # Pause before the offer acknowledgement: no Ctrl+V may be submitted later.
 reset();os.kill(host,signal.SIGSTOP);frozen.add(host)
 pending=c.rpc('keyboard.paste',{'text':'must-not-arrive'})
 c.rpc('seat.pause')
 os.kill(host,signal.SIGCONT);frozen.remove(host)
 result=c.rpc('operation.get',{'operation_id':pending['operation_id']})
 assert result['execution']=='outcome_unknown' and not result['paste_keys_submitted'],result
 eventually(lambda:clip(True)[0]!=0)
 time.sleep(.1);assert ui()['title']==''
 report['tests']['pause_before_offer_ack']={'receipt':result,'actual':ui()['title']}
 c.lease=None
 report['status']='PASS' 
except BaseException as error:
 report['status']='FAIL';report['error']=repr(error);raise
finally:
 for pid in frozen:
  try:os.kill(pid,signal.SIGCONT)
  except ProcessLookupError:pass
 for reader in readers:
  reader.terminate()
  try:reader.wait(timeout=2)
  except subprocess.TimeoutExpired:reader.terminate();reader.wait(timeout=2)
 if owner and owner.poll() is None:owner.terminate();owner.wait(timeout=2)
 if c:
  try:c.release()
  except Exception as error:report['release_error']=repr(error)
 try:report['stop']=cli('stop')
 except Exception as error:report['stop_error']=repr(error)
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({'status':report['status'],'tests':list(report['tests']),'report':str(out/'report.json')}))
