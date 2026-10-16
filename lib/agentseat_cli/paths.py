"""Installation, XDG and per-instance runtime paths."""

from __future__ import annotations

import os
from pathlib import Path

# This package lives in <root>/lib/agentseat_cli, in the source tree and in a release.
ROOT = Path(os.environ.get("AGENTSEAT_ROOT", Path(__file__).resolve().parents[2])).resolve()
CONTROLLER = ROOT / "bin/agentseat"
AGENTSEAT_VERSION = "0.6.2"
USER_HOME = Path.home()
XDG_STATE_BASE = Path(os.environ.get("XDG_STATE_HOME", USER_HOME / ".local/state")).expanduser().resolve()
CONFIG_HOME = Path(
    os.environ.get(
        "AGENTSEAT_CONFIG_HOME",
        Path(os.environ.get("XDG_CONFIG_HOME", USER_HOME / ".config")) / "agentseat",
    )
).expanduser().resolve()
DATA_HOME = Path(
    os.environ.get(
        "AGENTSEAT_DATA_HOME",
        Path(os.environ.get("XDG_DATA_HOME", USER_HOME / ".local/share")) / "agentseat",
    )
).expanduser().resolve()
DEFAULT_STATE_HOME = XDG_STATE_BASE / "agentseat"
STATE_HOME = Path(os.environ.get("AGENTSEAT_STATE_HOME", DEFAULT_STATE_HOME)).expanduser().resolve()
CACHE_HOME = Path(
    os.environ.get(
        "AGENTSEAT_CACHE_HOME",
        Path(os.environ.get("XDG_CACHE_HOME", USER_HOME / ".cache")) / "agentseat",
    )
).expanduser().resolve()
USER_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
RUNTIME = Path(os.environ.get("AGENTSEAT_RUNTIME_DIR", USER_RUNTIME_DIR / "agentseat")).expanduser().resolve()
INSTANCE_RUNTIME_ROOT = USER_RUNTIME_DIR / "agentseat-apps"
INSTANCE_STATE_ROOT = DEFAULT_STATE_HOME / "apps"
SELECTED_INSTANCE = os.environ.get("AGENTSEAT_INSTANCE")
LOG_DIR = STATE_HOME / "logs"
EVIDENCE = STATE_HOME / "captures"
CONFIG_FILE = Path(os.environ.get("AGENTSEAT_CONFIG_FILE", CONFIG_HOME / "config.toml")).expanduser().resolve()
STATE = RUNTIME / "state.json"
LAUNCH_SPEC = RUNTIME / "launch.json"
NATIVE_DAEMON = ROOT / "bin/agentseatd"
EYE_BINARY = ROOT / "bin/agentseat-eye"
RUN_MICROHOST = ROOT / "bin/run-microhost"
LAUNCH_APP = ROOT / "bin/launch-app"
MICROHOST_EXE = ROOT / "bin/agentseat-microhost"
GRIM = Path("/usr/bin/grim")
MICROHOST_PID = RUNTIME / "microhost.pid"
EYE_PID = RUNTIME / "eye.pid"
APPLICATION_ENV = RUNTIME / "application-env.json"
MICROHOST_READY = RUNTIME / "microhost.ready"
CONTROL_SOCKET = RUNTIME / "control.sock"
ACTIVITY_SOCKET = RUNTIME / "human.sock"
LIFECYCLE_LOCK = RUNTIME / "lifecycle.lock"
