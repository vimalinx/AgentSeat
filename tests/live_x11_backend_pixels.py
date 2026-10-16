#!/usr/bin/env python3
"""Check backend pointer coordinates at 1.6x using two private compositors."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
from agentseat_client import Session
p=argparse.ArgumentParser();p.add_argument('--report-dir',type=Path,required=True);p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat');a=p.parse_args()
out=a.report_dir.resolve();out.mkdir(parents=True,exist_ok=True)
instance=f'backend-pixels-{os.getpid()}';c=None;report={'status':'RUNNING','clicks':[]}
def cli(*args):return json.loads(subprocess.check_output([str(a.cli.resolve()),'--instance',instance,*args],timeout=30))
def wait_for(fn):
    end=time.monotonic()+10
    while time.monotonic()<end:
        result=fn()
        if result:return result
        time.sleep(.05)
    raise AssertionError('nested backend input oracle timed out')
try:
    report['launch']=cli('run','--host-workspace','119','--','/usr/bin/python3',str(ROOT/'tests/nested_control_fixture.py'),'--fixture','pixels','--out',str(out),'--bin-dir',str(a.cli.resolve().parent))
    wait_for(lambda:(out/'nested.json').exists() and (out/'ui.json').exists())
    c=Session.from_instance(instance,controller='backend-pixels-parent')
    app=json.loads((out/'ui.json').read_text());report['application']=app
    for index,point in enumerate([(.15,.2),(.5,.5),(.85,.8)],1):
        frame=cli('observe',str(out/f'before-{index}.png'))['capture']
        receipt=c.activate(point=point,frame=frame)
        actual=wait_for(lambda:(s if (s:=json.loads((out/'ui.json').read_text()))['clicks']==index else None))
        assert abs(actual['x']-point[0]*app['width'])<=3,actual
        assert abs(actual['y']-point[1]*app['height'])<=3,actual
        report['clicks'].append({'point':point,'actual':actual,'receipt':receipt})
    report['status']='PASS'
finally:
    if c:c.release()
    report['cleanup']=cli('stop');(out/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
