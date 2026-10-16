<p align="center">
  <img src="assets/brand/agentseat-hero.svg" alt="AgentSeat — one app, two operators" width="100%">
</p>

<p align="center">
  <strong>English</strong> · <a href="README.zh-CN.md">简体中文</a>
</p>

<h1 align="center">AgentSeat</h1>

<p align="center">
  <strong>A private seat for an AI Agent inside one local application.</strong><br>
  The Agent gets its own pointer and keyboard. You keep yours.
</p>

<p align="center">
  <a href="https://github.com/vimalinx/AgentSeat/releases/latest"><img alt="GitHub release" src="https://img.shields.io/github/v/release/vimalinx/AgentSeat?style=flat-square&amp;color=57b8ed"></a>
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-080c11?style=flat-square"></a>
  <a href="https://linux.do/"><img alt="LINUX DO Community" src="https://img.shields.io/badge/community-LINUX%20DO-1F883D?style=flat-square&amp;logo=discourse&amp;logoColor=white"></a>
  <img alt="Wayland native" src="https://img.shields.io/badge/Wayland-native-a9dcf7?style=flat-square&amp;labelColor=080c11">
  <img alt="Hyprland host" src="https://img.shields.io/badge/Hyprland-host-a9dcf7?style=flat-square&amp;labelColor=080c11">
</p>

AgentSeat lets a human and an AI Agent operate the same normal GUI application
at the same time. It creates a narrow application boundary around the Agent's
virtual input and observation—not another desktop around the user.

| Human keeps | Agent receives | Boundary guarantees |
| --- | --- | --- |
| Real pointer and keyboard | Private virtual pointer and keyboard | No host pointer movement |
| Host focus and workspace | App-scoped focus | No workspace switching |
| Normal copy/paste | Private Unicode paste | Agent paste preserves the human clipboard |

## Small by design

The runtime is deliberately small:

- one ordinary Hyprland window managed by the host's normal tiling layout;
- one reduced single-application wlroots micro-host;
- one native event-loop daemon for observation and scoped input;
- no nested Hyprland, desktop environment, VM, tmux, or per-application driver;
- no Hyprland patch or configuration change.

Native Wayland applications are the default. A private XWayland lane supports
legacy X11 software. Dialogs and child windows stay in the same private
application tree. The primary surface fills the host-assigned tile, while
dialogs keep their requested size and are constrained only when larger than the
available area. When explicitly enabled, the small collaboration head is
rendered inside that tree, not as a global desktop overlay.

> AgentSeat isolates input and focus. It is not a filesystem, process, or network
> sandbox; the wrapped program still runs as your local user.

## Development control protocol

The current source implements [control protocol v2](docs/PROTOCOL.md): bounded
nonblocking clients, write leases, bounded replay receipts, cancellable text,
absolute hold deadlines, and a compositor input gate for human takeover.
Window IDs and geometry come from the compositor; capture copies one frame.
Optional `--a11y` provides scoped controls, native semantic actions, events and
condition verification through the CLI or thin Python SDK. Input defaults to
`--ime auto`, preserving the user's toolkit modules, module order, locale and
Compose configuration. Standard Wayland text-input-v3 is relayed to the parent
compositor; selected Fcitx and IBus services retain their original endpoints.
`--ime fcitx5` explicitly uses a private profile copy; `--ime simple` disables IME integration. Automations should supply a stable, distinct
`--controller <identity>` across CLI calls. Build the controller, daemon and micro-host
together. Submission receipts require application-level observation to verify
the effect. Full vNext progress and remaining work are tracked in
[the implementation plan](docs/IMPLEMENTATION_PLAN.md).

## Control by meaning

Launch an app with `--a11y`, inspect its actual controls, then act and verify.

```sh
agentseat --instance editor run --a11y -- /usr/bin/mousepad --disable-server
agentseat --instance editor a11y query Save --exact --role button
agentseat --instance editor a11y get '<node_ref>'
agentseat --instance editor --controller writer a11y perform '<node_ref>' 0
agentseat paths
```

