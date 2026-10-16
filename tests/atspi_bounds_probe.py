#!/usr/bin/env python3
"""Read-only coordinate comparison on a verified existing private application bus."""
import argparse
import collections
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from agentseat_client import Session
loader=importlib.machinery.SourceFileLoader('bounds_bridge',str(ROOT/'bin/agentseat-a11y'))
spec=importlib.util.spec_from_loader(loader.name,loader);module=importlib.util.module_from_spec(spec);loader.exec_module(module)
p=argparse.ArgumentParser();p.add_argument('--instance',required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--name',action='append',required=True);a=p.parse_args()
c=Session.from_instance(a.instance);receipt=json.loads((c.runtime/'a11y.json').read_text())
bridge_pid=receipt['bridge_pid'];assert module.process(bridge_pid)[1]==receipt['bridge_start']
# Extract one known private connection locator, never dump process environment.
env=dict(item.split(b'=',1) for item in Path(f'/proc/{bridge_pid}/environ').read_bytes().split(b'\0') if b'=' in item)
session=module.connect(env[b'DBUS_SESSION_BUS_ADDRESS'].decode())
b=module.Bridge(c.runtime,True,'simple');b.deadline=time.monotonic()+8
assert module.process(b.pid(session))[0]==module.process(bridge_pid)[0]
launcher=b.pid(session,'org.a11y.Bus');assert module.descendant(launcher,bridge_pid)
address=b.call(session,'org.a11y.Bus','/org/a11y/bus','org.a11y.Bus','GetAddress')[0]
b.bus=module.connect(address);assert module.descendant(b.pid(b.bus),launcher)
app_pid=receipt['app_pid'];assert module.process(app_pid)[1]==receipt['app_start']

def scoped(owner):
 pid=b.pid(b.bus,owner);assert module.descendant(pid,app_pid);return pid

def rects(owner,path):
 output={}
 for coord,label in [(0,'screen'),(1,'window'),(2,'parent')]:
  try:output[label]=b.call(b.bus,owner,path,module.API+'Component','GetExtents','(u)',(coord,))[0]
  except Exception as error:output[label]={'error':str(error)}
 return output

roots=b.call(b.bus,'org.a11y.atspi.Registry','/org/a11y/atspi/accessible/root',module.API+'Accessible','GetChildren')[0]
queue=collections.deque(roots);seen=set();found={};count=0
while queue and count<2048 and time.monotonic()<b.deadline:
 owner,path=queue.popleft()
 if (owner,path) in seen:continue
 seen.add((owner,path));count+=1
 scoped(owner);props=b.properties(owner,path,module.API+'Accessible')
 if props.get('Name') in a.name:
  ancestors=[];current=(owner,path);frame_ref=None
  for _ in range(16):
   peer,node_path=current;scoped(peer);properties=b.properties(peer,node_path,module.API+'Accessible')
   role=b.call(b.bus,peer,node_path,module.API+'Accessible','GetRoleName')[0]
   ancestors.append({'name':properties.get('Name'),'role':role,'bounds':rects(peer,node_path)})
   if role=='frame' and properties.get('Name'):frame_ref=(peer,node_path)
   parent=properties.get('Parent')
   if not parent or parent[1].endswith('/null') or role=='application':break
   current=parent
  hits={}
  if frame_ref:
   rect=ancestors[0]['bounds']['window'];center=(rect[0]+rect[2]/2,rect[1]+rect[3]/2)
   scale=c.rpc('session.heartbeat')['output']['scale']
   for label,xy in [('raw',center),('divided_by_output_scale',(center[0]/scale,center[1]/scale))]:
    peer,node_path=frame_ref
    hit=b.call(b.bus,peer,node_path,module.API+'Component','GetAccessibleAtPoint','(iiu)',(round(xy[0]),round(xy[1]),1))[0]
    hits[label]={'point':xy,'reference':hit}
    if hit[1].endswith('/null'):continue
    scoped(hit[0]);hits[label]['name']=b.properties(*hit,module.API+'Accessible').get('Name')
  found[props['Name']]={'ancestry':ancestors,'frame_hit_tests':hits}
  if all(name in found for name in a.name):break
 queue.extend(b.call(b.bus,owner,path,module.API+'Accessible','GetChildren')[0])
assert all(name in found for name in a.name),(count,list(found))
a.out.write_text(json.dumps({'status':'READ_ONLY','app_pid':app_pid,'output':c.rpc('session.heartbeat')['output'],'nodes':found},ensure_ascii=False,indent=2))
print(json.dumps(found,ensure_ascii=False))
