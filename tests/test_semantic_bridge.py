"""Privacy and bounded waits at the semantic bridge boundary."""
import collections
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest
ROOT=Path(__file__).resolve().parents[1]
loader=importlib.machinery.SourceFileLoader('semantic_bridge',str(ROOT/'bin/agentseat-a11y'))
spec=importlib.util.spec_from_loader(loader.name,loader)
bridge=importlib.util.module_from_spec(spec);loader.exec_module(bridge)


def test_password_role_blocks_content_even_when_localized_role_name_is_generic():
 b=bridge.Bridge(Path('/unused'),True,'simple');b.bus=None;b.scoped_peer=lambda owner:(42,'start')
 calls=[]
 def call(bus,owner,path,interface,method,signature=None,values=None):
  calls.append((interface,method,values))
  if method=='GetRole':return (int(bridge.Atspi.Role.PASSWORD_TEXT),)
  if method=='GetRoleName':return ('text box',)
  if method=='GetInterfaces':return ([bridge.API+'Text',bridge.API+'EditableText'],)
  if method=='GetState':return ([0,0],)
  if method=='Get' and values[1]=='Parent':return (('', '/org/a11y/atspi/null'),)
  if method=='Get' and values[1]=='ChildCount':return (0,)
  pytest.fail('protected content method was called: '+repr((interface,method,values)))
 b.call=call
 node=b.node(':1.1','/entry',0,'ref',details=True)
 assert node['protected'] and node['name']==node['description']==''
 assert all(method!='GetAll' for _,method,_ in calls)


@pytest.mark.parametrize(('interface','expected'),[
 ('Accessible',{'Name':'Save','Description':'Save document','Parent':(':1.1','/window'),'ChildCount':0}),
 ('Value',{'MinimumValue':0.,'MaximumValue':100.,'MinimumIncrement':1.,'CurrentValue':42.}),
])
def test_named_properties_work_when_provider_bulk_enumeration_is_fatal(interface,expected):
 b=bridge.Bridge(Path('/unused'),True,'simple');b.bus=None
 def call(bus,owner,path,iface,method,signature=None,values=None):
  if method=='GetAll':raise RuntimeError('provider exited while enumerating properties')
  assert iface=='org.freedesktop.DBus.Properties' and method=='Get'
  assert values[0]==bridge.API+interface
  return (expected[values[1]],)
 b.call=call
 assert b.properties(':1.1','/object',bridge.API+interface)==expected


def text_state_bridge(*, protected=False, count=1, total=20, caret=7, selection=(2,7)):
 b=bridge.Bridge(Path('/unused'),True,'simple');b.bus=None;b.resolve=lambda _:(':1.1','/text')
 b.node=lambda *_:{'protected':protected,'interfaces':[bridge.API+'Text']}
 calls=[]
 def call(bus,owner,path,interface,method,signature=None,values=None):
  calls.append((method,values))
  if method=='Get':return ({'CharacterCount':total,'CaretOffset':caret}[values[1]],)
  if method=='GetNSelections':return (count,)
  if method=='GetSelection':return selection
  pytest.fail('unexpected content read or write '+method)
 b.call=call
 return b,calls


def test_text_state_protected_nodes_reject_before_reading_offsets():
 b,calls=text_state_bridge(protected=True)
 with pytest.raises(RuntimeError,match='PROTECTED_CONTENT'):b.text_state({'node_ref':'r'})
 assert not calls


def test_text_state_reads_offsets_without_text_or_writes_and_caps_selections():
 b,calls=text_state_bridge(count=100000)
 result=b.text_state({'node_ref':'r'})
 assert result['truncated'] and not result['atomic']
 assert len(result['selections'])==16 and result['selection_count']==100000
 assert sum(method=='GetSelection' for method,_ in calls)==16
 assert result['caret_offset']==7


@pytest.mark.parametrize('selection',[(-1,2),(8,2),(2,21)])
def test_text_state_rejects_inconsistent_selection(selection):
 b,_=text_state_bridge(selection=selection)
 with pytest.raises(RuntimeError,match='TEXT_STATE_CHANGED'):b.text_state({'node_ref':'r'})


def test_text_state_detects_caret_changes_during_read():
 b,_=text_state_bridge();original=b.call;seen=0
 def call(*args):
  nonlocal seen
  if args[-1]==(bridge.API+'Text','CaretOffset'):
   seen+=1
   if seen==2:return (8,)
  return original(*args)
 b.call=call
 with pytest.raises(RuntimeError,match='TEXT_STATE_CHANGED'):b.text_state({'node_ref':'r'})


