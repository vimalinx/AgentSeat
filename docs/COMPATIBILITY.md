# AgentSeat application compatibility

This matrix records only what a real run verified. Each row names the release or
source revision, the date and the local evidence directory (under the
workspace project's `.ai/`, not shipped in this repository). A row proves the
listed paths only; it does not certify every feature of that application.

Legend: **V** verified end to end with an application-side check (file, AT-SPI
read or fixture state); **O** verified by OCR or visual inspection only;
**P** partial, see notes; **F** failed and not yet fixed.

## Verified applications

| Application | Toolkit / lane | Verified paths | Result | Release, date | Evidence |
|---|---|---|---|---|---|
| GTK 4 control fixture | GTK 4, Wayland | Typing, chunked paste, takeover, write lease, window ops, capture, wait-change, doctor | V | source `f54ba9f`, 2026-10-16 | `20261016-review-followups/` |
| GTK / Qt 6 semantic fixtures | GTK, Qt, Wayland | AT-SPI reads, six semantic actions, events, condition waits, Chinese edit and save; 180 s two-session soak (149 rounds each, 28 GUI saves) | V | `249542f5071c`, 2026-09-06 | `20260906-semantic-actions/`, `computer-use-alignment-20260906/` |
| Mousepad | GTK, Wayland | Menus, save, cancel, overwrite confirmation (15 steps); real file saved | V | `249542f5071c`, 2026-09-06 | `computer-use-alignment-20260906/` |
| Thunar + Mousepad | GTK, Wayland | Create, rename, move; three drag-cancel paths; open, edit, save | V | `249542f5071c`, 2026-09-06 | `computer-use-alignment-20260906/` |
| Chromium | Chromium, Wayland | Form, attachment, native file chooser, scrolling, download; clicks before and after page zoom | V | `249542f5071c`, 2026-09-06 | `computer-use-alignment-20260906/` |
| LibreOffice Writer | VCL, Wayland | Editing, selection, copy, find and replace, undo/redo, ODT save checked from the file | V | `249542f5071c`, 2026-09-06 | `computer-use-alignment-20260906/` |
| ONLYOFFICE Desktop Editors | Chromium/CEF, XWayland (requested Wayland) | `start`-equivalent launch with isolated profile, Ctrl+A, chunked private paste, host focus unchanged | O | source `f54ba9f`, 2026-10-16 | `20261016-review-followups/onlyoffice-generic/` |
| WeChat 4.1.1.8 | Proprietary, XWayland, scale 1.6 | Login, navigation, search, private Unicode paste, private and desktop Fcitx pinyin, 168-node login tree read, close routing, background lifetime | P | `09d993f2d096`–`39e365050d45`, 2026-09-06/07 | `wechat-*-20260906/`, `wechat-runtime-update-20260907/` |
| GTK 4 / GTK 3 / Qt 6 with IBus | Wayland and XWayland | Preedit and multilingual commit through a real IBus daemon | V | 2026-09-06 | `transparent-input-20260906/` |
| GTK 4 keyboard layouts | Wayland and XWayland | German and French key positions; human and Agent layouts independent | V | 2026-09-06 | `transparent-input-20260906/` |
| Human clipboard | Wayland data device | Text, PNG and 1.2 MB HTML both ways; human clipboard survives Agent paste | V | `2a485d54c50a`, 2026-09-06 | `human-compat-20260906/` |

### Notes on partial and visual-only rows

- **ONLYOFFICE**: the inserted text is confirmed by OCR of the private frame, not
  by reading the saved document. The window ran under XWayland even though the
  launch requested Wayland. The legacy OCR paste lane for pre-0.5.0 sessions
  did not insert text in the same environment and was removed in 0.6.0.
- **WeChat**: buttons that expose only a focus action need a visual click. Some
  embedded subtrees report `bounds_unmapped`; IME candidates keep the top-left
  placement. Only the listed paths were exercised, and no messages were sent.

## Known failures

| Case | Observed | Release, date | Evidence |
|---|---|---|---|
| Mousepad Save As driven by AT-SPI | AT-SPI query timed out, then the dialog tree was empty; typed name not shown. Root cause not confirmed. A later keyboard-driven save workflow passed. | `65048e8d2897`, 2026-09-06 | `computer-use-demo-20260906-031753/` |
| ONLYOFFICE legacy X11 OCR paste (removed in 0.6.0) | Paste menu found and clicked; text not inserted | source `f54ba9f`, 2026-10-16 | `20261016-review-followups/onlyoffice-compat*/` |

## Not yet verified

These rows of the acceptance matrix in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)
have no current evidence:

- Electron applications (for example VS Code) and Chromium multi-process node
  replacement in the AT-SPI tree (T22). Screenshots from 2026-09-01 of Chrome,
  Kdenlive, btop and RawTherapee predate the vNext protocol and only show launch.
- Canvas-only or semantics-free applications reporting semantic absence instead
  of empty success (T23).
- Portal, authentication and external URL handlers crossing the scope boundary (T30).
- Parity with any named macOS Computer Use product on the same tasks.
- Input methods other than Fcitx5 and IBus, and toolkits other than GTK 3/4,
  Qt 6, VCL and Chromium.

Add a row only after a real run, with the release, date and evidence path.
