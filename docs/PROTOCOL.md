# AgentSeat control protocol v2

This is the implemented control/window/capture contract and optional scoped
accessibility, semantic actions, events and conditions. It does not claim every proposed interface in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md). The CLI reports
`protocol_version: 2`, the daemon reports `api_version: "2"`, and the internal
`agentseat_control_v1` Wayland interface is version 3. Build and install the
controller, daemon and micro-host together. Running sessions keep their existing
binaries until explicitly stopped; installing a release does not restart them.

## Transport, sessions and receipts

The private control Unix socket accepts one UTF-8 JSON object followed by LF per
connection, and responds with one JSON object followed by LF. Requests use
`jsonrpc: "2.0"`, a nonempty method string and object `params`. Check the response
`id`. Batches and notifications are not implemented.

The daemon checks the peer UID. It supports 32 clients, a 1 MiB transport request
limit, 16 KiB per-client work per turn, and absolute one-second read and write
deadlines. Incomplete input never executes at EOF or timeout. Capacity exhaustion
may close a connection without a JSON response.

Every write requires:

| Field | Meaning |
| --- | --- |
| `session_id` | Current application session, pinned by the caller |
| `controller_id` | Nonempty, NUL-free identity, at most 128 UTF-8 bytes |
| `request_id` | Positive signed 64-bit integer, strictly increasing for that controller |

Use a stable, distinct controller identity for each automation actor. With the
CLI, pass `--controller <identity>` or `AGENTSEAT_CONTROLLER_ID`. The interactive
fallback uses the parent PID and process start time. Ephemeral shells and parallel
Agents should supply an explicit identity; shell replacement is not a reconnect
identity. Generate a request sequence after any preceding acquire/renew call.

The in-memory ledger retains at most 128 receipts and 1 MiB of serialized
parameters/responses. Write parameters have a 64 KiB limit. It keeps high-water
marks for at most 128 distinct controllers for the lifetime of the session.
A repeated `(controller_id, request_id)` with the same method and parameters
returns the original receipt, without repeating its effect. Different parameters
produce `REQUEST_ID_CONFLICT`. An evicted or older sequence produces
`REQUEST_EXPIRED`, even after the full receipt is gone.

`request.get` accepts `controller_id` and `request_id` and returns the original
JSON-RPC response. A missing receipt is not evidence that an action had no effect.
An asynchronous receipt records initial acceptance; use `operation.get` to read
its subsequent outcome. Restart loses receipts and changes the session ID, so an
old write cannot run in the new session. This is bounded session deduplication,
not durable exactly-once execution across crashes.

## One writer and human takeover

`control.acquire` grants one controller a write lease. `ttl_ms` defaults to 10000
and must be 100–30000. `control.renew` requires the same controller and `lease_id`.
`control.release` revokes the lease, cancels work and releases virtual holds.
`control.status` reports current ownership. A different controller receives
`WRITE_LEASE_BUSY`.

Keyboard, pointer, window focus/move/resize and semantic requests require `lease_id` and
`control_epoch`. Emergency pause/cancellation and lifecycle requests remain
session-gated. HUD chat/activity do not acquire the input writer. The CLI acquires
before input and renews during long typing; a short-lived CLI does not implicitly
end a sequence of commands from the same controller.

The daemon owns one private Wayland gate. Acquire and virtual input use the same
connection. The micro-host checks the owner and epoch at final input delivery.
Backend human input revokes the gate immediately, increments the epoch and
releases only Agent keys/buttons. A human's matching physical hold is preserved.
This still works while the daemon is frozen or after it crashes. The quiet timer
never automatically restores permission.

Every newly pressed virtual key/button has an absolute deadline: 2000 ms by
default, configurable with integer `hold_ms` from 100 to 10000. Repeated acquisition
cannot extend an existing hold. Deadline expiry clears virtual holds and revokes
control. Writer expiry also cancels unfinished input.

`seat.pause` cancels work and requests revocation. The CLI waits for the
compositor acknowledgement before returning `compositor_acknowledged: true`.
Resume does not resume cancelled text. After human takeover, observe again and
start a new operation using the new epoch.

