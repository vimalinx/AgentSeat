# AgentSeat micro-host provenance

- Upstream: https://github.com/cage-kiosk/cage
- Release: v0.3.1
- Commit: `b7b774461a5bccb9308b1d7ad3ff4ba441466af1`
- License: MIT (`LICENSE` in this directory)

AgentSeat keeps Cage's small single-application wlroots boundary and XWayland
support. Local changes are deliberately scoped to the collaboration wrapper:

- name the binary `agentseat-microhost`;
- emit a mode-0600 readiness receipt after the application surface maps;
- render all outputs on `SIGUSR1` so standard `wlr-screencopy` can capture an
  application while the outer host window is on an inactive workspace;
- preserve parent-owned logical geometry while rendering the nested output at
  the target monitor's physical resolution through `wp_viewporter`;
- expose standard fractional-scale and viewporter protocols inside the private
  compositor so native clients render at the physical pixel density without an
  application-specific zoom override;
- size the primary application surface from the parent-owned tiled output while
  preserving natural transient-dialog geometry;
- run a demand-driven fallback frame timer only when an inactive parent stops
  delivering frames and the private scene still needs one;
- own and terminate the launched application's process group on shutdown;
- route virtual input to the focused application surface, including the
  private XWayland single-app root-surface case.
- fill the parent-owned tiled output with the primary application surface,
  while preserving natural transient-dialog geometry and centering transients
  on their parent;
- leave outer sizing and tiling to Hyprland instead of publishing an
  application-size receipt or forcing the host window to float;
- keep mapped GUI surfaces alive when a short-lived launcher hands work to a
  resident process, while retaining bounded failed-launch cleanup and owned
  process-group teardown;
- expose current and legacy Wayland data-control managers only on the private
  micro-host socket for private application clipboard support without host
  input serials or access to the host clipboard;
- publish bounded, multi-read Agent clipboard sources through the same
  epoch-checked control gate as virtual input, with nonblocking receive-pipe
  writes and cleanup on takeover, timeout, replacement and controller loss;
- classify physical and virtual micro-host input devices and emit a throttled
  local datagram only for physical activity, enabling AgentSeat's human-priority
  arbitration without observing or modifying the host compositor;
- recognize only `dev.vimalinx.agentseat.eye` as a private collaboration overlay,
  place it at the micro-host bottom-right, keep it above the wrapped app without
  stealing focus on map, and exclude it from the primary-app readiness receipt;
- implement bounded XDG interactive movement for that overlay, preserving its
  dragged anchor across expand/collapse and keeping pointer grab serials scoped
  to the micro-host seat.

No host Hyprland source, binary, configuration, input socket, or private
protocol is replaced or patched.
