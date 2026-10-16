#!/usr/bin/env python3
"""Independent Qt widget implementation of the semantic acceptance task."""
import argparse
import json
import os
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton, QCheckBox, QSlider, QListWidget, QDialog

p=argparse.ArgumentParser();p.add_argument('--state',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args()
a.output_dir.mkdir(parents=True,exist_ok=True)
app=QApplication([]);window=QWidget();window.setWindowTitle('AgentSeat semantic controls Qt')
layout=QVBoxLayout(window);layout.setContentsMargins(24,24,24,24);layout.setSpacing(14)
state={'count':0,'saved':None,'pid':os.getpid(),'title':'Draft','checked':False,'level':20,'selected':-1}
def write():
 temp=a.state.with_suffix('.tmp');temp.write_text(json.dumps(state,ensure_ascii=False));temp.replace(a.state)
def named(widget,name):widget.setAccessibleName(name);return widget
layout.addWidget(QLabel('AgentSeat — Qt semantic action acceptance'))
entry=named(QLineEdit('Draft'),'Document title');layout.addWidget(entry)
entry.textChanged.connect(lambda text:(state.update(title=text),write()))
count=QPushButton('Count');layout.addWidget(count);count_label=QLabel('Count 0');layout.addWidget(count_label)
def count_clicked():state['count']+=1;count_label.setText('Count '+str(state['count']));write()
count.clicked.connect(count_clicked)
check=QCheckBox('Enable option');layout.addWidget(check);check.toggled.connect(lambda v:(state.update(checked=v),write()))
scale=named(QSlider(Qt.Horizontal),'Level');scale.setRange(0,100);scale.setValue(20);layout.addWidget(scale)
scale.valueChanged.connect(lambda v:(state.update(level=v),write()))
choices=named(QListWidget(),'Choices');choices.addItems(['Alpha','Beta','Gamma']);layout.addWidget(choices)
choices.itemSelectionChanged.connect(lambda:(state.update(selected=choices.row(choices.selectedItems()[0]) if choices.selectedItems() else -1),write()))
disabled=QPushButton('Disabled action');disabled.setEnabled(False);layout.addWidget(disabled)
password=named(QLineEdit('PRIVATE_SENTINEL_do_not_read'),'Protected field');password.setEchoMode(QLineEdit.Password);layout.addWidget(password)
save_as=QPushButton('Save as');layout.addWidget(save_as)
def open_dialog():
 dialog=QDialog(window);dialog.setWindowTitle('Save document');dialog.setModal(True);content=QVBoxLayout(dialog)
 filename=named(QLineEdit('draft.txt'),'File name');content.addWidget(filename)
 save=QPushButton('Save document');content.addWidget(save)
 def commit():
  name=filename.text()
  if not name or Path(name).name!=name:return
  target=a.output_dir/name;target.write_text(entry.text());state['saved']=str(target);write();dialog.accept();dialog.deleteLater()
 save.clicked.connect(commit);dialog.resize(480,200);dialog.show();filename.setFocus()
save_as.clicked.connect(open_dialog)
window.resize(800,760);window.show();write();app.exec()
