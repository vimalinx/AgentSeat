"""Named application instances and how they are launched."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from . import common, desktop, paths, procs
from .i18n import _


def instances() -> list[dict[str, Any]]:
    return json.loads(common.run(["hyprctl", "instances", "-j"]).stdout)


def list_instances() -> dict[str, Any]:
    instances: list[dict[str, Any]] = []
    if not paths.INSTANCE_RUNTIME_ROOT.is_dir():
        return {"count": 0, "instances": instances}
    for runtime in sorted(paths.INSTANCE_RUNTIME_ROOT.iterdir(), key=lambda item: item.name):
        if runtime.is_symlink() or not runtime.is_dir():
            continue
        if not re.fullmatch(r"app-[0-9a-f]{24}", runtime.name):
            continue
        state_path = runtime / "state.json"
        try:
            state_value = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        try:
            instance_id = normalize_instance_id(str(state_value.get("instance", "")))
        except argparse.ArgumentTypeError:
            continue
        if instance_directory_name(instance_id) != runtime.name:
            continue
        try:
            microhost = int((runtime / "microhost.pid").read_text(encoding="utf-8").strip())
        except (FileNotFoundError, OSError, ValueError):
            microhost = None
        running = bool(
            microhost
            and procs.microhost_belongs_to_runtime(microhost, runtime)
            and (runtime / "control.sock").exists()
        )
        instances.append({
            "instance": instance_id,
            "running": running,
            "session_id": state_value.get("session_id"),
            "host_workspace": state_value.get("host_workspace"),
            "microhost_pid": microhost if running else None,
            "launch": state_value.get("launch", {}),
        })
    return {"count": len(instances), "instances": instances}


def normalize_instance_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}", value):
        raise argparse.ArgumentTypeError(
            _("instance identifier must use 1-48 letters, digits, dots, underscores, or hyphens")
        )
    return value


def instance_paths(instance_id: str) -> tuple[Path, Path]:
    normalized = normalize_instance_id(instance_id)
    directory_name = instance_directory_name(normalized)
    return paths.INSTANCE_RUNTIME_ROOT / directory_name, paths.INSTANCE_STATE_ROOT / directory_name


def instance_directory_name(instance_id: str) -> str:
    normalized = normalize_instance_id(instance_id)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"app-{digest}"


def instance_environment(instance_id: str) -> dict[str, str]:
    runtime, state_home = instance_paths(instance_id)
    environment = dict(os.environ)
    environment.update({
        "AGENTSEAT_INSTANCE": instance_id,
        "AGENTSEAT_RUNTIME_DIR": str(runtime),
        "AGENTSEAT_STATE_HOME": str(state_home),
    })
    return environment


def execute_for_instance(instance_id: str, argv: list[str]) -> None:
    os.execve(
        sys.executable,
        [sys.executable, str(paths.CONTROLLER), *argv],
        instance_environment(instance_id),
    )


def launch_executable(argv: list[str]) -> str:
    """Return the real command name when argv uses env as a generic wrapper."""
    if not argv:
        return "app"
    index = 0
    if Path(argv[0]).name == "env":
        index = 1
        options_with_values = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
        while index < len(argv):
            value = argv[index]
            if value == "--":
                index += 1
                break
            if value in options_with_values:
                index += 2
                continue
            if value.startswith(("--unset=", "--chdir=", "--split-string=")):
                index += 1
                continue
            if value.startswith("-") or "=" in value:
                index += 1
                continue
            break
    return argv[index] if index < len(argv) else argv[0]


def allocate_instance_id(argv: list[str]) -> str:
    basename = Path(launch_executable(argv)).name.casefold()
    slug = re.sub(r"[^a-z0-9._-]+", "-", basename).strip(".-_") or "app"
    slug = slug[:24]
    for _attempt in range(16):
        candidate = f"{slug}-{uuid.uuid4().hex[:8]}"
        runtime, _state_home = instance_paths(candidate)
        if not runtime.exists():
            return candidate
    raise common.AgentSeatError(_("could not allocate a unique AgentSeat instance identifier"))


def launch_workspace(value: str) -> int:
    if value.casefold() == "current":
        workspace = desktop.host_snapshot(desktop.host_instance()).get("workspace")
        if isinstance(workspace, int) and workspace > 0:
            return workspace
        raise common.AgentSeatError(_("current host workspace is unavailable"))
    try:
        workspace = int(value)
    except ValueError as exc:
        raise common.AgentSeatError(_("host workspace must be 'current' or a positive integer")) from exc
    if workspace <= 0:
        raise common.AgentSeatError(_("host workspace must be 'current' or a positive integer"))
    return workspace


def application_hud_position_file(argv: list[str], cwd: str | None = None) -> Path:
    executable = launch_executable(argv)
    base = Path(cwd or os.getcwd())
    resolved = Path(shutil.which(executable) or (base / executable)).resolve()
    identity = str(resolved)
    # Keep script applications distinct even when they share an interpreter.
    if resolved.name.startswith(("python", "pypy")):
        args = argv[argv.index(executable) + 1:]
        if args and args[0] == "-m" and len(args) > 1:
            identity += " module:" + args[1]
        elif args and not args[0].startswith("-"):
            identity += " script:" + str((base / args[0]).resolve())
    key = hashlib.sha256(identity.encode()).hexdigest()
    directory = paths.CONFIG_HOME / "applications" / key
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / "hud-position.json"