def test_expired_wait_never_rechecks_and_reports_a_late_state_as_success(monkeypatch):
 b=object.__new__(bridge.Bridge)
 monkeypatch.setattr(bridge.time,'monotonic',lambda:11)
 b.evaluate=lambda _:pytest.fail('an expired wait must not inspect a later state')
 job={'checks':1,'deadline':10,'status':'pending'}
 b.check_wait(job)
 assert job['status']=='deadline_exceeded'


def test_removed_parent_invalidates_descendants_but_keeps_unrelated_toolbar():
 b=bridge.Bridge(Path('/unused'),True,'simple')
 owner=':1.7';app=(owner,'/app');dialog=(owner,'/dialog');entry=(owner,'/entry');toolbar=(owner,'/toolbar')
 b.parent_links.update({app:None,dialog:app,entry:dialog,toolbar:app})
 for name,key in [('entry',entry),('toolbar',toolbar)]:
  b.references[name]=(bridge.time.monotonic(),*key,42,'start',0,0)
 b.drain_events=lambda:None;b.scoped_peer=lambda _:(42,'start')
 b.invalidate_subtree(dialog)
 # Re-exporting the same paths does not revive the old descendant reference.
 b.parent_links.update({dialog:app,entry:dialog})
 with pytest.raises(RuntimeError,match='STALE_NODE_REF'):b.resolve('entry')
 assert b.resolve('toolbar')==toolbar


def test_removed_parent_still_invalidates_unknown_or_cyclic_ancestry():
 b=bridge.Bridge(Path('/unused'),True,'simple');owner=':1.7'
 b.parent_links.update({(owner,'/cycle'):(owner,'/cycle')})
 for path in ['/unknown','/cycle']:
  b.references[path]=(bridge.time.monotonic(),owner,path,42,'start',0,0)
 b.invalidate_subtree((owner,'/removed'))
 assert b.generations[(owner,'/unknown')]==b.generations[(owner,'/cycle')]==1


@pytest.mark.parametrize('params',[{'max_nodes':2049},{'max_nodes':True},{'max_depth':33},{'max_depth':-1}])
def test_traversal_limits_reject_unbounded_requests(params):
 b=bridge.Bridge(Path('/unused'),True,'simple')
 with pytest.raises(RuntimeError,match='INVALID_TRAVERSAL_BUDGET'):b.snapshot(**params)


def bounds_bridge(*, document=True, scale=1.6, frame_pixels=False):
 b=bridge.Bridge(Path('/unused'),True,'simple');b.bus=None;b.scoped_peer=lambda _:(42,'start')
 role=bridge.Atspi.Role
 nodes={
  '/button':(int(role.PUSH_BUTTON),'Alpha',('/doc' if document else '/frame'),[128,160,224,77]),
  '/doc':(int(role.DOCUMENT_WEB),'Document','/viewport',[0,0,2548,1536]),
  '/viewport':(int(role.PANEL),'','/frame',[0,0,1592,960]),
  '/frame':(int(role.FRAME),'Window','/app',[0,0,2548,1536] if frame_pixels else [0,0,1592,960]),
  '/app':(int(role.APPLICATION),'App','/null',[0,0,0,0]),
 }
 def properties(owner,path,interface):
  return {'Name':nodes[path][1],'Parent':(owner,nodes[path][2])}
 b.properties=properties
 def call(bus,owner,path,interface,method,signature=None,values=None):
  if method=='GetRole':return (nodes[path][0],)
  if method=='GetExtents':return (nodes[path][3],)
  pytest.fail('unexpected read '+method)
 b.call=call
 window={'id':'w-1','pid_verified':True,'pid':42,'title':'Window','popup':False,
         'width':1592,'height':960,'x':32,'y':40,'geometry_version':7}
 output={'scale':scale,'geometry_version':7,'truncated':False}
 b.control_read=lambda method:[window] if method=='window.list' else {'output':output}
 def mapped():return b.map_bounds(':1.7','/button',nodes['/button'][3],42,properties(':1.7','/button',''),nodes['/button'][0])
 return b,nodes,window,output,mapped