A low-level successful reply means `submitted: true` with
`verification: "not_requested"`. Input can race with takeover and be discarded
at the compositor; submission is not application-effect verification.

## Text and window operations

`keyboard.type` accepts UTF-8 `text` and optional integer `interval_ms` (default
4, range 0–10000). It validates all text before starting. Text is limited to
32768 UTF-8 bytes and three minutes of execution. Embedded NUL and unsupported
XKB characters are rejected before submission. It returns immediately:

```json
{
  "operation_id": "session-id:1",
  "execution": "running",
  "submitted": 0,
  "total": 250,
  "control_epoch": 1,
  "verification": "not_requested",
  "kind": "keyboard.type",
  "replay_safe": false
}
```

Read `operation.get`; cancel with `operation.cancel` and normal write metadata.
At most one text/window/semantic operation is active. Sixteen operation receipts are
retained without text content. Outcomes include `finished`, `cancelled`,
`rejected`, `deadline_exceeded` and `outcome_unknown`. A submitted window change
cancelled before confirmation has an unknown outcome. `submitted` counts
Wayland submissions, not characters proven in an application.

The CLI polls long text, renews its lease, and cancels on interruption. A lost
receipt produces `OPERATION_UNCONFIRMED`; it never resubmits automatically.
`pointer.click` submits press and release in one RPC.

`keyboard.paste` accepts 1–32768 NUL-free UTF-8 bytes. It requires the same
writer, epoch, focus lease and deduplication metadata as keyboard input. Held
virtual keys must be released first. The daemon queues a sealed memfd on its
existing private Wayland control connection. After the compositor accepts it,
it submits Ctrl+V once. `version.private_clipboard` advertises availability and
bounds; an older micro-host fails closed rather than using a helper process.

A successful operation reports `kind: "keyboard.paste"`,
`verification: "clipboard_transport_only"`, `clipboard_transfers`,
`clipboard_bytes` and `paste_keys_submitted`. These counts describe bytes written
to application receive pipes, **not inserted text**. The source remains available
for multiple reads after the first transport receipt, until replacement,
revocation or its absolute ten-second lifetime. Verify the target application's
text or rendered result **before releasing control**. A caller that immediately
releases can interrupt further reads; never use the first read as a paste
postcondition. No receiver within five seconds leaves an unknown outcome, and
neither CLI nor SDK replays the request.

Sessions launched with an input method (`fcitx5`, `desktop`, `inherit`) type by
chunked private paste so the engine cannot rewrite the text. Canvases, games and
terminals that only handle key events ignore paste; `agentseat type --keys`
sends `keyboard.type` key events instead and fails with `UNSUPPORTED_CHARACTER`
for characters the Agent keymap cannot produce, without falling back to paste.

The micro-host bounds each source at 64 read requests and 16 simultaneous
nonblocking writers. Closed/broken recipients cannot block or terminate its
input loop. Human takeover, pause, writer expiry and daemon disconnect clear
Agent-owned selection and pending writers. A later application-owned selection
is preserved. Clipboard publication itself is behind the compositor gate;
there is no host clipboard access or per-paste `wl-copy` subprocess. The CLI's
Unicode fallback and `Session.paste(text)` use this native operation. A successful
Unicode CLI result intentionally has no `typed` count.


`window.list` reads the compositor registry without focus or lease writes.
Mapped top-level windows, dialogs and XDG popups have non-reused session IDs
`w-N`, actual parent/focus information, logical geometry and scene version.
Wayland peer PIDs are kernel-verified; XWayland's advertised PID is explicitly
`pid_verified: false`. X11 override-redirect surfaces are marked as popups.
The registry is bounded at 256 entries; heartbeat output reports truncation.

`window.focus`, `window.move` (`x`, `y`) and `window.resize` (`width`, `height`)
produce asynchronous operations. `finished` with
`verification: "compositor_confirmed"` requires the compositor to report the
actual requested focus/geometry. Primary window geometry belongs to the host;
primary move/resize is `UNSUPPORTED`. A fixed-size application may decline the
compositor's viewport-sized configure, so its actual window size can be smaller
than the output; never infer one from the other. Popup placement/grabs belong to the
application. `window.geometry` is `UNSUPPORTED`: callers cannot replace observed
geometry with guessed metadata. The legacy `agentseat:root` alias resolves to
the current primary window; new callers should use actual IDs.

