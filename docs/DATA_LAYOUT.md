# Runtime data layout

AgentSeat follows the XDG Base Directory specification and keeps its checkout
read-only at runtime.

| Purpose | Default path | Lifetime |
| --- | --- | --- |
| User configuration | `${XDG_CONFIG_HOME:-~/.config}/agentseat/config.toml` | persistent |
| Per-program HUD position | `${XDG_CONFIG_HOME:-~/.config}/agentseat/applications/<program-hash>/hud-position.json` | persistent |
| Persistent application data | `${XDG_DATA_HOME:-~/.local/share}/agentseat/` | persistent |
| Logs | `${XDG_STATE_HOME:-~/.local/state}/agentseat/logs/` | persistent, user-managed |
| Explicit captures | `${XDG_STATE_HOME:-~/.local/state}/agentseat/captures/` | persistent, user-managed |
| Named instance logs and captures | `${XDG_STATE_HOME:-~/.local/state}/agentseat/apps/app-<hash>/` | persistent, user-managed |
| Rebuildable cache | `${XDG_CACHE_HOME:-~/.cache}/agentseat/` | disposable |
| Sockets, PID, lifecycle lock and current state | `$XDG_RUNTIME_DIR/agentseat/` | current login session |
| Named application runtimes | `$XDG_RUNTIME_DIR/agentseat-apps/app-<hash>/` | current login session |

Directories are mode 0700. State, launch files, logs, and captures are mode
0600. The runtime directory is disposable and must not be backed up. A background
application with no open window retains its live runtime; closing a window is
not equivalent to deleting the runtime or stopping the application.
The current `state.json` contains the random session identity used to pin
heartbeat reconnects. It is runtime coordination state, not an account token
or persistent Agent credential, and disappears when the session stops.
`agentseat launch` creates one runtime/state pair per automatically named
application. `agentseat instances` discovers recorded pairs and reports whether
each one is currently running, while
`agentseat --instance <id> ...` reconnects to exactly one pair.

HUD positions use a SHA-256 key of the resolved program path (plus the script or
module for direct Python launches), independent of instance names and document
arguments. The file contains a version and normalized `x`/`y` positions within the
available movement area. Drag completion writes it atomically with mode 0600.
The next launch restores it and adapts to output size/scale changes. Invalid or
out-of-range positions fall back to the bottom-right default. `AGENTSEAT_CONFIG_HOME`
also controls this directory.

Chat messages shown by the collaboration HUD remain only in the daemon's
memory and disappear when AgentSeat stops. AgentSeat does not create a transcript.
Observation PNGs are written only on explicit CLI/API capture requests. The
native daemon retains at most eight `f-*.png` files below its runtime directory;
these are removed on normal daemon shutdown. Saved `observe` outputs remain
user-managed. A crash can leave transient files until runtime cleanup/login end.

Optional `--a11y`/`--ime fcitx5` sessions add `a11y.sock`, `a11y.json` and a private
`session-bus.conf` below the instance runtime. The bridge removes its socket and
receipt on normal exit. Private IME configuration/data/cache live in `ime-config/`,
`ime-data/` and `ime-cache/` there, and do not replace the user's host IME settings.
These are disposable login-session data, not durable personalized dictionaries.

`agentseat paths` prints the effective locations. Advanced packagers and test
harnesses can override a complete AgentSeat directory with
`AGENTSEAT_CONFIG_HOME`, `AGENTSEAT_DATA_HOME`, `AGENTSEAT_STATE_HOME`,
`AGENTSEAT_CACHE_HOME`, or `AGENTSEAT_RUNTIME_DIR`.

The optional `config.toml` currently accepts:

```toml
[general]
default_workspace = 1
human_priority_grace_ms = 1500
eye_hud = false
```

The collaboration head is disabled by default (`eye_hud = false`). Set it to
`true` only when the human-facing task and chat surface is wanted; observation
and virtual input continue to work without it.

Invalid types and unsafe ranges fail closed with a clear error. The installer
places an example beside the application but never creates or overwrites the
user's configuration.

The installed Python client is `share/python/agentseat_client.py` within each
immutable release and participates in its digest and integrity manifest.
`agentseat paths` exposes the exact file as `python_sdk`. Accessibility snapshots,
references, event metadata and waits are bounded in-memory state in the optional
bridge. Text/value event payloads are not retained. Semantic operation receipts
live in the daemon's bounded memory; the bridge does not execute writes.
