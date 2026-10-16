#!/usr/bin/env python3
"""Persistent HUD drag/restart checks using only an isolated parent seat."""
import argparse,json,os,subprocess,sys,time
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--report-dir',type=Path,required=True);p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat');a=p.parse_args()
out=a.report_dir.resolve();out.mkdir(parents=True,exist_ok=False)
sdk=a.cli.resolve().parents[1]/'share/python';sys.path.insert(0,str(sdk if sdk.is_dir() else ROOT/'python'))
from agentseat_client import Session
instance=f'hud-position-{os.getpid()}';report={'status':'RUNNING','tests':{}}
def cli(*args):
    for attempt in range(3):
        result=subprocess.run([str(a.cli.resolve()),'--instance',instance,*args],capture_output=True,text=True,timeout=40)
        if result.returncode==0:return json.loads(result.stdout)
        # Offscreen swapchains may temporarily have no released buffer. Retry
        # only this read-only observation, never an input or lifecycle action.
        if args[0]!='observe' or 'CAPTURE_FAILED: Compositor rejected frame copy' not in result.stderr or attempt==2:
            raise RuntimeError(result.stderr)
        report.setdefault('capture_rejections',[]).append({'path':args[1],'error':result.stderr.strip()})
        time.sleep(.25*(attempt+1))
def wait(fn):
    end=time.monotonic()+12
    while time.monotonic()<end:
        value=fn()
        if value:return value
        time.sleep(.1)
    raise AssertionError('HUD position postcondition timed out')
def launch(name,config,scale="1.6"):
    global instance
    instance=f'hud-position-{os.getpid()}-{name}'
    folder=out/name
    cli('run','--host-workspace','126','--','/usr/bin/python3',str(ROOT/'tests/nested_control_fixture.py'),'--out',str(folder),'--fixture','pixels','--scale',scale,'--hud','--hud-position-file',str(config),'--bin-dir',str(a.cli.resolve().parent))
    cli('hud','stop');wait(lambda:(folder/'nested.json').exists() and (folder/'ui.json').exists());time.sleep(.7)
    state=json.loads((folder/'ui.json').read_text());return folder,Session.from_instance(instance),state['width'],state['height']
def frame(folder,name):return cli('observe',str(folder/(name+'.png')))['capture']
def dark_at(folder,name,x,y,w,h):
    frame(folder,name);im=Image.open(folder/(name+'.png')).convert('RGB');px=im.getpixel((round(x/w*im.width),round((y+14)/h*im.height)))
    assert max(px)<70,(name,px,x,y)
    return px
try:
    config=out/'program-a.json';other=out/'program-b.json'
    folder,parent,w,h=launch('a-first',config);x=w-16-23*1.6;y=h-16-23*1.6
    dark_at(folder,'before',x,y,w,h);g=frame(folder,'drag-start')['geometry_version']
    def send(method,**params):
        current=cli('observe',str(folder/'current.png'))['capture']['geometry_version']
        r=parent.rpc(method,{**params,'geometry_version':current})
        if 'operation_id' in r:assert parent.await_operation(r)['execution']=='finished'
    send('pointer.move_absolute',x=x/w,y=y/h);send('pointer.button',button=272,pressed=True,hold_ms=5000)
    for dx,dy in [(12,8),(100,70),(250,180)]:
        send('pointer.move_absolute',x=(x-dx)/w,y=(y-dy)/h);time.sleep(.12)
    send('pointer.button',button=272,pressed=False);wait(config.exists)
    saved=json.loads(config.read_text());assert 0<saved['x']<1 and 0<saved['y']<1,saved
    dark_at(folder,'dragged',x-250,y-180,w,h);parent.release();cli('stop')
    report['tests']['drag_saved']={'position':saved,'mode':oct(config.stat().st_mode&0o777)}
    folder,parent,w,h=launch('a-reopen',config);dark_at(folder,'restored',x-250,y-180,w,h);cli('stop')
    report['tests']['restart_restored']=True
    folder,parent,w,h=launch('b-first',other);dark_at(folder,'independent',w-16-23*1.6,h-16-23*1.6,w,h)
    assert not other.exists();assert json.loads(config.read_text())==saved;cli('stop')
    report['tests']['programs_independent']=True
    folder,parent,w,h=launch('a-scale-one',config,'1')
    rx=8+round(saved['x']*max(0,w-320-16))+320-23
    ry=8+round(saved['y']*max(0,h-232-16))+232-23
    dark_at(folder,'scaled-restore',rx,ry,w,h);cli('stop')
    report['tests']['scale_restores_relative_position']={'width':w,'height':h,'head':[rx,ry]}
    config.write_text('{"version":1,"x":9,"y":-2}')
    folder,parent,w,h=launch('a-invalid',config);dark_at(folder,'fallback',w-16-23*1.6,h-16-23*1.6,w,h)
    report['tests']['invalid_position_falls_back']=True;report['status']='PASS'
finally:
    report['cleanup']=cli('stop');(out/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report['tests']))
