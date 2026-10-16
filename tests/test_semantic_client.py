"""Client boundary regressions: uncertain writes and ambiguity never become clicks."""
import importlib.util
from pathlib import Path
import sys

import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'python'))
import agentseat_client as sdk

@pytest.fixture
def session(monkeypatch,tmp_path):
 s=object.__new__(sdk.Session)
 s.runtime=tmp_path;s.session_id='session';s.daemon_pid=42;s.daemon_start='start'
 s.controller='actor';s.scope_id='scope';s.lease={'lease_id':'lease','control_epoch':2}
 s._renew_at=float('inf')
 monkeypatch.setattr(sdk,'_identity',lambda pid:(1,'start'))
 return s

@pytest.mark.parametrize('response',['disconnect','wrong_id','bad_shape'])
def test_unconfirmed_write_retains_recovery_identity_and_never_replays(session,monkeypatch,response):
 calls=[]
 def exchange(path,request,pid):
  calls.append(request)
  if response=='disconnect':raise ConnectionResetError()
  if response=='wrong_id':return {'id':'wrong','result':{}}
  return []
 monkeypatch.setattr(session,'_exchange',exchange)
 with pytest.raises(sdk.AgentSeatError) as error:session.rpc('a11y.perform',{'node_ref':'ref','action':0})
 assert error.value.kind=='WRITE_UNCONFIRMED' and len(calls)==1
 assert error.value.receipt['request_id']==calls[0]['params']['request_id']
 assert error.value.receipt['controller_id']=='actor'

@pytest.mark.parametrize(('result','kind'),[({'truncated':True,'total':1,'nodes':[{'node_ref':'x'}]},'QUERY_INCOMPLETE'),({'truncated':False,'total':2,'nodes':[{'node_ref':'x'},{'node_ref':'y'}]},'TARGET_AMBIGUOUS')])
def test_find_never_guesses_from_incomplete_or_ambiguous_tree(session,monkeypatch,result,kind):
 monkeypatch.setattr(session,'query',lambda *a,**k:result)
 with pytest.raises(sdk.AgentSeatError,match=kind):session.find('Save')


def test_unknown_semantic_result_does_not_fall_back_to_coordinates(session,monkeypatch):
 monkeypatch.setattr(session,'node',lambda ref:{'states':['sensitive'],'actions':[{'index':0,'name':'click'}]})
 def fail(*a,**k):raise sdk.AgentSeatError('outcome_unknown')
 monkeypatch.setattr(session,'perform',fail)
 monkeypatch.setattr(session,'rpc',lambda *a,**k:pytest.fail('must not send coordinate input'))
 with pytest.raises(sdk.AgentSeatError,match='outcome_unknown'):
  session.activate('ref',point=(.5,.5),frame={'session_id':'session','geometry_version':1})


def test_visual_fallback_checks_frame_session_before_input(session,monkeypatch):
 monkeypatch.setattr(session,'rpc',lambda *a,**k:pytest.fail('must not send input'))
 with pytest.raises(sdk.AgentSeatError,match='FRAME_SESSION_MISMATCH'):
  session.activate(point=(.5,.5),frame={'session_id':'other','geometry_version':1})


def test_action_success_with_failed_postcondition_is_not_success(session,monkeypatch):
 receipt={'operation_id':'op','execution':'finished','submitted':1}
 monkeypatch.setattr(session,'rpc',lambda *a,**k:receipt)
 monkeypatch.setattr(session,'await_operation',lambda r:r)
 monkeypatch.setattr(session,'wait',lambda *a,**k:{'status':'deadline_exceeded'})
 with pytest.raises(sdk.AgentSeatError) as error:session.perform('ref',0,expect={'kind':'exists'})
 assert error.value.kind=='POSTCONDITION_NOT_SATISFIED'
 assert error.value.receipt['verification']=='condition_not_satisfied'


def test_visual_fallback_uses_native_evdev_button_contract(session,monkeypatch):
    calls=[]
    monkeypatch.setattr(session,'rpc',lambda method,params:(calls.append((method,params)) or {'submitted':True}))
    result=session.activate(point=(.25,.5),frame={'session_id':'session','geometry_version':7})
    assert calls==[('pointer.move_absolute',{'x':.25,'y':.5,'geometry_version':7}),
                   ('pointer.click',{'button':272,'geometry_version':7})]
    assert result['route']=='coordinate'