`window.close` (CLI `agentseat window close w-N`) requests that one mapped
application toplevel close, under the same session, window lease, write lease,
epoch and pause checks as other writes. Its completed operation reports
`verification: "close_request_delivered"`: the client received a close request,
but may still show a save/cancel dialog or keep the window open. Re-observe the
window registry and application before claiming it closed. Do not automatically
replay a close request. Popup and HUD targets are not accepted.

The Wayland parent window is owned by AgentSeat. An ordinary host close request
(including a compositor close shortcut) is forwarded to the currently focused
internal application toplevel instead of destroying the private display server.
It revokes Agent input before delivery and also works while Agent control is
paused. If the HUD has focus, the last focused mapped application identity is
used; a stale or missing identity never selects an arbitrary replacement.
After a focused child unmaps, focus returns to its mapped parent or a remaining
application window. Application save/cancel behavior is preserved. Closing the
last application normally still ends the session; `agentseat stop` explicitly
ends the entire wrapper. Forced process termination cannot be forwarded.

Window exclusion uses `window.release`; `window.lease` restores it. New mapped
windows in the private application output are included automatically. Releasing
a currently controlled window cancels its input. Explicit exclusions remain in
effect across scene refreshes.

## Coordinates and one-frame capture

Pointer coordinates are normalized to the entire private output, not a secondary
window. Window coordinates are `output-logical`. Heartbeat `output` reports
logical and physical buffer sizes, scale, `geometry_version` and truncation.
The scene version also changes on focus/title updates, so it is a conservative
precondition, not only a size counter.

An optional positive integer `geometry_version` on input/window writes rejects
stale frame coordinates with `STALE_GEOMETRY`. The compositor checks it again at
final acquisition; an intervening scene change revokes delivery. Use a new frame
and a fresh request rather than replaying an old coordinate action.

`capture.submit` returns a `frame_id` and `pending` state. Poll `capture.get` with
that ID until `finished` or `failed`; `encoding` is also a pending state. The
receipt includes `session_id`, physical dimensions, geometry version at
submission, submission time and the compositor's frame timestamp. Scene changes
after submission can invalidate that geometry version. A ready frame is not
proof that an application completed a previously requested operation.

Capture registers one screencopy on the daemon's existing Wayland connection
before requesting an offscreen render. There are no repeated `grim` processes
or fixed settle sleeps. PNG encoding runs outside the control event loop and
signals completion through an eventfd. A frame has a three-second capture
deadline and a 64 MiB pixel budget. Only one capture/encoding operation runs at a
time. Up to eight private runtime PNGs/receipts are retained; old ones are removed.
The CLI atomically copies the completed PNG to its requested, validated output
path. Explicit saved captures remain user-managed.

Capture covers the complete single-application output. It refuses an incomplete
registry or an output containing an explicitly excluded window. This is not a
per-window crop endpoint.

### Content changes and visual waits

Microhost control protocol v4 reports a `content` serial. It advances only on an
output commit that carried scene damage. Capture renders, frame-clock commits
without damage, software-cursor motion and the AgentSeat HUD do not advance it.
Heartbeat `output` adds `content_serial` and `content_age_ms` (both `null` with
an older microhost), and every frame receipt carries the `content_serial` at
submission.

`content.changes` is read-only. It takes `after_serial` and returns the current
`content_serial`, the retained newer changes oldest first, `output`, and `gap`.
Each change has `serial`, a damage box (`x`, `y`, `width`, `height` in output
buffer pixels), `area` (sum of damage rectangles) and `age_ms`. The daemon keeps
the last 256 changes; `gap: true` means newer changes were evicted or missing,
so treat the output as changed.

