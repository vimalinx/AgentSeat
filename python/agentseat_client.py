"""Thin, dependency-free client for one existing AgentSeat application session.

No app discovery by title, implicit app launch, model calls or retry of writes.
The native daemon owns actions; the verified bridge owns reads/events/waits.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time
import uuid

SEMANTIC = frozenset({'a11y.perform','a11y.set_text','a11y.set_value','a11y.focus','a11y.select','a11y.scroll_to'})
INPUT = frozenset({'window.focus','window.move','window.resize','window.close','pointer.move_absolute','pointer.button','pointer.click','pointer.scroll','keyboard.key','keyboard.type','keyboard.paste'}) | SEMANTIC
WRITES = INPUT | {'seat.create','seat.destroy','seat.pause','seat.resume','shutdown','window.lease','window.release','control.acquire','control.renew','control.release','operation.cancel'}

class AgentSeatError(RuntimeError):
    def __init__(self, kind, receipt=None):
        super().__init__(kind)
        self.kind, self.receipt = kind, receipt

@dataclass(frozen=True)
class EventCursor:
    scope_id: str
    sequence: int


def _identity(pid):
    fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    return int(fields[1]), fields[19]


def _descendant(pid, root):
    for _ in range(32):
        if pid == root:return True
        if pid <= 1:return False
        pid=_identity(pid)[0]
    return False


class Session:
    def __init__(self, runtime, *, controller=None):
        self.runtime=Path(runtime)
        state=json.loads((self.runtime/'state.json').read_text())
        self.session_id=state['session_id']
        self.daemon_pid=state['daemon_pid'];self.daemon_start=_identity(self.daemon_pid)[1]
        self.microhost_pid=state['microhost_pid'];self.microhost_start=_identity(self.microhost_pid)[1]
        self.controller=controller or 'python:'+uuid.uuid4().hex
        self.lease=None;self.scope_id=None;self._renew_at=0

    @classmethod
    def from_instance(cls, instance, *, controller=None):
        runtime=Path(os.environ.get('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}'))/'agentseat-apps'/('app-'+hashlib.sha256(instance.encode()).hexdigest()[:24])
        return cls(runtime,controller=controller)

    @staticmethod
    def _exchange(path, request, expected_pid):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
            sock.settimeout(4);sock.connect(str(path))
            pid,uid,_=struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=os.getuid() or pid!=expected_pid:raise AgentSeatError('PEER_MISMATCH')
            sock.sendall((json.dumps(request,ensure_ascii=False,allow_nan=False)+'\n').encode())
            result=bytearray()
            while b'\n' not in result:
                chunk=sock.recv(16384)
                if not chunk or len(result)+len(chunk)>1048576:raise AgentSeatError('RESPONSE_INVALID')
                result.extend(chunk)
        return json.loads(result.split(b'\n')[0])

    def rpc(self, method, params=None):
        if _identity(self.daemon_pid)[1]!=self.daemon_start:raise AgentSeatError('SESSION_CHANGED')
        params=dict(params or {})
        if method in INPUT:
            if self.lease is None:self.acquire()
            elif time.monotonic() >= self._renew_at:self.renew()
        if method in WRITES:
            params.update(session_id=self.session_id,controller_id=self.controller,request_id=time.monotonic_ns())
            if method in INPUT:params.update(lease_id=self.lease['lease_id'],control_epoch=self.lease['control_epoch'])
        identifier=uuid.uuid4().hex
        try:
            response=self._exchange(self.runtime/'control.sock',{'jsonrpc':'2.0','id':identifier,'method':method,'params':params},self.daemon_pid)
            if not isinstance(response,dict) or response.get('id')!=identifier or ('result' not in response and 'error' not in response):
                raise AgentSeatError('RESPONSE_INVALID')
        except (OSError,ValueError,AgentSeatError) as error:
            if method in WRITES:
                raise AgentSeatError('WRITE_UNCONFIRMED',{'controller_id':self.controller,'request_id':params['request_id'],'method':method}) from error
            raise
        if 'error' in response:raise AgentSeatError(response['error'].get('data',{}).get('kind','RPC_FAILED'),response['error'])
        return response['result']

    def acquire(self):
        heartbeat=self.rpc('session.heartbeat')
        if heartbeat['session_id']!=self.session_id:raise AgentSeatError('SESSION_CHANGED')
        self.lease=self.rpc('control.acquire',{'ttl_ms':10000})
        self._renew_at=time.monotonic()+2
        return self.lease

    def renew(self):
        if self.lease is None:raise AgentSeatError('NO_WRITE_LEASE')
        result=self.rpc('control.renew',{'lease_id':self.lease['lease_id'],'ttl_ms':10000})
        self._renew_at=time.monotonic()+2
        return result

    def release(self):
        if self.lease is None:return
        try:return self.rpc('control.release',{'lease_id':self.lease['lease_id']})
        finally:self.lease=None

    def request_receipt(self, request_id):
        return self.rpc('request.get',{'controller_id':self.controller,'request_id':request_id})

    def read(self, method, **params):
        receipt=json.loads((self.runtime/'a11y.json').read_text())
        pid=receipt['bridge_pid']
        if _identity(pid)[1]!=receipt['bridge_start'] or _identity(self.microhost_pid)[1]!=self.microhost_start or not _descendant(pid,self.microhost_pid):
            raise AgentSeatError('A11Y_SCOPE_STALE')
        if receipt['socket']!=str(self.runtime/'a11y.sock'):raise AgentSeatError('A11Y_SOCKET_MISMATCH')
        if self.scope_id is not None and self.scope_id!=receipt['scope_id']:raise AgentSeatError('A11Y_SCOPE_CHANGED')
        self.scope_id=receipt['scope_id']
        response=self._exchange(self.runtime/'a11y.sock',{**params,'scope_id':self.scope_id,'method':method},pid)
        if 'error' in response:raise AgentSeatError(response['error']['kind'],response['error'])
        return response['result']

    def capabilities(self):
        return {'control':self.rpc('version'),'a11y':self.read('a11y.status')}

    def snapshot(self, **params):return self.read('a11y.snapshot',**params)
    def query(self, name='', *, role=None, interface=None, root_ref=None, states=None, max_nodes=512):
        params={'selector':{'name':name,'exact':True,'role':role,'interface':interface,'states':states or []},'limit':128,'max_nodes':max_nodes}
        if root_ref is not None:params['root_ref']=root_ref
        return self.read('a11y.query',**params)

    def find(self, name, *, role=None, interface=None, root_ref=None, states=None, max_nodes=512):
        result=self.query(name,role=role,interface=interface,root_ref=root_ref,states=states,max_nodes=max_nodes)
        if result['truncated']:raise AgentSeatError('QUERY_INCOMPLETE',result)
        if result['total']!=1:raise AgentSeatError('TARGET_NOT_FOUND' if not result['total'] else 'TARGET_AMBIGUOUS',result)
        return result['nodes'][0]['node_ref']

    def node(self, node_ref):return self.read('a11y.get',node_ref=node_ref)
    def text(self, node_ref, *, start=0, count=1024):return self.read('a11y.text',node_ref=node_ref,start=start,count=count)
    def text_state(self, node_ref):return self.read('a11y.text_state',node_ref=node_ref)

    def await_operation(self, receipt, *, timeout=16):
        identifier=receipt['operation_id'];deadline=time.monotonic()+timeout;renew=time.monotonic()+2
        origin={k:receipt[k] for k in ('request_id','request_controller_id') if k in receipt}
        try:
            while receipt['execution']=='running':
                if time.monotonic()>=deadline:raise AgentSeatError('OPERATION_UNCONFIRMED',receipt)
                time.sleep(.02)
                receipt=self.rpc('operation.get',{'operation_id':identifier})
                if receipt['execution']=='running' and self.lease and time.monotonic()>=renew:
                    self.renew();renew=time.monotonic()+2
        except BaseException:
            try:self.rpc('operation.cancel',{'operation_id':identifier})
            except (AgentSeatError,OSError):pass
            raise
        return {**receipt,**origin}

    def paste(self, text):
        """Submit Ctrl+V once and await transport, then verify UI before release.

        The private source permits repeated reads until revoke or ten seconds.
        A transfer receipt does not assert application insertion.
        """
        receipt = self.await_operation(self.rpc('keyboard.paste', {'text':text}))
        if receipt['execution'] != 'finished':raise AgentSeatError('INPUT_INTERRUPTED', receipt)
        return receipt

    def act(self, method, node_ref, *, expect=None, timeout_ms=5000, **params):
        if method not in SEMANTIC:raise AgentSeatError('ACTION_UNSUPPORTED')
        receipt=self.await_operation(self.rpc(method,{**params,'node_ref':node_ref,'timeout_ms':timeout_ms}))
        if receipt['execution']!='finished':raise AgentSeatError(receipt.get('error_kind') or receipt['execution'],receipt)
        if expect is not None:
            proof=self.wait(expect,timeout_ms=timeout_ms)
            receipt['postcondition']=proof
            receipt['verification']='condition_satisfied' if proof['status']=='satisfied' else 'condition_not_satisfied'
            if proof['status']!='satisfied':raise AgentSeatError('POSTCONDITION_NOT_SATISFIED',receipt)
        return receipt

    def perform(self, node_ref, action, **kwargs):return self.act('a11y.perform',node_ref,action=action,**kwargs)
    def set_text(self, node_ref, text, **kwargs):return self.act('a11y.set_text',node_ref,text=text,**kwargs)
    def set_value(self, node_ref, value, **kwargs):return self.act('a11y.set_value',node_ref,value=value,**kwargs)

    def subscribe(self):
        value=self.read('events.subscribe');return EventCursor(value['scope_id'],value['event_seq'])
    def events(self, cursor, *, limit=64):
        if self.scope_id!=cursor.scope_id:raise AgentSeatError('EVENT_SCOPE_CHANGED')
        result=self.read('events.read',after=cursor.sequence,limit=limit)
        return result,EventCursor(result['scope_id'],result['next_seq'])

    def wait(self, condition, *, timeout_ms=5000, cancel_event=None):
        result=self.read('wait.start',condition=condition,timeout_ms=timeout_ms)
        deadline=time.monotonic()+timeout_ms/1000+4;identifier=result['wait_id']
        try:
            while result['status']=='pending':
                if cancel_event is not None and cancel_event.is_set():return self.read('wait.cancel',wait_id=identifier)
                if time.monotonic()>=deadline:raise AgentSeatError('WAIT_UNCONFIRMED',result)
                time.sleep(.05);result=self.read('wait.get',wait_id=identifier)
        except BaseException:
            try:self.read('wait.cancel',wait_id=identifier)
            except (AgentSeatError,OSError):pass
            raise
        return result

    def activate(self, node_ref=None, *, point=None, frame=None):
        if node_ref is not None:
            node=self.node(node_ref)
            if 'sensitive' not in node['states']:raise AgentSeatError('NODE_DISABLED')
            actions=[action for action in node['actions']
                     if action.get('name','').casefold() in {'click','press','activate','toggle'}]
            if node.get('role_key') in {'check-box','toggle-button','radio-button','check-menu-item','radio-menu-item'}:
                toggles=[action for action in actions if action['name'].casefold()=='toggle']
                if toggles:actions=toggles
            if len(actions)>1:raise AgentSeatError('ACTIVATION_AMBIGUOUS')
            if actions:
                receipt=self.perform(node_ref,actions[0]['index'])
                return {**receipt,'route':'semantic','route_reason':'target exposes a recognized activation Action'}
        if point is None or frame is None:raise AgentSeatError('VISUAL_TARGET_REQUIRED')
        if frame.get('session_id')!=self.session_id:raise AgentSeatError('FRAME_SESSION_MISMATCH')
        geometry=frame['geometry_version']
        self.rpc('pointer.move_absolute',{'x':point[0],'y':point[1],'geometry_version':geometry})
        receipt=self.rpc('pointer.click',{'button':272,'geometry_version':geometry})
        return {**receipt,'route':'coordinate','route_reason':'caller supplied a fresh visual target; no semantic call was attempted'}
