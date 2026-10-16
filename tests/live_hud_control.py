#!/usr/bin/env python3
"""Real HUD controls and click-only takeover through a private parent seat."""
import argparse
import json
import os
from pathlib import Path
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
instance=f'hud-control-{os.getpid()}';parent=None;inner=None
report={'status':'RUNNING','input_source':'private parent seat; no host input','tests':{}}
def cli(*args):return json.loads(subprocess.check_output([str(a.cli.resolve()),'--instance',instance,*args],timeout=30))
def eventually(fn):
    end=time.monotonic()+8
    while time.monotonic()<end:
        value=fn()
        if value:return value
        time.sleep(.04)
    raise AssertionError('HUD or application postcondition timed out')
def rpc(method,params=None):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(3);s.connect(inner['socket'])
        pid,uid,_=struct.unpack('3i',s.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        assert pid==inner['daemon_pid'] and uid==os.getuid()
        s.sendall((json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':{
            'session_id':inner['session_id'],'controller_id':'hud-test','request_id':time.monotonic_ns(),**(params or {})}})+'\n').encode())
        data=b''
        while b'\n' not in data:
            chunk=s.recv(16384)
            if not chunk or len(data)>1048576:raise RuntimeError('invalid daemon response')
            data+=chunk
        result=json.loads(data.split(b'\n')[0])
        if 'error' in result:return {'rpc_error':result['error']}
        return result['result']
def wait_operation(receipt):
    assert 'rpc_error' not in receipt,receipt
    if 'operation_id' in receipt:
        operation=eventually(lambda:(op if (op:=rpc('operation.get',{'operation_id':receipt['operation_id']}))['execution'] in {'finished','failed','cancelled'} else None))
        assert operation['execution']=='finished',operation
    return receipt
def observe(name):
    return cli('observe',str(out/(name+'.png')))['capture']
def app():return json.loads((out/'ui.json').read_text())
def native_action(method,**params):
    return wait_operation(rpc(method,{'lease_id':lease['lease_id'],'control_epoch':lease['control_epoch'],**params}))
try:
    report['launch']=cli('run','--host-workspace','119','--','/usr/bin/python3',str(ROOT/'tests/nested_control_fixture.py'),'--out',str(out),'--fixture','pixels','--hud','--bin-dir',str(a.cli.resolve().parent))
    cli('hud','stop')  # Only the inner HUD should receive the fixture clicks.
    eventually(lambda:(out/'nested.json').exists() and (out/'ui.json').exists())
    inner=json.loads((out/'nested.json').read_text());parent=Session.from_instance(instance,controller='hud-parent')
    time.sleep(.5);frame=observe('ready');application=app();w,h=application['width'],application['height']
    lease=rpc('control.acquire',{'ttl_ms':10000});native_action('pointer.move_absolute',x=.25,y=.3)
    before=rpc('session.heartbeat')
    parent.rpc('pointer.move_absolute',{'x':.85,'y':.8,'geometry_version':frame['geometry_version']})
    after=rpc('session.heartbeat')
    assert after['control_epoch']==before['control_epoch']
    assert not rpc('collaboration.status')['human_active']
    native_action('pointer.click',button=272)
    first=eventually(lambda:(s if (s:=app())['clicks']==1 else None))
    assert abs(first['x']-.25*w)<3 and abs(first['y']-.3*h)<3,first
    report['tests']['hover_keeps_agent_position']={'before':before,'after':after,'actual':first}
    parent.rpc('pointer.click',{'button':272,'geometry_version':frame['geometry_version']})
    second=eventually(lambda:(s if (s:=app())['clicks']==2 else None))
    assert abs(second['x']-.85*w)<3 and abs(second['y']-.8*h)<3,second
    assert rpc('session.heartbeat')['control_epoch']>before['control_epoch']
    report['tests']['click_restores_human_position']=second
    eventually(lambda:not rpc('collaboration.status')['human_active'])
    # The known fixture HUD uses 1.6x logical sizing inside a physical X11 grid.
    button=((w-16-(46*1.6+8)-72)/w,(h-16-4*1.6-30)/h)
    parent.activate(point=button,frame=observe('before-stop'))
    paused=eventually(lambda:(s if (s:=rpc('collaboration.status'))['seat_paused'] else None))
    observe('stopped');report['app_after_stop']=app();blocked=rpc('control.acquire')
    assert 'rpc_error' in blocked,blocked
    report['tests']['stop_blocks_input']={'state':paused,'acquire':blocked}
    parent.activate(point=button,frame=observe('before-continue'))
    resumed=eventually(lambda:(s if not (s:=rpc('collaboration.status'))['seat_paused'] else None))
    observe('continued');report['app_after_continue']=app();eventually(lambda:not rpc('collaboration.status')['human_active'])
    lease=rpc('control.acquire',{'ttl_ms':10000})
    report['app_before_focus']=app()
    native_action('window.focus',window_id='agentseat:root')
    report['app_after_focus']=app()
    native_action('pointer.move_absolute',x=.4,y=.4)
    motion=eventually(lambda:(s if abs((s:=app())['motion_x']-.4*w)<3 and abs(s['motion_y']-.4*h)<3 else None))
    report['tests']['resume_pointer_motion']=motion
    native_action('pointer.click',button=272)
    third=eventually(lambda:(s if (s:=app())['clicks']==3 else None))
    report['third_debug']=third
    assert abs(third['x']-.4*w)<3 and abs(third['y']-.4*h)<3,third
    report['tests']['continue_allows_real_input']={'state':resumed,'application':third}
    parent.activate(point=button,frame=observe('before-final-stop'))
    eventually(lambda:rpc('collaboration.status')['seat_paused'])
    head=((w-16-23*1.6)/w,(h-16-23*1.6)/h)
    parent.activate(point=head,frame=observe('before-expand'))
    time.sleep(.4);observe('expanded')
    report['status']='PASS'
except BaseException as error:
    report['status']='FAIL';report['error']=repr(error);raise
finally:
    if parent:
        try:parent.release()
        except Exception as error:report['release_error']=repr(error)
    try:report['cleanup']=cli('stop')
    finally:(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({'status':report['status'],'report':str(out/'report.json')}))