`agentseat wait-change` polls this bounded history. By default it waits from the
current serial; pass `--since` with the `content_serial` returned by `observe` so
a change between that frame and the wait is never missed. A change qualifies
when its damage box is at least `--min-extent` logical pixels wide and tall
(default 5, wider than a caret blink), covers `--min-area-ratio` of the output
(default 0) and intersects `--region X Y WIDTH HEIGHT` (normalized); others are
counted as ignored. `--settle-ms`
then waits until qualifying changes stop for that long. The result `status` is
`changed`, `settled`, `changed_unsettled` or `timeout`; `--timeout-ms` is at most
60000. A change proves only damage, not task completion: observe the content.

### Doctor and capabilities

`agentseat doctor` only reads. It reports host and dependency checks, session
identity, controller/daemon versions, application scope, coordinate mapping,
visual-wait support, Agent input state, the text lane and accessibility. With
`--probe-capture` it times one private frame and compares it with the output
pixels. It prints IM module names but no other environment values or bus
addresses. `status` is `ok`, `degraded` or `failing`.

`agentseat capabilities` lists each capability with `implemented`,
`application_exposes`, `session_authorized` and `available`, plus a `reason`.
Implemented AT-SPI support does not mean the application exposes controls;
exposed controls still list their own actions per node in `a11y get`.

## Optional scoped accessibility

Launch with `run --a11y -- <application>` or `launch --a11y -- <application>`.
The supervised bridge starts an owned private session bus, accessibility bus and
Registry. It verifies bus ownership, unique peer names, PID/descendant chains and
process start times. Unrelated peers are not traversed, including a same-named
process on the private bus. Detached/reparented apps are not silently adopted.
GTK/Qt accessibility is enabled only inside the launched application environment.
An app without a usable accessibility tree remains available through visual input.

The bridge owns reads and action preparation. It never performs semantic writes.
Its `a11y.sock` accepts bounded JSON/LF requests carrying `scope_id`; CLI and SDK
verify bridge ancestry and socket peer identity. Bridge protocol version is 3.
`a11y.status` reports actual scope identity and implemented read/write/wait/event
methods. `native_writes` is the runtime's method list, not proof that an app or
individual node implements those methods. `--expect-scope` pins a CLI read scope;
the SDK pins it automatically. Node references also contain their scope.

```sh
agentseat --instance example a11y status
agentseat --instance example a11y snapshot --limit 64
agentseat --instance example a11y query 'Save' --exact --role button --interface Action
agentseat --instance example a11y get '<node_ref>'
agentseat --instance example a11y snapshot --root-ref '<node_ref>'
agentseat --instance example a11y snapshot --snapshot-id '<id>' --offset 64
agentseat --instance example a11y text '<node_ref>' --start 0 --count 1024
agentseat --instance example a11y text-state '<node_ref>'
```

When no accessible root inside the application scope is registered (Tk, raw
X11, games and other canvas toolkits), `snapshot`, `query` and selector-based
`wait` fail with `SEMANTICS_UNAVAILABLE` instead of returning an empty tree. An
empty result would read like an empty application and would satisfy an
`absent` condition. Operate such applications visually; `doctor` reports the
case as a warning and `capabilities` marks semantic entries as not exposed by
the application.

AT-SPI null object references (`/org/a11y/atspi/null`) are skipped and counted
in the snapshot's `null_children`; they are not read errors. Application nodes
carry `toolkit` (`Application.ToolkitName`). Chromium and Electron expose only
their frame until renderer accessibility is on: launch them with
`--force-renderer-accessibility`. `doctor` warns and `capabilities` adds a note
when a Chromium toolkit reports a null child.

`text_state` (SDK `text_state(ref)`) reads the application's caret offset,
character count and up to 16 selection ranges. Password/protected nodes reject
this read before fetching offsets. It does not fetch text or modify selection.
Offsets use the application's Text indexing, not byte positions. Reads are
sequential (`atomic: false`); changed counts/caret or inconsistent ranges return
`TEXT_STATE_CHANGED`. Unsupported toolkit methods remain explicit errors.
Verify selection/caret and resulting text around dependent shortcuts: delivery
of a key does not prove the application has processed it. Re-observe after a
state change, and never repeat an unknown paste to compensate for missing text.

