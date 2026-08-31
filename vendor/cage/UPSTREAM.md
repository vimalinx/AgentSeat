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
- own and terminate the launched application's process group on shutdown;
- route virtual input to the focused application surface, including the
  private XWayland single-app root-surface case.
- preserve application-requested top-level and dialog geometry, honor managed
  XWayland configure requests, center transient children on their parent, and
  constrain only surfaces which exceed the private output;
- fit the nested output to the largest mapped application surface and publish a
  private mode-0600 size receipt so the AgentSeat controller can keep its one
  known floating host window free of desktop-sized black canvas;
- keep mapped GUI surfaces alive when a short-lived launcher hands work to a
  resident process, while retaining bounded failed-launch cleanup and owned
  process-group teardown;
- expose current and legacy Wayland data-control managers only on the private
  micro-host socket so one-shot Unicode clipboard offers do not require a host
  input serial or touch the host clipboard;
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