def test_active_client_renews_before_next_action_without_reacquiring(session,monkeypatch):
    session._renew_at=0;calls=[]
    def exchange(path,request,pid):
        calls.append(request['method'])
        return {'id':request['id'],'result':{'accepted':True}}
    monkeypatch.setattr(session,'_exchange',exchange)
    session.rpc('a11y.perform',{'node_ref':'ref','action':0})
    assert calls==['control.renew','a11y.perform']


def test_expired_lease_never_silently_reacquires_or_submits_action(session,monkeypatch):
    session._renew_at=0;calls=[]
    def exchange(path,request,pid):
        calls.append(request['method'])
        return {'id':request['id'],'error':{'data':{'kind':'NO_WRITE_LEASE'}}}
    monkeypatch.setattr(session,'_exchange',exchange)
    with pytest.raises(sdk.AgentSeatError,match='NO_WRITE_LEASE'):
        session.rpc('a11y.perform',{'node_ref':'ref','action':0})
    assert calls==['control.renew']


def test_paste_unknown_is_not_insertion_or_retried(session, monkeypatch):
 calls=[]
 monkeypatch.setattr(session,'rpc',lambda method,params:(calls.append((method,params)) or {'operation_id':'id','execution':'running'}))
 monkeypatch.setattr(session,'await_operation',lambda receipt:{'operation_id':'id','execution':'outcome_unknown','submitted':1})
 with pytest.raises(sdk.AgentSeatError) as error:session.paste('Unicode 中文')
 assert error.value.kind=='INPUT_INTERRUPTED'
 assert error.value.receipt['submitted']==1
 assert [method for method,_ in calls]==['keyboard.paste']


def test_paste_does_not_promote_transport_to_application_effect(session, monkeypatch):
 receipt={'operation_id':'id','execution':'finished','verification':'clipboard_transport_only','clipboard_transfers':1}
 monkeypatch.setattr(session,'rpc',lambda method,params:receipt)
 monkeypatch.setattr(session,'await_operation',lambda value:value)
 assert session.paste('Unicode 中文')==receipt


def test_activation_skips_focus_action(session,monkeypatch):
 calls=[]
 monkeypatch.setattr(session,'node',lambda ref:{'states':['sensitive'],'actions':[{'index':0,'name':'SetFocus'},{'index':1,'name':'Press'}]})
 monkeypatch.setattr(session,'perform',lambda ref,index:(calls.append(index) or {}))
 assert session.activate('ref')['route']=='semantic'
 assert calls==[1]


@pytest.mark.parametrize('name',['SetFocus','unknown',''])
def test_nonactivation_action_requires_visual_target(session,monkeypatch,name):
 monkeypatch.setattr(session,'node',lambda ref:{'states':['sensitive'],'actions':[{'index':0,'name':name,'localized_name':'click'}]})
 monkeypatch.setattr(session,'perform',lambda *a:pytest.fail('must not dispatch nonactivation action'))
 with pytest.raises(sdk.AgentSeatError,match='VISUAL_TARGET_REQUIRED'):session.activate('ref')
 calls=[]
 monkeypatch.setattr(session,'rpc',lambda method,params:(calls.append(method) or {}))
 assert session.activate('ref',point=(.5,.5),frame={'session_id':'session','geometry_version':1})['route']=='coordinate'
 assert calls==['pointer.move_absolute','pointer.click']


def test_ambiguous_activation_requires_explicit_action(session,monkeypatch):
 monkeypatch.setattr(session,'node',lambda ref:{'states':['sensitive'],'actions':[{'index':0,'name':'click'},{'index':1,'name':'toggle'}]})
 monkeypatch.setattr(session,'perform',lambda *a:pytest.fail('must not guess'))
 monkeypatch.setattr(session,'rpc',lambda *a:pytest.fail('must not fall back'))
 with pytest.raises(sdk.AgentSeatError,match='ACTIVATION_AMBIGUOUS'):
  session.activate('ref',point=(.5,.5),frame={'session_id':'session','geometry_version':1})


def test_checkable_role_prefers_toggle_over_press(session,monkeypatch):
 calls=[]
 monkeypatch.setattr(session,'node',lambda ref:{'states':['sensitive'],'role_key':'check-box','actions':[{'index':0,'name':'Press'},{'index':1,'name':'Toggle'}]})
 monkeypatch.setattr(session,'perform',lambda ref,index:(calls.append(index) or {}))
 assert session.activate('ref')['route']=='semantic'
 assert calls==[1]