Pointer drag ownership follows the first effective button press of the implicit
grab. Revocation cancels an Agent drag's data source before orphaned button
releases, so cancellation is not translated into a drop. Physical and touch
drags are not cancelled by Agent control revocation. This cannot reverse a drop
that was already delivered, or navigation the application performed while
hovering. Re-observe the current folder/window before starting the next drag.

Reads use a 1.5-second traversal budget, 250 ms per D-Bus call, depth 16 and 512
nodes by default. `max_nodes` (1–2048) and `max_depth` (0–32) explicitly narrow or
expand traversal while retaining the time limit. Pages contain 1–128 nodes.
Four snapshots and 4096 independent references
are retained for at most 30 seconds. At most 256 unique application peers can be
registered during one bridge lifetime. Limits return partial results or explicit
budget errors. Query results search a bounded snapshot: `truncated: true` cannot
establish uniqueness or absence. Use `root_ref` to narrow a large application's
tree. Exact matching uses the actual name; role selectors accept the toolkit role
name or canonical `role_key`. Interface selectors accept a full name or suffix.
The Python `find` helper refuses incomplete, missing and ambiguous matches. It
accepts `states` and `max_nodes`; use `states=['showing']` for displayed controls.
Removal invalidates references to the affected subtree. Unrelated references
survive only when the observed ancestry proves they are outside that subtree;
unknown ancestry remains conservative. Parent metadata is capped at 8192 nodes.

Nodes include actual/canonical role, name, description, interfaces, states,
parent reference, child count and observation time. `get` reads actual Action
names/indices, Component bounds and numeric Value properties on demand. Bounds
are tied to a native window by verified PID, unique title and matching ancestor
extents. They carry `window_id` and `geometry_version`; a geometry change during
mapping fails closed. A semantic web dialog without its own native surface does
not prevent matching its real ancestor window.

A toolkit can use logical coordinates for its frame and physical pixels for an
embedded document. The bridge compares the document's complete viewport extent
with its parent and the compositor scale. A matching uniform conversion returns
`mapping: "compositor_scaled"`, `coordinate_scale`, `unit_inferred: true`,
`scale_evidence` and `requires_visual_confirmation: true`, alongside unchanged
`raw` coordinates. This inference is explicitly distinct from an unscaled
`compositor_verified` mapping. Confirm the candidate against a current frame and
the application's result before relying on it. Inconsistent or ambiguous units
remain `bounds_unmapped`; unavailable bounds are `bounds_unavailable`. Never
interpret raw AT-SPI numbers as output coordinates just because the frame size
matches the compositor.

`text` explicitly reads at most 4096 characters and 32768 UTF-8 bytes, returning
the range, character count and truncation. Snapshots do not read Text contents.
Password roles suppress Name/Description reads and reject text/value mutation or
content reads with `PROTECTED_CONTENT`. Labels elsewhere are application content;
`text_interface_read: false` does not mean that no visible words were read.

References revalidate the peer/PID/start time. Removed/defunct nodes invalidate
references, including a path reused for a new object. A removed subtree invalidates its descendants and any references whose
ancestry cannot be proven; unrelated references with complete ancestry survive.
Re-query after structural changes; do not replay a write just because its later verification used a stale ref.

## Native semantic writes

All six methods go through `control.sock` and the same session/controller/request
ledger, single-writer lease and control epoch as keyboard and pointer operations:

| Method | Additional parameters | Standard AT-SPI method |
| --- | --- | --- |
| `a11y.perform` | `node_ref`, actual `action` name or integer index | `Action.DoAction` |
| `a11y.set_text` | `node_ref`, UTF-8 `text` up to 32768 bytes | `EditableText.SetTextContents` |
| `a11y.set_value` | `node_ref`, finite in-range `value` | `Properties.Set(Value.CurrentValue)` |
| `a11y.focus` | `node_ref` | `Component.GrabFocus` |
| `a11y.select` | `node_ref`, zero-based child `index` | `Selection.SelectChild` |
| `a11y.scroll_to` | `node_ref` | `Component.ScrollTo(ANYWHERE)` |

The bridge checks node identity, protection, sensitivity and the actual required
interface/action/range. The native daemon alone can request a prepared target;
public bridge writes return `NATIVE_CONTROL_REQUIRED`. The daemon independently
verifies the private bus and target process and performs asynchronous GDBus calls.
It does not spawn a process for each action. Slow reads/connections/replies do not
block its control loop. GIO state is created only on the first semantic write.

