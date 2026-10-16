#!/usr/bin/env python3
"""Disposable non-GUI background work which the application deliberately leaves alive."""
import json,os,sys,time
from pathlib import Path
state=Path(sys.argv[1]);ticks=0
while True:
    temporary=state.with_suffix('.tmp');temporary.write_text(json.dumps({'pid':os.getpid(),'ticks':ticks}));temporary.replace(state)
    ticks+=1;time.sleep(.1)
