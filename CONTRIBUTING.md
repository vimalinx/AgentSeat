# Contributing

AgentSeat aims to remain a small, application-agnostic input and observation
scaffold. Changes should preserve these invariants:

- no host pointer, keyboard, clipboard, focus, or workspace injection;
- no Hyprland patch or private Hyprland protocol dependency;
- no per-application adapter in the generic core;
- no persistent chat transcript by default;
- no runtime data in the source tree.

Before submitting a change, run:

```sh
./scripts/build.sh
pytest -q tests/test_controller.py
```

Live tests open ordinary host windows. Run them on a workspace where they will
not interrupt another person, then confirm that host focus and the hardware
pointer did not change.