All current windows must be included in the scope; an excluded window or
truncated compositor registry gives `SEMANTIC_SCOPE_INCOMPLETE`. The final daemon
check includes a compositor authority acknowledgement before dispatch. Pending
preparations are cancelled on takeover, pause, lease expiry or scope changes.
Unlike Wayland input, an AT-SPI call already queued to the application cannot be
recalled atomically by the compositor. A later pause/timeout does not undo it.

For `--a11y` applications, the release-local `libagentseat-atk-dispatch.so` is
preloaded into the application process only. It defers ATK actions invoked from
inside libdbus dispatch to the GUI loop after dispatch returns. This prevents
GTK 3 modal loops and accessibility key notifications from re-entering libdbus's
non-reentrant dispatch lock. Direct application ATK calls keep their synchronous
behavior; non-ATK actions keep their toolkit behavior. The queue is bounded at
64 retained targets. Status discloses `atk_dispatch: deferred-outside-dbus`.
The private bus, registry, IME and host desktop do not receive this preload.

Each action returns an operation ID. Stages distinguish preparation, identity
validation, compositor acknowledgement, in-flight execution and completion.
`submitted` is 0 or 1; `application_replied`, `method_succeeded`,
`cancel_requested`, `may_have_side_effects` and `error_kind` expose what is known.
`finished` means the method returned success, with `verification: not_requested`.
In particular, the ATK adaptor acknowledges `DoAction` before executing its
callback: `method_succeeded` proves acceptance, not the callback's effect. Check
dialog state, application text or another task-specific postcondition.
A standard dispatch refusal or false return is `rejected`. A lost/late response
or cancellation after dispatch is `outcome_unknown`. A late successful reply may
update the reply fields, but never changes a cancelled operation into success.
Do not retry it or automatically click coordinates afterward.

Preparation has a three-second deadline; optional `timeout_ms` is 100–10000
(default 5000) for the dispatched method. One semantic/text/window operation can
run at a time, with 16 retained receipts. A cancelled in-flight semantic call
keeps its bounded reply slot; new semantic calls get `SEMANTIC_BUSY` until it ends.
A declared Component interface can still reject focus/scroll, and an Action
interface can expose zero actions. Treat these as application capability results.
`select` changes selection, which need not change a widget's current keyboard item.

```sh
agentseat --instance example --controller worker a11y perform '<ref>' 0
agentseat --instance example --controller worker a11y set-text '<ref>' 'Draft 中文'
agentseat --instance example --controller worker a11y set-value '<ref>' 42
```

## Events, conditions and Python client

`events.subscribe` registers scoped peers and returns a starting sequence.
`events.read` accepts `after` and `limit` (1–128). A 256-entry metadata ring reports
`event_seq`, `next_seq`, `gap` and `code: EVENT_GAP` after overflow. It does not
retain event text/value payloads. Gaps require a fresh read; an event is never
proof of a requested result. Idle operation has no full-tree polling.

`wait.start` takes `condition` and `timeout_ms` (0–30000), returning a `wait_id`.
Read with `wait.get`, cancel with `wait.cancel`. There are 16 active/32 retained
waits. Conditions support `exists`, `absent`, `state`, `text` and `value`, selected
by `node_ref` or `selector` (`name`, `exact`, `role`, `states`, `interface`).
Text uses `equals` or `contains`; value uses finite `equals`; state uses `state`
and optional boolean `is`. Existence/absence require a complete bounded query.
Use a selector for disappearance; stale references are errors, not absence proof.

Registration precedes the initial check. Events trigger a fresh condition read,
coalesced at 100 ms, with a 500 ms fallback for omitted toolkit events. Each read
has a 300 ms budget; at most one wait is evaluated per bridge turn. Scheduling
and an already-running bounded read can delay the timeout response. Receipt
statuses are `pending`, `satisfied`, `deadline_exceeded`, `cancelled` or `failed`,
with check count, event-gap flag and proof metadata. No event content is taken as
a condition value without re-reading the node.

