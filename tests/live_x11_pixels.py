#!/usr/bin/env python3
"""Verify X11 drawable pixels survive capture and real clicks reach their targets."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from PIL import Image
ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--report-dir', type=Path, required=True)
p.add_argument('--workspace', default='125')
p.add_argument('--cli', type=Path, default=ROOT/'bin/agentseat')
a=p.parse_args(); out=a.report_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
sdk=a.cli.resolve().parents[1]/'share/python'
sys.path.insert(0,str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session
instance=f'x11-pixels-{os.getpid()}'; c=None
report={'status':'RUNNING','clicks':[]}
def cli(*args):
    return json.loads(subprocess.check_output([str(a.cli.resolve()),'--instance',instance,*args],cwd=ROOT,timeout=30))
def wait_for(fn):
    end=time.monotonic()+8
    while time.monotonic()<end:
        result=fn()
        if result:return result
        time.sleep(.05)
    raise AssertionError('application oracle timed out')
try:
    binary=out/'fixture'; state=out/'fixture.json'
    subprocess.run(['cc','-Wall','-Wextra',str(ROOT/'tests/x11_pixels_fixture.c'),'-lX11','-o',str(binary)],check=True)
    report['launch']=cli('run','--x11','--host-workspace',a.workspace,'--',str(binary),str(state))
    wait_for(state.exists); time.sleep(.5)
    c=Session.from_instance(instance,controller='pixel-oracle')
    app=json.loads(state.read_text()); output=c.rpc('session.heartbeat')['output']
    report['application']=app; report['output']=output
    assert app['width']==app['image_width']==output['pixel_width']==output['width']
    assert app['height']==app['image_height']==output['pixel_height']==output['height']
    capture=cli('observe',str(out/'stripes.png')); report['capture']=capture
    im=Image.open(out/'stripes.png').convert('RGB')
    assert im.size==(app['width'],app['height'])
    row=[im.getpixel((x,30)) for x in range(40,240)]
    assert all(rgb==((0,0,0) if i%2==0 else (255,255,255)) for i,rgb in enumerate(row)), 'single pixel stripes were resampled'
    report['exact_alternating_pixels']=len(row)
    for index,point in enumerate([(.1,.1),(.5,.5),(.9,.9)],1):
        frame=cli('observe',str(out/f'before-{index}.png'))['capture']
        receipt=c.activate(point=point,frame=frame)
        actual=wait_for(lambda:(s if (s:=json.loads(state.read_text()))['clicks']==index else None))
        assert abs(actual['x']-point[0]*app['width'])<=2,actual
        assert abs(actual['y']-point[1]*app['height'])<=2,actual
        report['clicks'].append({'point':point,'actual':actual,'receipt':receipt})
    report['final']=cli('status'); report['status']='PASS'
finally:
    if c:c.release()
    report['cleanup']=cli('stop')
    (out/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
