<p align="center">
  <img src="assets/brand/agentseat-hero.svg" alt="AgentSeat — one app, two operators" width="100%">
</p>

<h1 align="center">AgentSeat</h1>

<p align="center">
  <strong>A private seat for an AI Agent inside one local application.</strong><br>
  The Agent gets its own pointer and keyboard. You keep yours.
</p>

<p align="center">
  <a href="https://github.com/vimalinx/AgentSeat/releases/latest"><img alt="GitHub release" src="https://img.shields.io/github/v/release/vimalinx/AgentSeat?style=flat-square&amp;color=57b8ed"></a>
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-080c11?style=flat-square"></a>
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
| Host clipboard | Private one-shot Unicode paste | No host clipboard replacement |

## Small by design

The runtime is deliberately small:

- one ordinary Hyprland window on the host;
- one reduced single-application wlroots micro-host;
- one native event-loop daemon for observation and scoped input;
- no nested Hyprland, desktop environment, VM, tmux, or per-application driver;
- no Hyprland patch or configuration change.

Native Wayland applications are the default. A private XWayland lane supports
legacy X11 software. Dialogs and child windows stay in the same private
application tree. When explicitly enabled, the small collaboration head is
rendered inside that tree, not as a global desktop overlay.

> AgentSeat isolates input and focus. It is not a filesystem, process, or network
> sandbox; the wrapped program still runs as your local user.

## Requirements

The current release targets Linux with a Hyprland host and requires:

- C17 compiler, CMake, Meson, Ninja, and GNU gettext tools;
- wlroots 0.20, Wayland, Wayland protocols, and libxkbcommon;
- json-c, GTK 4, and gtk4-layer-shell;
- `grim` and `wl-clipboard` at runtime;
- Python 3.11 or newer for the controller;
- XWayland only when using `--x11`.

On Arch Linux, the corresponding package names include `base-devel`, `cmake`,
`meson`, `ninja`, `wlroots0.20`, `wayland`, `wayland-protocols`, `libxkbcommon`,
`json-c`, `gtk4`, `gtk4-layer-shell`, `gettext`, `grim`, and `wl-clipboard`.

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

Set `PREFIX` to install elsewhere. The default installation is
`~/.local/lib/agentseat` with a command symlink in `~/.local/bin`.

## Use

```sh
agentseat run --host-workspace 8 -- /usr/bin/zenity --info --text='Hello'
agentseat status
agentseat windows
agentseat move 0.50 0.50
agentseat click
agentseat type 'hello from the Agent'
agentseat scroll vertical -120
agentseat observe
agentseat pause
agentseat resume
agentseat stop
```

`run` receives argv directly and never evaluates it through a shell. Wayland
is the default; use `run --x11 -- COMMAND ...` for the generic private-XWayland
lane. Applications must be launched through AgentSeat; an already-running host
window cannot be moved across Wayland compositor boundaries.

Pointer coordinates are normalized from `0.0` to `1.0`. Text expressible by
the current XKB keymap uses virtual keyboard events. Other Unicode text uses a
one-shot clipboard offer inside the private micro-host; it never reads or
replaces the host clipboard.

The target workspace defaults to 1. For an inactive workspace, AgentSeat uses a
one-shot Hyprland placement rule with focus suppression. It does not switch the
human workspace. The outer window remains an ordinary window that the human
can choose to focus.

The collaboration head is hidden by default. Set `eye_hud = true` in
`~/.config/agentseat/config.toml` to make it appear automatically, or run
`agentseat hud start` for the current AgentSeat session. Observation, virtual
input, and focus isolation do not depend on the head.

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
~/.cache/agentseat/               disposable cache
$XDG_RUNTIME_DIR/agentseat/       sockets, PID files and current state
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

The opt-in collaboration head is a private micro-host surface. Human input
inside the wrapper takes priority and temporarily pauses Agent injection. Agent
focus changes only the private application tree.

The convenience command `start onlyoffice` selects the same native Wayland
lane as `run`. A legacy private-X11 paste path remains only for old runtime-state
compatibility; it is not part of the generic core contract.

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
```

They verify native Wayland and private X11 input, Unicode text, child-window
leasing, observation, cleanup, and host focus/pointer isolation.
The generic matrix uses host workspace 8 by default; set
`AGENTSEAT_TEST_WORKSPACE=N` to choose another unused workspace.

## License and provenance

AgentSeat is released under the MIT License. The modified Cage micro-host keeps
its upstream MIT license and provenance under `vendor/cage`. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and
[vendor/cage/UPSTREAM.md](vendor/cage/UPSTREAM.md).