```sh
agentseat --instance example a11y subscribe
agentseat --instance example a11y events --after 0
agentseat --instance example a11y wait --timeout-ms 3000 \
  --condition '{"kind":"exists","selector":{"name":"Save document","exact":true,"role":"button"}}'
```

`--condition` can be attached to a CLI action for a separate postcondition check.
The dependency-free Python client is installed at the `python_sdk` path reported
by `agentseat paths`. Add its containing directory to `sys.path` or `PYTHONPATH`:

```python
from agentseat_client import Session

app = Session.from_instance("example", controller="document-worker")
try:
    ref = app.find("Document title", interface="EditableText")
    app.set_text(ref, "Draft 中文", expect={"kind": "text", "node_ref": ref,
                                          "equals": "Draft 中文"})
    button = app.find("Save", role="button", interface="Action")
    app.activate(button)
finally:
    app.release()
```

An active SDK client renews its existing lease before subsequent actions when
two seconds have elapsed, and while polling long operations. This avoids expiry
in a multi-step task. It never silently reacquires an expired or revoked lease.
For an intentional pause longer than the lease TTL, release it and explicitly
acquire after re-observing the application. Read-only calls do not renew leases;
call `renew()` explicitly when retaining control during a long read phase.

SDK `activate` selects a single exported Action whose nonlocalized name is
`click`, `press`, `activate`, or `toggle` (case insensitive). Checkable roles
prefer their explicit `toggle` action. Other multiple matches raise `ACTIVATION_AMBIGUOUS`; callers can use `perform` with an explicit index.
Focus-only and unknown actions are not activation. Without a recognized Action,
the caller must supply a visually inspected point and a frame from this session.
The coordinate path uses the frame's geometry guard. No fallback occurs after
an attempted semantic call. `act(..., expect=...)` raises if the requested
postcondition is not satisfied. `WRITE_UNCONFIRMED` carries the request identity
for `request_receipt`; the SDK never retries writes after transport errors.

## Human input method and clipboard

`run` and `launch` default to `--ime auto`. Input settings are captured before
Hyprland launches the wrapper, so it does not substitute its older environment.
Toolkit modules, Qt module order, locale, Compose and XKB defaults retain their
original values. An unset value stays unset. The private launch file is consumed
and deleted; credentials and unrelated environment variables are not copied.
A running input daemon alone is not evidence of the user's preferred engine.

The parent `wl_keyboard` keymap and repeat information are imported explicitly,
including subsequent changes. Each input source has its own keyboard group;
changing the human layout cannot overwrite the Agent keyboard. Parent modifier
and layout-group events use the normal nested backend. Host keys are never
synthesized by this adapter.

When the parent advertises `zwp_text_input_manager_v3`, the wrapper advertises
and relays v3 to it. Enable/disable, surrounding text, change cause, content
purpose/hints and cursor rectangles travel outward. Preedit, UTF-8 byte cursor
offsets, commit and surrounding-text deletion travel inward as one `done`
batch. Parent commit serials are checked against the current inner input serial;
stale batches and unfocused contexts are discarded. Cursor rectangles include
the inner scene position and output-to-parent scale. No dead v3 global is
advertised when the parent has no v3 support. This does not implement v1 or v2.

When the user's configuration selects Fcitx, auto attaches the existing Fcitx5
service to the private X display through `OpenX11Connection`. Toolkit choices
remain unchanged, including mixed IBus/Fcitx configurations. The same process
owns the scheme, shortcuts, dictionary and learning. The optional accessibility
bus remains private. AgentSeat does not restart or reconfigure the desktop
service. Candidate windows on this Fcitx lane belong to the private X display.

IBus discovery runs before the display changes. GTK's `IBUS_ADDRESS` and Qt's
`IBUS_ADDRESS_FILE` retain the original service; `IBUS_USE_PORTAL` is preserved.
The application retains its original session bus even when the accessibility
supervisor owns a separate bus. `--ime inherit` selects this unmodified toolkit
configuration explicitly. The IBus transport qualification uses a disposable
real daemon and a deterministic test engine, not a user's dictionary or panel.

