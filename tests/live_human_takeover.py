#!/usr/bin/env python3
"""Wait for a HUMAN click, then verify HUMAN input cancels a private test stream."""
import argparse
import json
import os
import socket
import struct
import subprocess
import time
from pathlib import Path

def verify_takeover(report):
    """Check receipts and app events; this function never synthesizes input."""
    operation = report['operation']
    assert operation['execution'] == 'cancelled', operation
    assert 0 < operation['submitted'] < 4000, operation
    assert report['heartbeat_after']['control_epoch'] > report['lease']['control_epoch']
    assert report.get('human_held_observed', {}).get('human_held') is True, 'no backend human hold observed'
    assert report['human_held_observed']['control_epoch'] > report['lease']['control_epoch']
    assert report.get('human_released', {}).get('human_held') is False, 'human key not released'
    keys = [event for event in report['fixture_after']['keys']
            if len(event) == 4 and event[3] >= report['stream_started_ns']]
    # The operator holds the ordinary a key; Agent input consists only of 0.
    presses = [i for i, event in enumerate(keys) if event[:2] == ['press', 97]]
    assert presses, 'application did not receive human a key press'
    assert any(event[:2] == ['release', 97] for event in keys[presses[-1] + 1:]), 'application did not receive human a key release'


def main():
    ROOT = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument('--instance', required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--cli', type=Path, default=ROOT / 'bin/agentseat')
    parser.add_argument('--state', type=Path, help='control_fixture.py state file')
    parser.add_argument('--wait-seconds', type=float, default=60)
    args = parser.parse_args()
    if not 1 <= args.wait_seconds <= 3600: parser.error('--wait-seconds must be 1..3600')
    out = args.report_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'human-takeover.json').exists(): parser.error('use a fresh report directory')
    cli = [str(args.cli), '--instance', args.instance]
    status = json.loads(subprocess.check_output([*cli, 'status'], text=True))
    state = status['state']
    receipt = json.loads(subprocess.check_output([*cli, 'a11y', 'status'], text=True))
    control = Path(receipt['socket']).with_name('control.sock')
    ui_path = args.state.resolve() if args.state else out / 'ime-ui.json'
    baseline = json.loads(ui_path.read_text())
    if baseline['pid'] not in [w['pid'] for w in status['windows'] if w.get('pid_verified')]:
        parser.error('fixture PID does not belong to the selected application')
    report = {'status': 'waiting_for_human_click', 'baseline': baseline, 'actor': 'manual operator; not yet verified', 'cli': str(args.cli.resolve()), 'fixture_path': str(ui_path)}

    def save():
        (out / 'human-takeover.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))

    def rpc(method, params=None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(3); c.connect(str(control))
            pid, uid, _gid = struct.unpack('3i', c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != state['daemon_pid'] or uid != os.getuid():
                raise RuntimeError('control peer identity changed')
            c.sendall((json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': {
                'session_id': state['session_id'], 'controller_id': 'human-takeover-test',
                'request_id': time.monotonic_ns(), **(params or {})}}) + '\n').encode())
            data = b''
            while b'\n' not in data:
                block = c.recv(65536)
                if not block: raise RuntimeError('daemon disconnected')
                data += block
                if len(data) > 1024 * 1024: raise RuntimeError('oversized control response')
        response = json.loads(data.split(b'\n')[0])
        if response.get('id') != 1: raise RuntimeError('response identity mismatch')
        if 'error' in response: raise RuntimeError(response['error'])
        return response['result']

    save()
    mutated = False
    try:
        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            if json.loads(ui_path.read_text())['clicks'] > baseline['clicks']:
                break
            time.sleep(.05)
        else:
            report.update({'status': 'NOT_RUN', 'reason': 'no manual trigger received'})
            return 2
        quiet_deadline = time.monotonic() + 10
        while rpc('collaboration.status')['human_active']:
            if time.monotonic() >= quiet_deadline: raise RuntimeError('human is still active; no stream started')
            time.sleep(.05)
        mutated = True
        rpc('seat.resume')
        lease = rpc('control.acquire', {'ttl_ms': 30000})
        report['stream_started_ns'] = time.monotonic_ns()
        operation = rpc('keyboard.type', {'text': '0' * 4000, 'interval_ms': 10,
            'lease_id': lease['lease_id'], 'control_epoch': lease['control_epoch']})
        report.update({'status': 'waiting_for_human_key', 'lease': lease, 'submitted': operation})
        print('Hold the ordinary a key for one second, then release it inside the test window.', flush=True)
        save()
        deadline = time.monotonic() + 25
        while operation['execution'] == 'running' and time.monotonic() < deadline:
            time.sleep(.02)
            operation = rpc('operation.get', {'operation_id': operation['operation_id']})
            heartbeat = rpc('session.heartbeat')
            if heartbeat['human_held']: report['human_held_observed'] = heartbeat
        report['operation'] = operation
        report['heartbeat_after'] = rpc('session.heartbeat')
        assert operation['execution'] == 'cancelled', operation
        assert 0 < operation['submitted'] < 4000, operation
        assert report['heartbeat_after']['control_epoch'] > lease['control_epoch']
        # Wait for the human to release the held key; never send a synthetic release.
        release_deadline = time.monotonic() + 8
        while time.monotonic() < release_deadline:
            h = rpc('session.heartbeat')
            if not h['human_held']:
                report['human_released'] = h
                break
            time.sleep(.05)
        else:
            raise RuntimeError('human key remained held after the release deadline')
        report['fixture_after'] = json.loads(ui_path.read_text())
        verify_takeover(report)
        report.update({'status': 'PASS', 'actor': 'manual operator with backend hold and application a-key press/release evidence'})
    except Exception as error:
        report.update({'status': 'FAIL', 'error': repr(error)})
    finally:
        if mutated:
            try: rpc('seat.pause')
            except Exception as error:
                report.update({'status': 'FAIL', 'pause_error': repr(error)})
        save()
        print(json.dumps({'status': report['status'], 'report': str(out / 'human-takeover.json')}, ensure_ascii=False))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == "__main__":
    raise SystemExit(main())
