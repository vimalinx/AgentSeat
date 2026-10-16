#!/usr/bin/env python3
"""Real scoped accessibility, fresh frames and bounded slow-reader acceptance."""
import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--workspace', type=int, default=109)
    args = parser.parse_args()
    out = args.report_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    instance = f'read-paths-{os.getpid()}'
    report = {'status': 'RUNNING', 'tests': {}}
    foreign = None; frozen = None; state = None
    def cli(*argv, check=True):
        r = subprocess.run([str(ROOT/'bin/agentseat'), '--instance', instance, *argv],
            capture_output=True, text=True, timeout=20)
        if not check: return r
        if r.returncode: raise RuntimeError(r.stderr)
        return json.loads(r.stdout)
    def raw(path, request):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(4); c.connect(str(path)); c.sendall((json.dumps(request)+'\n').encode()); data=b''
            while b'\n' not in data:
                chunk=c.recv(65536)
                if not chunk: raise RuntimeError('missing reply')
                data+=chunk
        return json.loads(data.split(b'\n')[0])
    def control(method, params=None):
        return raw(Path(receipt['socket']).with_name('control.sock'), {'jsonrpc':'2.0','id':1,'method':method,'params':{
            'session_id':state['session_id'],'controller_id':'read-paths','request_id':time.monotonic_ns(),**(params or {})}})
    try:
        state=cli('run','--a11y','--host-workspace',str(args.workspace),'--','/usr/bin/env','GSK_RENDERER=cairo',
            '/usr/bin/python3',str(ROOT/'tests/control_fixture.py'),'--state',str(out/'ui.json'),'--tree-nodes','400')['state']
        receipt=cli('a11y','status'); pid=receipt['app_pid']
        env=dict(os.environ)
        for value in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0'):
            if value.startswith((b'WAYLAND_DISPLAY=',b'DISPLAY=',b'DBUS_SESSION_BUS_ADDRESS=',b'AT_SPI_BUS_ADDRESS=',b'GTK_A11Y=')):
                key,val=value.decode().split('=',1);env[key]=val
        env['GSK_RENDERER']='cairo'
        with (out/'foreign.log').open('w') as log:
            foreign=subprocess.Popen(['/usr/bin/python3',str(ROOT/'tests/control_fixture.py'),'--state',str(out/'foreign-ui.json')],
                env=env,stdout=log,stderr=log)
        end=time.monotonic()+5
        while time.monotonic()<end and not (out/'foreign-ui.json').exists():time.sleep(.02)
        assert foreign.poll() is None
        windows=cli('windows'); focus=[w['id'] for w in windows if w['focused']]
        q=cli('a11y','query','Count click')
        assert q['nodes'] and {n['pid'] for n in q['nodes']}=={pid},q
        assert all(n['pid']!=foreign.pid for n in q['nodes'])
        assert [w['id'] for w in cli('windows') if w['focused']]==focus
        report['tests']['same_name_foreign_peer_excluded_without_focus_write']={'own_pid':pid,'foreign_pid':foreign.pid,'nodes':q['nodes']}
        page=cli('a11y','snapshot','--limit','3')
        nextpage=cli('a11y','snapshot','--snapshot-id',page['snapshot_id'],'--offset','3','--limit','3')
        assert page['next_offset']==3 and len(nextpage['nodes'])==3
        assert page['truncated'] and page['total'] <= 256, page
        report['tests']['large_tree_budget']='PASS'
        node=cli('a11y','get',page['nodes'][0]['node_ref']); assert node['pid']==pid
        bad=cli('a11y','get','old-scope:expired:0',check=False);assert bad.returncode and 'STALE_NODE_REF' in bad.stderr
        forbidden=raw(receipt['socket'],{'scope_id':receipt['scope_id'],'method':'a11y.perform'})
        assert forbidden['error']['kind']=='NATIVE_CONTROL_REQUIRED',forbidden
        mismatch=raw(receipt['socket'],{'scope_id':'old','method':'a11y.snapshot'})
        assert mismatch['error']['kind']=='SCOPE_MISMATCH'
        report['tests']['pagination_refs_and_read_only'] = 'PASS'
        # A stalled bridge client cannot block another complete bridge request.
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as slow:
            slow.connect(receipt['socket']);slow.sendall(b'{"scope_id":')
            began=time.perf_counter();cli('a11y','status');elapsed=time.perf_counter()-began
            assert elapsed < .5,elapsed
            report['tests']['partial_reader_status_ms']=elapsed*1000
        frozen=pid;os.kill(pid,signal.SIGSTOP)
        reader=subprocess.Popen([str(ROOT/'bin/agentseat'),'--instance',instance,'a11y','snapshot'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        time.sleep(.05);began=time.perf_counter(); heartbeat=control('session.heartbeat');elapsed=time.perf_counter()-began
        assert 'result' in heartbeat and elapsed<.1
        output,error=reader.communicate(timeout=4)
        assert reader.returncode==0 and json.loads(output)['truncated'],(output,error)
        os.kill(pid,signal.SIGCONT);frozen=None
        report['tests']['frozen_application_control_heartbeat_ms']=elapsed*1000
        foreign.terminate();foreign.wait(timeout=3);foreign=None
        primary=next(w for w in cli('windows') if w['primary'])
        cli('window','focus',primary['id'])
        before=cli('observe',str(out/'before.png'))
        cli('control', 'release')
        # Explicit frame coordinate preconditions reject a stale scene before input.
        lease = control('control.acquire')['result']
        context = {'lease_id':lease['lease_id'], 'control_epoch':lease['control_epoch']}
        stale = control('pointer.move_absolute', {'x':.2,'y':.2,'geometry_version':2147483647,**context})
        assert stale['error']['data']['kind']=='STALE_GEOMETRY', stale
        current = control('session.heartbeat')['result']['output']['geometry_version']
        moved = control('pointer.move_absolute', {'x':.2,'y':.2,'geometry_version':current,**context})
        assert moved['result']['submitted'], moved
        control('control.release',{'lease_id':lease['lease_id']})
        report['tests']['frame_geometry_precondition']='PASS' 
        cli('type','Fresh frame 123')
        end=time.monotonic()+3
        while time.monotonic()<end:
            if json.loads((out/'ui.json').read_text())['text']=='Fresh frame 123':break
            time.sleep(.01)
        else:raise AssertionError('application did not receive fresh text')
        after=cli('observe',str(out/'after.png'))
        assert before['capture_passes']==after['capture_passes']==1
        assert after['capture']['presented_ns']>before['capture']['presented_ns']
        assert hashlib.sha256((out/'before.png').read_bytes()).digest()!=hashlib.sha256((out/'after.png').read_bytes()).digest()
        began=time.perf_counter()
        frames=[]
        for _ in range(10):
            f=control('capture.submit')['result']
            while f['status'] in {'pending','encoding'}:
                time.sleep(.005);f=control('capture.get',{'frame_id':f['frame_id']})['result']
            assert f['status']=='finished',f
            frames.append(f)
        runtime=Path(receipt['socket']).parent
        assert len(list(runtime.glob('f-*.png')))<=8
        report['tests']['fresh_single_frame_and_retention']={'before':before,'after':after,'ten_frame_seconds':time.perf_counter()-began,'retained':len(list(runtime.glob('f-*.png')))}
        report['status']='PASS'
    except Exception as error:
        report.update(status='FAIL',error=repr(error));raise
    finally:
        if frozen:os.kill(frozen,signal.SIGCONT)
        if foreign and foreign.poll() is None:foreign.terminate();foreign.wait(timeout=3)
        if state:
            report['stop']=cli('stop',check=False).returncode
            report['bridge_gone']=not Path(f"/proc/{receipt['bridge_pid']}").exists()
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'status':report['status'],'tests':list(report['tests'])}))

if __name__=='__main__':main()
