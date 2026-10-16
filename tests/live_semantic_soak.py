#!/usr/bin/env python3
"""Bounded concurrent GTK/Qt task soak, reconnects and owned-process resources."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat')
p.add_argument('--report-dir',type=Path,required=True);p.add_argument('--seconds',type=float,default=180)
a=p.parse_args()
if not 30<=a.seconds<=600:p.error('--seconds must be 30..600')
out=a.report_dir.resolve();out.mkdir(parents=True,exist_ok=False)
sdk=a.cli.resolve().parents[1]/'share/python';sys.path.insert(0,str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session,AgentSeatError
env={**os.environ,'WLR_RENDERER':'pixman','GDK_GL':'disable','GSK_RENDERER':'cairo',
 'LIBGL_ALWAYS_SOFTWARE':'1','QT_QUICK_BACKEND':'software'}
report={'status':'RUNNING','sessions':{},'cycles':{},'resources':[]};instances={};stop=threading.Event()
HZ=os.sysconf('SC_CLK_TCK');PAGE=os.sysconf('SC_PAGE_SIZE')
def cli(instance,*argv):
 r=subprocess.run([str(a.cli),'--instance',instance,*argv],cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
 if r.returncode:raise RuntimeError(r.stderr)
 return json.loads(r.stdout)
def ui(kind):return json.loads((out/kind/'ui.json').read_text())
def eventually(fn):
 end=time.monotonic()+5
 while time.monotonic()<end:
  try:r=fn()
  except AgentSeatError as error:
   if error.kind!='STALE_NODE_REF':raise
   r=None
  if r:return r
  time.sleep(.03)
 raise AssertionError('application postcondition did not arrive')
def sample():
 # Follow only descendants of these owned launch receipts. Do not inventory
 # unrelated applications or expose their command lines/environment.
 core={pid for s in report['sessions'].values() for pid in [s['state']['daemon_pid'],s['state']['microhost_pid']]}
 pending=list(core);seen=set();rows=[]
 while pending and len(seen)<256:
  pid=pending.pop()
  if pid in seen:continue
  seen.add(pid)
  try:
   proc=Path('/proc')/str(pid);stat=(proc/'stat').read_text().rsplit(')',1)[1].split()
   fds=len(list((proc/'fd').iterdir()))
   rows.append({'pid':pid,'start':stat[19],'core':pid in core,'cpu_s':(int(stat[11])+int(stat[12]))/HZ,
    'rss_bytes':int(stat[21])*PAGE,'fds':fds})
   for task in list((proc/'task').iterdir())[:128]:
    try:pending.extend(int(v) for v in (task/'children').read_text().split())
    except FileNotFoundError:pass
  except (FileNotFoundError,ProcessLookupError):pass
 return {'time':time.monotonic(),'rows':rows,'total_rss_bytes':sum(r['rss_bytes'] for r in rows),
  'core_rss_bytes':sum(r['rss_bytes'] for r in rows if r['core']),
  'core_fds':sum(r['fds'] for r in rows if r['core'])}
def monitor():
 while not stop.wait(1):report['resources'].append(sample())
def cycle(kind,number):
 start=time.monotonic();c=Session.from_instance(instances[kind],controller='soak-'+kind)
 assert c.session_id==report['sessions'][kind]['state']['session_id']
 marker=f'{kind} cycle {number:03d} 中文 🧭';receipts=[]
 try:
  title=c.find('Document title',interface='EditableText')
  receipts.append(c.set_text(title,marker,expect={'kind':'text','node_ref':title,'equals':marker}))
  receipts.append(c.activate(c.find('Count',role='button')))
  eventually(lambda:ui(kind)['title']==marker and ui(kind)['count']==number)
  if number%10==0:
   receipts.append(c.activate(c.find('Save as',role='button')))
   eventually(lambda:(q['total']==1 and not q['truncated']) if (q:=c.query('File name',interface='EditableText')) else False)
   filename=c.find('File name',interface='EditableText');name=f'cycle-{number:03d}.txt'
   receipts.append(c.set_text(filename,name,expect={'kind':'text','node_ref':filename,'equals':name}))
   receipts.append(c.activate(c.find('Save document',role='button')))
   target=out/kind/'documents'/name
   eventually(lambda:target.is_file() and target.read_text()==marker)
  return {'number':number,'elapsed_ms':(time.monotonic()-start)*1000,'marker':marker,
   'receipts':receipts,'actual':ui(kind)}
 finally:c.release()
def idle_cpu(first,last):
 before={(r['pid'],r['start']):r['cpu_s'] for r in first['rows'] if r['core']}
 elapsed=last['time']-first['time']
 return sum(max(0,r['cpu_s']-before.get((r['pid'],r['start']),r['cpu_s'])) for r in last['rows'] if r['core'])/elapsed
monitor_thread=None
try:
 for index,kind in enumerate(['gtk','qt']):
  folder=out/kind;folder.mkdir();instances[kind]=f'semantic-soak-{kind}-{os.getpid()}'
  fixture=ROOT/'tests'/('semantic_fixture.py' if kind=='gtk' else 'semantic_fixture_qt.py')
  report['sessions'][kind]=cli(instances[kind],'run','--a11y','--host-workspace',str(117+index),
   '--','/usr/bin/python3',str(fixture),'--state',str(folder/'ui.json'),'--output-dir',str(folder/'documents'))
  report['cycles'][kind]=[]
  assert report['sessions'][kind]['state']['host_launch_receipt']['relation']!='outer-focused'
  cli(instances[kind],'observe',str(folder/'before.png'))
 # Let toolkit initialization settle, then measure actual idle core CPU.
 time.sleep(2);idle_before=sample();monitor_thread=threading.Thread(target=monitor,daemon=True);monitor_thread.start()
 time.sleep(15);idle_after=sample();report['idle_core_cpu_fraction']=idle_cpu(idle_before,idle_after)
 began=time.monotonic();warm=None;number=0
 with ThreadPoolExecutor(max_workers=2) as pool:
  while time.monotonic()-began<a.seconds:
   number+=1;results=list(pool.map(lambda kind:cycle(kind,number),['gtk','qt']))
   for kind,result in zip(['gtk','qt'],results):report['cycles'][kind].append(result)
   if number==5:warm=sample()
   if number%10==0:print(json.dumps({'cycles_per_session':number,'elapsed_s':round(time.monotonic()-began,1)}),flush=True)
   time.sleep(.75)
 report['active_seconds']=time.monotonic()-began
 for kind in instances:
  assert ui(kind)['count']==number
  report['sessions'][kind]['result_capture']=cli(instances[kind],'observe',str(out/kind/'result.png'))
  report['sessions'][kind]['final_ui']=ui(kind)
  times=[r['elapsed_ms'] for r in report['cycles'][kind]]
  report['sessions'][kind]['latency_ms']={'median':statistics.median(times),'p95':sorted(times)[int((len(times)-1)*.95)],'max':max(times)}
 end_idle_start=sample();time.sleep(15);end_idle=sample()
 report['final_idle_core_cpu_fraction']=idle_cpu(end_idle_start,end_idle)
 report['resource_accounting']='sum of owned-process RSS; shared pages may be counted more than once; core=daemon+microhost'
 report['resource_growth']={'core_rss_bytes':end_idle['core_rss_bytes']-warm['core_rss_bytes'],
  'core_fds':end_idle['core_fds']-warm['core_fds'],'warm':warm,'end':end_idle}
 assert report['resource_growth']['core_rss_bytes']<64*1024*1024
 assert report['resource_growth']['core_fds']<=16
 assert max(r['total_rss_bytes'] for r in report['resources'])<2*1024**3
 report['status']='PASS'
except BaseException as error:
 report['status']='FAIL';report['error']=repr(error)
 if isinstance(error,AgentSeatError):report['error_receipt']=error.receipt
 raise
finally:
 stop.set()
 if monitor_thread:monitor_thread.join(timeout=3)
 for kind,instance in instances.items():
  try:report.setdefault('stop',{})[kind]=cli(instance,'stop')
  except Exception as error:report.setdefault('stop_errors',{})[kind]=repr(error)
 (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
