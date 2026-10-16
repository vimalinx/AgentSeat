# Changelog

## 0.6.0 — 2026-10-16

Sessions started by an earlier release keep running on their own binaries;
restart a session to use 0.6.0.

### Added

- Leases, operation receipts, frame capture receipts and semantic AT-SPI
  actions in a non-blocking `agentseatd`; Python SDK
  (`python/agentseat_client.py`).
- Control protocol v4 content serial: the micro-host reports damaged output
  commits (box and area), excluding capture renders, the software cursor and
  the HUD. New read-only RPC `content.changes`.
- `wait-change` with `--since`, `--settle-ms`, `--region` and a minimum change
  extent that ignores the blinking caret; `observe` returns `content_serial`.
- `doctor [--probe-capture]` and `capabilities` (implemented, exposed by the
  application, authorized for the session, available now).
- `bench/run_baseline.py` and adopted performance budgets
  ([docs/BENCHMARKS.md](docs/BENCHMARKS.md)); evidence-backed
  [compatibility matrix](docs/COMPATIBILITY.md).

### Changed

- Long text is pasted in 256-codepoint chunks; human input stops the
  remaining chunks, and the receipt reports exactly what was transferred.
- The eye HUD rests when nothing changes and only blinks. Idle CPU fell from
  about 8 % (HUD) + 2.5 % (micro-host) to 0.8 % + 0.2 %.
- Versioned installer with integrity manifest and `--rollback`; it honours
  `AGENTSEAT_BUILD_DIR`.

### Removed

- The private-X11 OCR paste lane for OnlyOffice sessions started before 0.5.0.
