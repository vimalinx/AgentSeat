# Runtime data layout

AgentSeat follows the XDG Base Directory specification and keeps its checkout
read-only at runtime.

| Purpose | Default path | Lifetime |
| --- | --- | --- |
| User configuration | `${XDG_CONFIG_HOME:-~/.config}/agentseat/config.toml` | persistent |
| Persistent application data | `${XDG_DATA_HOME:-~/.local/share}/agentseat/` | persistent |
| Logs | `${XDG_STATE_HOME:-~/.local/state}/agentseat/logs/` | persistent, user-managed |
| Explicit captures | `${XDG_STATE_HOME:-~/.local/state}/agentseat/captures/` | persistent, user-managed |
| Rebuildable cache | `${XDG_CACHE_HOME:-~/.cache}/agentseat/` | disposable |
| Sockets, PID and current state | `$XDG_RUNTIME_DIR/agentseat/` | current login session |

Directories are mode 0700. State, launch files, logs, and captures are mode
0600. The runtime directory is disposable and must not be backed up.
The current `state.json` contains the random session identity used to pin
heartbeat reconnects. It is runtime coordination state, not an account token
or persistent Agent credential, and disappears when the session stops.

Chat messages shown by the collaboration HUD remain only in the daemon's
memory and disappear when AgentSeat stops. AgentSeat does not create a transcript.
An observation PNG is written only when `agentseat observe` is called.

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
