# AgentSeat benchmarks

`bench/run_baseline.py` measures one reproducible baseline against the real GTK
control fixture. It launches a private instance on host workspace 20 (no focus
change), runs the measurements, writes `baseline.json` into `--report-dir` and
stops the instance. It needs a live Hyprland session; nothing is mocked.

```sh
python3 bench/run_baseline.py --report-dir /path/to/evidence/bench-run
python3 bench/run_baseline.py --cli ~/.local/bin/agentseat --report-dir ...  # installed release
```

## Method (version 1)

| Metric | What is timed |
|---|---|
| `launch_ms` | `agentseat run` until it returns a ready session |
| `heartbeat_cli` | one `agentseat heartbeat` process, end to end |
| `observe_cli` | one `agentseat observe` process, end to end, PNG copied |
| `capture_submit_to_present` | frame receipt `presented_ns - submitted_ns` inside the daemon |
| `type_5_chars_to_app` | `agentseat type bench` until the fixture reports the text |
| `type_2048_chars_to_app_ms` | 2048 characters until all are in the fixture |
| `wait_change_after_type` | `wait-change` elapsed time after a one-character type, minus the 400 ms head start |
| `idle` | microhost and daemon CPU over 10 s after the caret stops blinking, and RSS |

Samples default to 20 (10 for `wait_change_after_type`). Compare runs only with
the same `method_version`, text lane, output size, scale and HUD setting.

## Baseline 2026-10-16

Source `f54ba9f`, Arch Linux 7.2.9, Hyprland, GTK 4 fixture, output
1592×960 logical / 2548×1536 pixels at scale 1.6, text lane `desktop`
(chunked private paste), eye HUD enabled by configuration.

| Metric | p50 | p95 |
|---|---|---|
| `launch_ms` | 2269 | – |
| `heartbeat_cli` | 274 ms | 282 ms |
| `observe_cli` | 366 ms | 388 ms |
| `capture_submit_to_present` | 3.4 ms | 22 ms |
| `type_5_chars_to_app` | 306 ms | 316 ms |
| `type_2048_chars_to_app_ms` | 1031 | – |
| `wait_change_after_type` | 57 ms | 76 ms |

Idle: microhost 2.6 % CPU, 74 MiB RSS; daemon 0.1 % CPU, 7.4 MiB RSS. With the
HUD stopped the idle microhost measured 0.0 % CPU, so the HUD animation is the
whole idle cost.

### Idle HUD after `19dc92d`

The head now rests when nothing changes and only blinks. Same fixture and
method, HUD enabled: microhost 0.3 % (benchmark) / 0.2 % (separate 10 s
sample), daemon 0.1 %. The HUD process itself, which method v1 does not
sample, fell from 8.3 % to 0.8 %; the remainder is its 300 ms state poll.

### Reading the baseline

- Each CLI call costs a fixed process start-up (Python plus the host geometry
  query) before any work; the daemon-side capture itself takes a few
  milliseconds. Agents that call repeatedly should use the Python SDK
  (`agentseat paths` → `python_sdk`) and avoid one process per action.
- The start-up cost depends on the host's state. Baseline runs at 20:32 and
  21:12 measured `heartbeat_cli` near 270 ms; at 21:26 the same 0.6.1 release
  measured 110 ms. The cause of the earlier, slower state was not identified,
  so compare CLI latencies only between runs taken back to back.
- The idle cost came from the optional HUD animation, not the runtime core,
  and was removed in 0.6.0.

### Controller split (0.6.2)

Back to back on one live session: `heartbeat` 110 ms with 0.6.1 (single
script) and 87 ms with 0.6.2 (package with cached bytecode). Benchmark of the
installed 0.6.2: `heartbeat_cli` p50 90 ms, `observe_cli` p95 125 ms, capture
p95 5.7 ms, wait-change p95 52 ms, idle micro-host 0.0 %, daemon 0.1 %.

## Budgets

Adopted for 0.6.0. A release must meet them on the method-v1 fixture; a miss
blocks the release until it is fixed or the budget is changed here with a
reason.

| Metric | Budget |
|---|---|
| `capture_submit_to_present` p95 | ≤ 50 ms |
| `wait_change_after_type` p95 | ≤ 150 ms |
| `observe_cli` p95 | ≤ 500 ms |
| idle microhost CPU, HUD on | ≤ 0.5 % |
| idle daemon CPU | ≤ 0.5 % |
