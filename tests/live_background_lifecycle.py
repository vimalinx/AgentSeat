#!/usr/bin/env python3
"""Private parent close -> application background -> application remap -> real exit."""
import argparse,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--report-dir',type=Path,required=True);p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat');p.add_argument('--x11',action='store_true');a=p.parse_args()
out=a.report_dir.resolve();out.mkdir(parents=True,exist_ok=False)
instance=f'background-{os.getpid()}';report={'status':'RUNNING','checks':{},'mode':'x11' if a.x11 else 'wayland'}
def cli(*args):return json.loads(subprocess.check_output([str(a.cli.resolve()),'--instance',instance,*args],timeout=35))
def wait(fn):
    end=time.monotonic()+12
    while time.monotonic()<end:
        value=fn()
        if value:return value
        time.sleep(.1)
    raise AssertionError('background lifecycle postcondition timed out')
def state():return json.loads((out/'ui.json').read_text())
def capture(name):return cli('observe',str(out/(name+'.png')))
def desktop_windows():
    return [w for w in cli("windows") if w["title"] != "AgentSeat lifecycle parent guard"]
def host_present():
    value=cli('status');s=value['state']
    clients=json.loads(subprocess.check_output(['hyprctl','--instance',s['host_instance'],'clients','-j']))
    return any(c['pid']==s['microhost_pid'] and c.get('mapped',True) for c in clients)
try:
    report['launch']=cli('run','--ime','simple','--host-workspace','127','--','/usr/bin/python3',str(ROOT/'tests/nested_control_fixture.py'),'--out',str(out),'--fixture','background','--hud','--keep-parent-window','--bin-dir',str(a.cli.resolve().parent),*(['--x11'] if a.x11 else []))
    cli('hud','stop');wait(lambda:(out/'nested.json').exists() and (out/'ui.json').exists());wait(lambda:len(desktop_windows())==1)
    initial=state();capture('initial')
    for number in (1,2):
        windows=desktop_windows();assert len(windows)==1,windows
        before=cli('status')['host_now']
        receipt=cli('window','close',windows[0]['id']);assert receipt['execution']=='finished',receipt
        wait(lambda:state()['closes']==number and not state()['visible'])
        wait(lambda:not desktop_windows());assert host_present()
        assert cli('status')['running'];os.kill(initial['pid'],0)
        tick=state()['ticks'];wait(lambda:state()['ticks']>=tick+4)
        report['checks'][f'close_{number}_background']={'state':state(),'private_parent_window_hidden':True,'resident_pid_unchanged':state()['pid']==initial['pid']}
        (out/'ui.command').write_text('show')
        wait(lambda:state()['visible']);wait(lambda:len(desktop_windows())==1);assert host_present()
        capture(f'restored-{number}');after=cli('status')['host_now']
        assert after['window'] != report['launch']['state']['outer_window'], {'before':before,'after':after}
        assert state()['pid']==initial['pid']
        report['checks'][f'restore_{number}']={'state':state(),'host_focus_outside_fixture':True}
        time.sleep(1.6)
    # Last window can be destroyed while the resident process still has work.
    windows=desktop_windows();cli('window','close',windows[0]['id']);wait(lambda:state()['closes']==3);assert host_present()
    (out/'ui.command').write_text('exit');wait(lambda:(out/'inner-exit.json').exists())
    report['checks']['real_exit']=json.loads((out/'inner-exit.json').read_text());assert report['checks']['real_exit']['exit_code']==0
    worker=out/'ui.worker.json';wait(worker.exists)
    worker_state=json.loads(worker.read_text());os.kill(worker_state['pid'],0)
    wait(lambda:json.loads(worker.read_text())['ticks']>=worker_state['ticks']+4)
    report['checks']['non_gui_work_survives_natural_exit']=json.loads(worker.read_text())
    report['status']='PASS'
except BaseException as error:
    report.update(status='FAIL',error=repr(error));raise
finally:
    worker=out/'ui.worker.json'
    if worker.exists():
        worker_pid=json.loads(worker.read_text())['pid']
        try:
            command=Path(f'/proc/{worker_pid}/cmdline').read_bytes()
            if str(ROOT/'tests/background_worker_fixture.py').encode() in command and str(worker).encode() in command:
                os.kill(worker_pid,15)
        except ProcessLookupError:pass
    report['cleanup']=cli('stop');(out/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'status':report['status'],'checks':list(report['checks'])}))
