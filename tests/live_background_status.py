#!/usr/bin/env python3
"""An invisible real app remains a healthy session; explicit stop ends it."""
import argparse,json,os,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--report-dir',type=Path,required=True);p.add_argument('--cli',type=Path,default=ROOT/'bin/agentseat');a=p.parse_args()
out=a.report_dir.resolve();out.mkdir(parents=True,exist_ok=False);instance=f'background-status-{os.getpid()}';report={'status':'RUNNING'}
def cli(*args):return json.loads(subprocess.check_output([str(a.cli.resolve()),'--instance',instance,*args],timeout=35))
def wait(fn):
    end=time.monotonic()+10
    while time.monotonic()<end:
        value=fn()
        if value:return value
        time.sleep(.1)
    raise AssertionError('background status postcondition timed out')
def ended(pid):
    try:return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0]=='Z'
    except FileNotFoundError:return True
try:
    report['launch']=cli('run','--ime','simple','--host-workspace','128','--','/usr/bin/python3',str(ROOT/'tests/background_fixture.py'),'--state',str(out/'ui.json'))
    wait(lambda:(out/'ui.json').exists());app=json.loads((out/'ui.json').read_text());pid=app['pid']
    windows=cli('windows');assert len(windows)==1
    cli('window','close',windows[0]['id']);wait(lambda:not cli('windows'))
    report['background']=cli('heartbeat');assert report['background']['healthy'] and report['background']['application_state']=='background' and not report['background']['window_ready']
    current=cli('status');clients=json.loads(subprocess.check_output(['hyprctl','--instance',current['state']['host_instance'],'clients','-j']))
    assert not any(c['pid']==current['state']['microhost_pid'] for c in clients)
    assert not ended(pid)
    report['stop']=cli('stop');wait(lambda:ended(pid));report['explicit_stop_ended_resident']=True;report['status']='PASS'
finally:
    report['cleanup']=cli('stop');(out/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'status':report['status']}))