`paths` reports the installed `python_sdk` for reusable `find → act → wait` loops.
[The protocol](docs/PROTOCOL.md#events-conditions-and-python-client) documents the
SDK, bounded subtree queries and postconditions. GTK/Qt fixture workflows and a
real Mousepad save have been verified. Capability varies by app: unsupported
controls require visual input, and already-dispatched calls cannot be undone by
pause. Unknown outcomes are never automatically replayed.

Weixin 4.1.1.8 has been exercised on the XWayland lane with an existing account:
login, navigation, search, private Unicode paste and private Pinyin composition.
Its focus-only actions require visual activation; some embedded accessibility
subtrees remain incomplete. Use bounded control subtrees and inspect each result.
The generic bridge reads named properties individually, and retained X11 dialogs
can resize while hidden without touching a destroyed scene tree.

## Requirements

The current release targets Linux with a Hyprland host and requires:

- C17 compiler, CMake, Meson, Ninja, and GNU gettext tools;
- wlroots 0.20, Wayland, Wayland protocols, and libxkbcommon;
- json-c, libpng, GLib/GIO, GTK 4, gtk4-layer-shell, ATK and libdbus development files;
- optional `wl-clipboard` for clipboard acceptance probes; native paste has no helper dependency;
- Python 3.11 or newer for the controller;
- XWayland for `--x11` or `--ime fcitx5`;
- optional accessibility: Python GObject/Gio, D-Bus and AT-SPI2;
- IME integration: Fcitx5, the configured input engine, and a compatible toolkit module or XIM frontend.

On Arch Linux, the corresponding package names include `base-devel`, `cmake`,
`meson`, `ninja`, `wlroots0.20`, `wayland`, `wayland-protocols`, `libxkbcommon`,
`json-c`, `libpng`, `gtk4`, `gtk4-layer-shell`, and `gettext`.

## Build and install

From this directory:

```sh
./scripts/build.sh
./bin/agentseat version
```

The build stays under `build/` and places three ignored executables in `bin/`.
For a per-user installation:

```sh
./scripts/install.sh
~/.local/bin/agentseat version
```

Installed releases live below
`${XDG_DATA_HOME:-~/.local/share}/agentseat/releases`; `current` selects the
active release and `${XDG_BIN_HOME:-~/.local/bin}/agentseat` points only to
that installed copy, never to this development checkout.

The installer verifies release checksums before atomically changing `current`.
It preserves existing releases and does not restart active applications. Roll
back through the same installer with `./scripts/install.sh --rollback <digest>`;
then start a new application session to use that release.

## Use

```sh
agentseat run --host-workspace 8 -- /usr/bin/zenity --info --text='Hello'
agentseat run --private-dbus -- /usr/bin/example-single-instance-app
agentseat status
agentseat heartbeat
agentseat watch --interval 5
agentseat windows
agentseat move 0.50 0.50
agentseat click
agentseat type 'hello from the Agent'
agentseat type --keys 'text for a canvas or terminal'
agentseat scroll vertical -120
agentseat observe
agentseat wait-change --since SERIAL --timeout-ms 5000
agentseat doctor
agentseat capabilities
agentseat pause
agentseat resume
agentseat stop
```

The basic loop is `observe → act → wait-change → observe`. `observe` returns a
`content_serial`; pass it to `wait-change --since` so a change after that frame
is never missed. Caret blinks, pointer motion and the HUD do not count as
changes. `doctor` diagnoses dependencies, scope and coordinate mapping without
changing anything; `capabilities` separates what is implemented, what the
application exposes, what this session may do and what is available now.
Applications without accessibility (Tk, games, canvas toolkits) report
`SEMANTICS_UNAVAILABLE` for semantic reads; operate them visually, and use
`type --keys` where they ignore paste.

`run` keeps the original single default session for direct Agent operation.
Use `launch` when each desktop application needs its own independently managed
AgentSeat instance:

```sh
agentseat launch -- /usr/bin/mousepad
agentseat launch --x11 -- /usr/bin/example-x11-app
agentseat instances
agentseat --instance mousepad-a1b2c3d4 observe /tmp/mousepad.png
agentseat --instance mousepad-a1b2c3d4 stop
```

`launch` allocates a private runtime/state namespace and places the outer
window on the current host workspace by default. Pass `--host-workspace N` to
place it elsewhere without switching the human workspace. The identifier is
returned in `state.instance` and remains discoverable through `instances`.

`run` receives argv directly and never evaluates it through a shell. Wayland
is the default; use `run --x11 -- COMMAND ...` for the generic private-XWayland
lane. Applications must be launched through AgentSeat; an already-running host
window cannot be moved across Wayland compositor boundaries.

When `--cwd` is omitted, the application inherits the directory from which
`agentseat` was invoked. Named instances isolate AgentSeat's own runtime and
state by default. Add `--private-dbus` for applications whose single-instance
IPC uses the session D-Bus. Application configuration, accounts, and files
remain caller-owned; AgentSeat does not rewrite `HOME` or application profiles.

Route only independent user applications through `launch`. Host-session
infrastructure such as wallpaper workers, Polkit agents, tray applets, desktop
portals, input methods, and idle daemons must remain attached to the host
Hyprland session. AgentSeat provides the multi-instance launch boundary but
does not silently rewrite Hyprland bindings or desktop entries.

Each controller command opens a fresh connection to the resident daemon. A
session has a stable random identity, reported by `status` and `heartbeat`.
`watch` pins that identity and streams one JSON heartbeat per reconnect; it
fails instead of silently attaching if the session is replaced. Use
`--expect-session ID` with either command when another Agent process hands the
application off:

```sh
agentseat heartbeat --expect-session ID
agentseat watch --expect-session ID --interval 5 --count 12
```

`--count 0` (the default) watches until interrupted. Monitoring is read-only:
it does not focus, restart, or inject input into the application. The GUI and
private seat remain resident when a controller or `watch` process exits, so a
later Agent can reconnect. AgentSeat provides this persistent operating seat;
an autonomous long-running task still needs its own Agent scheduler or loop.

Pointer coordinates are normalized from `0.0` to `1.0`. Text expressible by
the current XKB keymap uses virtual keyboard events. Other Unicode text uses a
bounded clipboard source inside the private micro-host, supporting repeated
reads until revocation or ten seconds. It never reads or replaces the host
clipboard. Paste receipts confirm transport only; verify the application text
before releasing control. See `keyboard.paste` in [PROTOCOL.md](docs/PROTOCOL.md).

For text editing, `a11y text-state <node_ref>` reports the application's caret
and selection ranges. Verify these before dependent copy/edit shortcuts, then
check the resulting text or saved file. Starting an application waits for its
focused normal window; application splash screens are not forced into focus.
During an Agent-owned drag, pause, lease release, hold timeout and human takeover
cancel the data source before releasing the button. An already completed drop
cannot be undone by pausing. Physical pointer/touch drags retain their ownership.

The target workspace defaults to 1. For an inactive workspace, AgentSeat uses a
one-shot Hyprland placement rule with focus suppression. It does not switch the
human workspace. The outer window remains an ordinary window that the human
can choose to focus.

The collaboration head is hidden by default. Set `eye_hud = true` in
`~/.config/agentseat/config.toml` to make it appear automatically, or run
`agentseat hud start` for the current AgentSeat session. Observation, virtual
input, and focus isolation do not depend on the head. Dragging the head saves its
position separately for each application under `~/.config/agentseat/applications/`.
Reopening the same program restores that position, including across named instances;
resizing the window keeps the head within view. X11 dialogs and menus retain
application-requested positions; Wayland popups retain their native positioner.

Closing the desktop window sends the normal close request to the application.
The application can cancel it, exit, or remain in the background. When its last
window disappears, the desktop wrapper disappears too; the private display stays
available to a resident application, and a new application window is shown normally.
`heartbeat` reports a healthy `background` session with `window_ready: false` while
no window is open. Natural exit preserves non-GUI work the application deliberately
left running; `agentseat stop` remains an explicit shutdown of the owned session.

## Language

AgentSeat follows the system language by default and currently ships complete
English, Simplified Chinese, and Traditional Chinese interfaces. Configure a
persistent choice in `~/.config/agentseat/config.toml`:

```toml
[general]
language = "zh-CN" # auto, en, zh-CN, or zh-TW
```

Use `agentseat --language zh-TW --help` for one command, or set
`AGENTSEAT_LANGUAGE` for one process tree. The order is command-line override,
environment override, configuration, then the system locale. Unsupported
locales fall back to English. JSON keys, enum values, RPC methods, and error
codes remain stable across languages.

## Data and privacy

The source and installation directories remain read-only during normal use.
Run `agentseat paths` to show the effective XDG locations:

```text
~/.config/agentseat/config.toml   optional user configuration
~/.local/share/agentseat/         persistent application data
~/.local/state/agentseat/logs/    private logs
~/.local/state/agentseat/captures explicit observation images
~/.local/state/agentseat/apps/    named instance logs and captures
~/.cache/agentseat/               disposable cache
$XDG_RUNTIME_DIR/agentseat/       sockets, PID files and current state
$XDG_RUNTIME_DIR/agentseat-apps/  named application instance runtimes
```

Chat messages are memory-only and disappear when the daemon exits. Captures
are written only after an explicit `observe` command. Directories use mode
0700; private files use mode 0600. See [docs/DATA_LAYOUT.md](docs/DATA_LAYOUT.md).

To customize defaults without modifying the installation:

```sh
mkdir -p ~/.config/agentseat
cp config/config.toml.example ~/.config/agentseat/config.toml
agentseat config
```

## Architecture and limits

The Python command is a short-lived controller. `agentseatd` owns the control
socket and standard Wayland virtual input objects. A reduced Cage 0.3.1 owns
the private application tree and XWayland server. Observation uses standard
`wlr-screencopy` against the private output, so it does not capture or move the
host desktop.

The micro-host follows the GUI surface lifetime rather than assuming the first
launcher process is the application. A launcher may hand off to a resident GUI
process and exit without tearing down the AgentSeat session.

Hyprland owns the outer window just like any ordinary tiled client. AgentSeat
does not force floating state or resize the outer window to an application's
startup geometry. The main private application surface follows the resulting
tile; transient dialogs keep their natural size inside it.

AgentSeat reads the target Hyprland monitor scale. Applications keep the tiled
window's logical geometry, while the micro-host allocates a physical-pixel
buffer and advertises the exact standard `wp_fractional_scale_v1` preference to
native Wayland clients. It does not rewrite application `QT_*`, `GDK_*`, or
browser page zoom, and does not inject X11 DPI resources. A 1.6x internal panel
therefore renders natively instead of enlarging a pre-rendered 1x image.

The parent compositor can stop presenting an outer window on an inactive
workspace. A demand-driven fallback clock advances private client frame
callbacks only while the scene requests another frame. Idle applications do not
poll continuously, and `observe` remains a capture operation rather than the
mechanism that keeps browser rendering alive.

The daemon assigns each launch a stable session identity. Heartbeats expose
daemon uptime, request count, application-window availability, seat state, and
human-priority arbitration without changing private focus. A reconnecting
controller can pin the identity to avoid operating a newly replaced session.

The opt-in collaboration head is a private micro-host surface. Human input
inside the wrapper takes priority and temporarily pauses Agent injection. Agent
focus changes only the private application tree. Human copy/paste crosses the
parent window through the normal clipboard protocol, including text, images and
HTML. Agent clipboard offers remain private; no clipboard content is logged.

The convenience command `start onlyoffice` selects the same native Wayland
lane as `run`. The legacy private-X11 OCR paste path for sessions started
before 0.5.0 was removed in 0.6.0.

## Tests

Offline tests do not open a GUI:

```sh
pytest -q tests/test_controller.py
```

The live suites open and close real applications, so run them on an unused
workspace:

```sh
python tests/live_generic_matrix.py
python tests/live_software_matrix.py
python tests/live_persistent_session.py --duration 60 --interval 5
python tests/live_frame_clock.py
```

They verify native Wayland and private X11 input, Unicode text, child-window
leasing, observation, cleanup, host focus/pointer isolation, and repeated
reconnection to one long-running GUI session.
The generic matrix uses host workspace 8 by default; set
`AGENTSEAT_TEST_WORKSPACE=N` to choose another unused workspace.

## Community

Questions, compatibility reports, and suggestions are welcome in the
[LINUX DO community](https://linux.do/).

## License and provenance

AgentSeat is released under the MIT License. The modified Cage micro-host keeps
its upstream MIT license and provenance under `vendor/cage`. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and
[vendor/cage/UPSTREAM.md](vendor/cage/UPSTREAM.md).

Explicit `--x11` uses full physical drawable pixels while retaining the host
logical viewport size, avoiding bitmap upscaling on fractional-scale displays.
Native Wayland keeps its fractional-scale path; existing application DPI
settings remain unchanged.

The embedded head now includes an always-visible **Stop / Continue** control.
It changes the persistent Agent input pause state; temporary human priority is
shown separately. Hover alone does not revoke an Agent lease or displace its
pointer. A mouse press cancels Agent input and restores the human pointer
position. Keyboard input and touch contact retain takeover protection. Wheel
events do not seize an active Agent lease.
