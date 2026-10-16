#!/usr/bin/env python3
"""Live private D-Bus fault injection with real daemon authority and GTK effects."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--report-dir',type=Path,required=True)
p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat');args=p.parse_args()
sdk=args.cli.resolve().parents[1]/'share/python'
sys.path.insert(0,str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session,AgentSeatError
out=args.report_dir.resolve();out.mkdir(parents=True,exist_ok=True)
instance=f'semantic-faults-{os.getpid()}';report={'status':'RUNNING','tests':{}}
state=None;client=None;stopped=set()
env={**os.environ,'WLR_RENDERER':'pixman','GSK_RENDERER':'cairo'}

def eventually(fn,seconds=4):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  value=fn()
  if value:return value
  time.sleep(.01)
 raise AssertionError('condition timed out')

def cli(*argv):
 r=subprocess.run([str(args.cli),'--instance',instance,*argv],env=env,cwd=ROOT,capture_output=True,text=True,timeout=25)
 if r.returncode:raise RuntimeError(r.stderr)
 return json.loads(r.stdout)

def fault():return json.loads((out/'fault.json').read_text())
def ui():return json.loads((out/'ui.json').read_text())
def raw(method,params=None,owner=None):
 payload={'session_id':client.session_id,'controller_id':owner or client.controller,'request_id':time.monotonic_ns(),**(params or {})}
 return client._exchange(client.runtime/'control.sock',{'jsonrpc':'2.0','id':1,'method':method,'params':payload},client.daemon_pid)
def ok(method,params=None):
 response=raw(method,params);assert 'result' in response,response
 return response['result']
def context():return {'lease_id':client.lease['lease_id'],'control_epoch':client.lease['control_epoch']}
def operation(receipt):return ok('operation.get',{'operation_id':receipt['operation_id']})
def freeze(pid):os.kill(pid,signal.SIGSTOP);stopped.add(pid)
def thaw(pid):os.kill(pid,signal.SIGCONT);stopped.discard(pid)
def resume():
 client.lease=None;ok('seat.resume');client.acquire()
def expected(fn,kind):
 try:fn()
 except AgentSeatError as error:
  assert error.kind==kind,(kind,error.kind,error.receipt)
  return error.receipt
 raise AssertionError('expected '+kind)

try:
 report['launch']=cli('run','--a11y','--host-workspace','109','--','/usr/bin/python3',str(ROOT/'tests/semantic_fixture.py'),'--state',str(out/'ui.json'),'--output-dir',str(out/'documents'),'--fault-state',str(out/'fault.json'))
 state=report['launch']['state'];client=Session.from_instance(instance,controller='fault-acceptance')
 eventually(lambda:(out/'fault.json').exists())
 snapshot=client.snapshot(limit=128);report['snapshot']=snapshot
 delayed=client.find('Delayed action',role='button');count=client.find('Count',role='button')
 client.acquire()
 # Replayed requests return the original receipt; they must not call the app again.
 request={**context(),'node_ref':count,'action':0,'request_id':time.monotonic_ns()}
 receipt=ok('a11y.perform',request);eventually(lambda:operation(receipt)['execution']!='running')
 assert ok('a11y.perform',request)==receipt
 assert raw('a11y.perform',{**request,'action':1})['error']['data']['kind']=='REQUEST_ID_CONFLICT'
 assert ok('request.get',{'request_id':request['request_id']})['result']==receipt
 eventually(lambda:ui()['count']==1)
 assert raw('a11y.perform',{**context(),'node_ref':count,'action':0},'other')['error']['data']['kind']=='NO_WRITE_LEASE'
 report['tests']['dedup_and_other_writer']={'actual_count':ui()['count'],'receipt':operation(receipt)}
 expected(lambda:client.read('a11y.perform',node_ref=count,action=0),'NATIVE_CONTROL_REQUIRED')
 report['tests']['bridge_has_no_write_lane']='PASS'
 # Pausing while the bridge is frozen must finish promptly with zero submissions.
 bridge=client.read('a11y.status')['bridge_pid'];freeze(bridge)
 receipt=ok('a11y.perform',{**context(),'node_ref':count,'action':0})
 began=time.perf_counter();ok('seat.pause');pause_ms=(time.perf_counter()-began)*1000
 assert pause_ms<100,pause_ms
 result=eventually(lambda:(r if (r:=operation(receipt))['execution']!='running' else None))
 assert result['submitted']==0 and result['execution']=='cancelled',result
 thaw(bridge);time.sleep(.2);assert ui()['count']==1
 report['tests']['pause_before_submission']={'pause_ms':pause_ms,'receipt':result,'actual_count':1}
 resume()
 # An accepted call times out before the app's deliberately delayed side effect.
 delayed=client.find('Delayed action',role='button')
 receipt=ok('a11y.perform',{**context(),'node_ref':delayed,'action':0,'timeout_ms':100})
 result=eventually(lambda:(r if (r:=operation(receipt))['execution']!='running' else None))
 assert result['execution']=='outcome_unknown' and result['submitted']==1,result
 eventually(lambda:fault()['completed']==1);time.sleep(.2);assert fault()['received']==1
 report['tests']['timeout_not_replayed']={'receipt':result,'application':fault()}
 # Pause after app receipt cannot undo a queued call; late success stays unknown.
 receipt=ok('a11y.perform',{**context(),'node_ref':delayed,'action':0,'timeout_ms':3000})
 eventually(lambda:fault()['received']==2)
 began=time.perf_counter();ok('seat.pause');pause_ms=(time.perf_counter()-began)*1000
 assert pause_ms<100,pause_ms
 result=operation(receipt);assert result['execution']=='outcome_unknown',result
 eventually(lambda:fault()['completed']==2)
 result=eventually(lambda:(r if (r:=operation(receipt))['application_replied'] else None))
 assert result['execution']=='outcome_unknown' and result['method_succeeded'] is True and result['cancel_requested'],result
 report['tests']['pause_after_submission']={'pause_ms':pause_ms,'late_receipt':result,'application':fault()}
 resume()
 # Private peer event floods expose sequence gaps without retaining text payloads.
 cursor=client.subscribe();flood=client.find('Flood events',role='button');client.perform(flood,0)
 eventually(lambda:client.read('a11y.status')['event_seq']>=cursor.sequence+600)
 events,_=client.events(cursor,limit=128)
 assert events['gap'] and events['code']=='EVENT_GAP' and len(events['events'])<=128,events
 assert 'PRIVATE_EVENT_SENTINEL' not in json.dumps(events) and all(not e['payload_read'] for e in events['events'])
 report['tests']['event_gap_and_redaction']=events
 # Path reuse invalidates old references even though the same path is exported again.
 old=client.find('Delayed action',role='button');replace=client.find('Replace target',role='button');client.perform(replace,0)
 eventually(lambda:fault()['replacements']==1)
 expected(lambda:client.node(old),'STALE_NODE_REF')
 new=client.find('Delayed action',role='button');assert old!=new
 report['tests']['path_reuse_invalidates_ref']={'old':old,'new':new}
 # A wait started before an action is woken and rechecks an actual new value.
 pending=client.read('wait.start',condition={'kind':'exists','selector':{'name':'Count 2','exact':True}},timeout_ms=3000)
 assert pending['status']=='pending'
 client.perform(client.find('Count',role='button'),0)
 result=eventually(lambda:(r if (r:=client.read('wait.get',wait_id=pending['wait_id']))['status']!='pending' else None))
 assert result['status']=='satisfied' and result['checks']>=2,result
 report['tests']['wait_no_lost_wakeup']=result
 # Writer expiry cancels a preparation independently of SDK lease renewal.
 count=client.find('Count',role='button');client.release()
 client.lease=ok('control.acquire',{'ttl_ms':150});freeze(bridge)
 receipt=ok('a11y.perform',{**context(),'node_ref':count,'action':0})
 result=eventually(lambda:(r if (r:=operation(receipt))['execution']!='running' else None))
 assert result['execution']=='cancelled' and result['submitted']==0,result
 thaw(bridge);time.sleep(.15);assert ui()['count']==2
 report['tests']['writer_expiry_before_submission']=result
 client.lease=None;client.acquire()
 # Repeated semantic actions reuse the daemon and do not leak per-call descriptors.
 def process_stats(pid):
  fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
  status=dict(line.split(':',1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
  return {'ticks':int(fields[11])+int(fields[12]),'rss_kib':int(status['VmRSS'].split()[0]),'fds':len(list(Path(f'/proc/{pid}/fd').iterdir()))}
 slider=client.find("Level",interface="Value")
 before=process_stats(client.daemon_pid)
 durations=[]
 for index in range(20):
  began=time.perf_counter();client.set_value(slider,index);durations.append((time.perf_counter()-began)*1000)
  eventually(lambda:ui()["level"]==index)
 assert ui()["count"]==2
 time.sleep(.1);after=process_stats(client.daemon_pid)
 assert after['fds']<=before['fds']+2,(before,after)
 latencies=[]
 for _ in range(100):
  began=time.perf_counter();ok('session.heartbeat');latencies.append((time.perf_counter()-began)*1000)
 idle_before={pid:process_stats(pid) for pid in [client.daemon_pid,bridge]}
 time.sleep(1)
 idle_after={pid:process_stats(pid) for pid in idle_before}
 report['tests']['bounded_semantic_resources']={'before':before,'after':after,'actions':20,
  'action_median_ms':sorted(durations)[10],'heartbeat_p95_ms':sorted(latencies)[94],
  'idle_ticks_in_one_second':{str(pid):idle_after[pid]['ticks']-stats['ticks'] for pid,stats in idle_before.items()},
  'clock_ticks_per_second':os.sysconf('SC_CLK_TCK'),'actual_count':ui()['count'],'actual_level':ui()['level']}
 report['capture']=cli('observe',str(out/'after.png'))
 report['status']='PASS'
except BaseException as error:
 report.update(status='FAIL',error=repr(error));raise
finally:
 for pid in list(stopped):
  try:thaw(pid)
  except ProcessLookupError:pass
 if client:
  try:client.release()
  except (AgentSeatError,OSError):pass
 try:report['stop']=cli('stop')
 finally:(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'status':report['status'],'tests':list(report['tests'])}))
