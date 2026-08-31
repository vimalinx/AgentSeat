# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting from this repository's
Security tab instead of opening a public issue. Include the affected version,
a minimal reproduction, and whether the issue can cross AgentSeat's
single-application boundary.

## Security boundary

AgentSeat isolates Agent-generated input from the host seat by launching the
target application inside a private Wayland micro-host. It does not sandbox the
application's filesystem, network, process privileges, or user account. Run
only applications and Agent commands that you would otherwise trust as your
local user.

The control socket and transient launch/state files are user-private and live
under `$XDG_RUNTIME_DIR/agentseat`. Chat messages are memory-only. Captures are
created only by an explicit observation request and are stored with mode 0600.

Do not expose the control socket to another user, container, or network bridge.
