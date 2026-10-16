#!/usr/bin/env python3
"""Real GTK data-device drag events, atomically published for nested tests."""
import argparse
import json
from pathlib import Path
import gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,Gdk,GLib
p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);a=p.parse_args()
state={'starts':0,'ends':0,'drops':0,'failures':0,'data':None,'pointer_events':[]}
window=Gtk.Window(title='AgentSeat drag fixture');window.set_default_size(1000,500);window.set_decorated(False)
layout=Gtk.Fixed();window.add(layout)
source=Gtk.EventBox();source.add(Gtk.Label(label='Drag source'));source.set_size_request(220,140);layout.put(source,80,120)
target=Gtk.EventBox();target.add(Gtk.Label(label='Drop target'));target.set_size_request(240,140);layout.put(target,540,120)
source.drag_source_set(Gdk.ModifierType.BUTTON1_MASK,[Gtk.TargetEntry.new('text/plain',0,0)],Gdk.DragAction.COPY)
target.drag_dest_set(Gtk.DestDefaults.ALL,[Gtk.TargetEntry.new('text/plain',0,0)],Gdk.DragAction.COPY)
def publish(*_):
 state['size']=[window.get_allocated_width(),window.get_allocated_height()]
 for name,widget in [('source',source),('target',target)]:
  r=widget.get_allocation();state[name]=[r.x,r.y,r.width,r.height]
 temporary=a.state.with_suffix('.tmp');temporary.write_text(json.dumps(state));temporary.replace(a.state)
 return False
def started(*_):state['starts']+=1;publish()
def ended(*_):state['ends']+=1;publish()
def failed(*_):state['failures']+=1;publish();return False
def get_data(widget,context,selection,info,event_time):selection.set_text('drag payload',-1)
def received(widget,context,x,y,selection,info,event_time):
 state['drops']+=1;state['data']=selection.get_text();publish();Gtk.drag_finish(context,True,False,event_time)
source.connect('drag-begin',started);source.connect('drag-end',ended);source.connect('drag-failed',failed)
source.connect('drag-data-get',get_data);target.connect('drag-data-received',received)
def pointer_event(widget,event):
 if event.type in (Gdk.EventType.BUTTON_PRESS,Gdk.EventType.BUTTON_RELEASE,Gdk.EventType.MOTION_NOTIFY):
  state['pointer_events'].append({'widget':'source' if widget==source else 'target' if widget==target else 'window',
   'type':int(event.type),'x':event.x,'y':event.y,'state':int(event.state)})
  state['pointer_events']=state['pointer_events'][-40:];publish()
 return False
for widget in [source,target,window]:
 widget.add_events(Gdk.EventMask.BUTTON_PRESS_MASK|Gdk.EventMask.BUTTON_RELEASE_MASK|Gdk.EventMask.POINTER_MOTION_MASK)
 widget.connect('event',pointer_event)
window.connect('size-allocate',publish);window.connect('destroy',Gtk.main_quit)
window.show_all();GLib.idle_add(publish);Gtk.main()