`--ime fcitx5` starts a separate service from a launch-time copy of the user's
Fcitx configuration and data. Changes and learning stay in that private copy.
If no user profile exists, this explicit mode initializes Pinyin. Auto with an
explicit `--private-dbus` uses the private copy when Fcitx is selected and ready.
`--ime simple` explicitly disables integration. Fcitx's X11 frontend requires
XWayland and a compatible toolkit module or XIM client.

With inherited or Fcitx input modes, CLI `type` uses private Unicode paste. Raw keyboard RPCs
still send keys and can interact with composition. Write leases and the final
input gate apply to Agent operations. The Agent policy `allow_host_clipboard`
remains false: there is no RPC to read or replace the desktop clipboard.

The human-facing window uses ordinary `wl_data_device` to bridge copy/paste to
its parent compositor. Parent offers are read lazily only while this window has
keyboard focus and physical human activity is inside the configured grace
period. Application selections are exported only after such a human gesture.
Agent Unicode offers and imported offers are never exported. Human exports are
cached asynchronously so a later Agent paste does not invalidate the copy.
Text, images, HTML and file-list MIME payloads use the same transport. Each copy
is bounded to 64 MIME entries, 64 MiB aggregate and 32 concurrent readers, with
ten-second capture/transfer deadlines. The bridge does not implement PRIMARY
selection or drag and drop, and does not read parent data in idle/background
Agent operation. Clipboard contents are never written to logs or receipts.

## Errors and verification boundary

Errors use integer `code`, readable `message` and stable `data.kind` on the native
socket. Request/method/parameter failures use -32600/-32601/-32602; domain failures
use -32000. The semantic read/preparation bridge returns an `error.kind` string. Never automatically
repeat an unknown side effect after a transport/display failure.

Session IDs, controller IDs and private sockets prevent accidental scope mistakes;
they are not authentication against malicious same-UID applications. Human and
Agent share the actual application's document state and focus. Condition verification establishes only the requested observable state. Automatic
application adapters and general task/model planning remain external work.

### X11 physical pixel coordinates

Explicit `run --x11` uses the full physical output grid for the private X11
server and scene. At host scale 1.6, a 1592×960 outer viewport therefore gives
the application a 2548×1536 drawable with private output scale 1. Native
Wayland retains fractional output scale 1.6. Existing application DPI settings
are preserved; this mode does not inject toolkit zoom variables.

`state.viewport_logical_size` describes the outer host viewport;
`state.application_coordinate_size` and `session.heartbeat.output.width/height`
describe the private scene used by window bounds and input. Capture uses
`pixel_width/pixel_height`. Normalized Agent input remains relative to the
private scene. Backend pointer correction uses the parent viewport scale,
independently of private output scale.

Validation: `tests/live_x11_pixels.py` checks the actual X11 drawable and
captured single-pixel stripes, plus three application-reported click positions.
`tests/live_x11_backend_pixels.py` uses a private parent compositor to verify
backend pointer positions at 1.6x without injecting host input.

### Explicit stop and click-only pointer takeover

`collaboration.status.seat_paused` distinguishes an explicit `seat.pause` from
the temporary human quiet interval. The embedded HUD reads this flag for its
Stop / Continue control and calls `seat.pause` / `seat.resume` directly.

Backend hover and motion do not revoke control. While the compositor input
gate is granted, absolute backend coordinates are recorded separately and
are not forwarded to the app. A backend button press revokes Agent input,
restores those coordinates, flushes a pointer frame (required by XWayland),
and then forwards the press. Keyboard and touch takeover remain protected;
wheel input does not seize an active Agent gate.

`tests/live_hud_control.py` validates hover isolation, the actual click
coordinates on takeover, Stop blocking acquisition, Continue restoring real
application input, and the expanded HUD. It uses two private compositors and
does not inject input into the host desktop.

When XWayland regains pointer focus from the native HUD, the compositor emits
a motion at the actual pointer position after `enter`. This avoids the
duplicate-motion cache suppressing the update needed for X11 button coordinates.
Continue allows a new Agent operation; it does not replay previously cancelled
actions. The Agent must reacquire control and focus its leased app after an
explicit stop, as required by the existing control protocol.
