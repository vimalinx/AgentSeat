#!/usr/bin/env python3
"""Owned AT-SPI application child with deterministic delayed replies and events.

The real GTK parent remains visible. This child exercises transport semantics
that toolkit Action implementations do not necessarily expose synchronously.
"""
import argparse
import json
import os
from pathlib import Path
import gi
gi.require_version('Gio','2.0')
from gi.repository import Gio,GLib

p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);a=p.parse_args()
state={'received':0,'completed':0,'floods':0,'replacements':0,'pid':os.getpid()}
def save():
 temp=a.state.with_suffix('.tmp');temp.write_text(json.dumps(state));temp.replace(a.state)
save()
bus=Gio.DBusConnection.new_for_address_sync(os.environ['AT_SPI_BUS_ADDRESS'],Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT|Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,None,None)
ROOT='/org/a11y/atspi/accessible/root';BASE='/org/a11y/atspi/accessible/'
children=['delayed','flood','replace'];names={'delayed':'Delayed action','flood':'Flood events','replace':'Replace target'}
owner=bus.get_unique_name()
XML='''<node>
<interface name="org.a11y.atspi.Accessible">
 <property name="Name" type="s" access="read"/><property name="Description" type="s" access="read"/>
 <property name="Parent" type="(so)" access="read"/><property name="ChildCount" type="i" access="read"/>
 <method name="GetRole"><arg type="u" direction="out"/></method><method name="GetRoleName"><arg type="s" direction="out"/></method>
 <method name="GetInterfaces"><arg type="as" direction="out"/></method><method name="GetState"><arg type="au" direction="out"/></method>
 <method name="GetChildren"><arg type="a(so)" direction="out"/></method>
</interface>
<interface name="org.a11y.atspi.Action">
 <property name="NActions" type="i" access="read"/>
 <method name="GetActions"><arg type="a(sss)" direction="out"/></method>
 <method name="GetName"><arg type="i" direction="in"/><arg type="s" direction="out"/></method>
 <method name="DoAction"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
</interface>
</node>'''
info=Gio.DBusNodeInfo.new_for_xml(XML)
def props(_c,_s,path,interface,prop):
 root=path==ROOT;key=path.rsplit('/',1)[1]
 if prop=='Name':return GLib.Variant('s','Fault transport fixture' if root else names[key])
 if prop=='Description':return GLib.Variant('s','')
 if prop=='Parent':return GLib.Variant('(so)',('', '/org/a11y/atspi/null') if root else (owner,ROOT))
 if prop=='ChildCount':return GLib.Variant('i',len(children) if root else 0)
 if prop=='NActions':return GLib.Variant('i',1)
 return None

def method(_c,_s,path,interface,name,parameters,invocation):
 root=path==ROOT;key=path.rsplit('/',1)[1]
 if name=='GetRole':invocation.return_value(GLib.Variant('(u)',(75 if root else 43,)))
 elif name=='GetRoleName':invocation.return_value(GLib.Variant('(s)',('application' if root else 'button',)))
 elif name=='GetInterfaces':invocation.return_value(GLib.Variant('(as)',(['org.a11y.atspi.Accessible']+([] if root else ['org.a11y.atspi.Action']),)))
 elif name=='GetState':invocation.return_value(GLib.Variant('(au)',([sum(1<<i for i in [8,24,25,30]),0],)))
 elif name=='GetChildren':invocation.return_value(GLib.Variant('(a(so))',([(owner,BASE+name) for name in children] if root else [],)))
 elif name=='GetActions':invocation.return_value(GLib.Variant('(a(sss))',([('Invoke','Invoke the test control','')],)))
 elif name=='GetName':invocation.return_value(GLib.Variant('(s)',('invoke',)))
 elif name=='DoAction':
  if parameters.unpack()!=(0,):invocation.return_value(GLib.Variant('(b)',(False,)));return
  if key=='delayed':
   state['received']+=1;save()
   def done():
    state['completed']+=1;save();invocation.return_value(GLib.Variant('(b)',(True,)));return False
   GLib.timeout_add(900,done)
  elif key=='flood':
   for index in range(600):
    bus.emit_signal(None,path,'org.a11y.atspi.Event.Object','TextChanged',GLib.Variant('(siiva{sv})',('insert',index,1,GLib.Variant('s','PRIVATE_EVENT_SENTINEL'),{})))
   state['floods']+=1;save();invocation.return_value(GLib.Variant('(b)',(True,)))
  elif key=='replace':
   bus.emit_signal(None,ROOT,'org.a11y.atspi.Event.Object','ChildrenChanged',GLib.Variant('(siiva{sv})',('remove',0,0,GLib.Variant('(so)',(owner,BASE+'delayed')),{})))
   for identifier in registrations['delayed']:bus.unregister_object(identifier)
   register('delayed')
   bus.emit_signal(None,ROOT,'org.a11y.atspi.Event.Object','ChildrenChanged',GLib.Variant('(siiva{sv})',('add',0,0,GLib.Variant('(so)',(owner,BASE+'delayed')),{})))
   state['replacements']+=1;save();invocation.return_value(GLib.Variant('(b)',(True,)))
 else:invocation.return_dbus_error('org.freedesktop.DBus.Error.UnknownMethod','Unsupported test method')
registrations={}
def register(name):
 registrations[name]=[bus.register_object(BASE+name,interface,method,props,None) for interface in info.interfaces]
bus.register_object(ROOT,info.interfaces[0],method,props,None)
for child in children:register(child)
# GTK's parent process owns this child; native scope checks still apply.
bus.call_sync('org.a11y.atspi.Registry',ROOT,'org.a11y.atspi.Socket','Embed',GLib.Variant('((so))',((owner,ROOT),)),None,Gio.DBusCallFlags.NONE,1000,None)
GLib.MainLoop().run()