def test_mixed_document_pixels_are_not_mislabelled_as_logical_coordinates():
 b,nodes,window,output,mapped=bounds_bridge()
 result=mapped()
 assert result['mapping']=='compositor_scaled'
 assert (result['x'],result['y'],result['width'])==(112,140,140)
 assert result['raw']['x']==128
 assert result['unit_inferred'] and result['requires_visual_confirmation']
 assert result['scale_evidence'][0]['kind']=='document_parent_extent_scale'


def test_hidden_toolkit_position_sentinel_is_not_a_mapped_coordinate():
 b,nodes,window,output,mapped=bounds_bridge(document=False)
 nodes['/button'][3][:]=[-2147483648,-2147483648,1,1]
 assert mapped()['reason']=='toolkit_position_unavailable'
 assert mapped()['mapping']=='bounds_unmapped'


def test_large_document_with_unproven_units_fails_closed():
 b,nodes,window,output,mapped=bounds_bridge()
 nodes['/doc'][3][3]=1200
 result=mapped()
 assert result['mapping']=='bounds_unmapped'
 assert result['reason']=='document_coordinate_units_unconfirmed'


def test_unscaled_toolkit_frame_keeps_its_existing_coordinate_contract():
 b,nodes,window,output,mapped=bounds_bridge(document=False)
 result=mapped()
 assert result['mapping']=='compositor_verified'
 assert (result['x'],result['y'])==(160,200)
 assert 'unit_inferred' not in result


def test_uniform_pixel_toolkit_frame_uses_the_compositor_scale():
 b,nodes,window,output,mapped=bounds_bridge(document=False,frame_pixels=True)
 result=mapped()
 assert result['mapping']=='compositor_scaled'
 assert result['x']==112 and result['y']==140


def test_geometry_change_during_mapping_does_not_certify_coordinates():
 b,nodes,window,output,mapped=bounds_bridge(document=False)
 output['geometry_version']=8
 assert mapped()['mapping']=='bounds_unmapped'


def test_web_dialog_role_does_not_hide_its_actual_native_frame():
 b,nodes,window,output,mapped=bounds_bridge()
 nodes['/dialog']=(int(bridge.Atspi.Role.DIALOG),'Web confirmation','/doc',[0,0,400,300])
 nodes['/button']=(nodes['/button'][0],'Alpha','/dialog',nodes['/button'][3])
 assert mapped()['mapping']=='compositor_scaled'


def test_top_level_panel_can_match_a_real_qt_window():
 b,nodes,window,output,mapped=bounds_bridge(document=False)
 nodes['/frame']=(int(bridge.Atspi.Role.PANEL),'Window','/app',[0,0,1592,960])
 assert mapped()['mapping']=='compositor_verified'


def test_ime_preserves_user_scheme_and_keeps_learning_in_private_copy(tmp_path):
 config = tmp_path/'user-config/fcitx5'
 data = tmp_path/'user-data/fcitx5/rime'
 config.mkdir(parents=True); data.mkdir(parents=True)
 (config/'profile').write_text('[Groups/0]\nDefaultIM=rime\n')
 (config/'config').write_text('[Hotkey/AltTriggerKeys]\n0=Shift_L\n')
 (data/'user.yaml').write_text('var:\n  previously_selected_schema: double_pinyin_flypy\n')
 (data/'custom.lua').write_text('return {}')
 (data/'LOCK').write_text('live host lock')
 env = bridge.prepare_ime(tmp_path/'runtime', {'XDG_CONFIG_HOME':str(config.parent), 'XDG_DATA_HOME':str(data.parent.parent)})
 private = Path(env['XDG_DATA_HOME'])/'fcitx5/rime'
 assert (Path(env['XDG_CONFIG_HOME'])/'fcitx5/profile').read_text() == (config/'profile').read_text()
 assert (private/'user.yaml').read_text() == (data/'user.yaml').read_text()
 assert (private/'custom.lua').read_text() == 'return {}'
 assert not (private/'LOCK').exists()
 (private/'custom.lua').write_text('private learning')
 assert (data/'custom.lua').read_text() == 'return {}'
 assert (data/'LOCK').read_text() == 'live host lock'
 # Restart snapshots the current profile, rather than retaining stock Pinyin.
 (config/'profile').write_text('[Groups/0]\nDefaultIM=new-scheme\n')
 bridge.prepare_ime(tmp_path/'runtime', {'XDG_CONFIG_HOME':str(config.parent), 'XDG_DATA_HOME':str(data.parent.parent)})
 assert 'new-scheme' in (Path(env['XDG_CONFIG_HOME'])/'fcitx5/profile').read_text()
