"""Process identity checks that tie pids to this runtime."""

from __future__ import annotations

import os
from pathlib import Path

from . import paths


def proc_exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def proc_argv(pid: int) -> list[str]:
    try:
        encoded = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [item.decode(errors="replace") for item in encoded.split(b"\0") if item]


def proc_environment_value(pid: int, name: str) -> str | None:
    try:
        encoded = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    prefix = os.fsencode(f"{name}=")
    for item in encoded.split(b"\0"):
        if item.startswith(prefix):
            return os.fsdecode(item[len(prefix):])
    return None


def argv_has_pair(pid: int, option: str, expected: Path) -> bool:
    argv = proc_argv(pid)
    return any(
        value == option and index + 1 < len(argv) and argv[index + 1] == str(expected)
        for index, value in enumerate(argv)
    )


def executable_name(pid: int) -> str:
    executable = proc_exe(pid).removesuffix(" (deleted)")
    return Path(executable).name if executable else ""


def daemon_belongs_to_runtime(pid: int) -> bool:
    if executable_name(pid) != paths.NATIVE_DAEMON.name:
        return False
    return argv_has_pair(pid, "--socket", paths.CONTROL_SOCKET)


def microhost_belongs_to_runtime(pid: int, runtime: Path | None = None) -> bool:
    target_runtime = runtime if runtime is not None else paths.RUNTIME
    return (
        executable_name(pid) == paths.MICROHOST_EXE.name
        and proc_environment_value(pid, "AGENTSEAT_RUNTIME_DIR") == str(target_runtime)
    )


def eye_belongs_to_runtime(pid: int) -> bool:
    return (
        executable_name(pid) == paths.EYE_BINARY.name
        and argv_has_pair(pid, "--socket", paths.CONTROL_SOCKET)
    )


def microhost_pid() -> int | None:
    try:
        pid = int(paths.MICROHOST_PID.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return pid if microhost_belongs_to_runtime(pid) else None


def eye_pid() -> int | None:
    try:
        pid = int(paths.EYE_PID.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return pid if eye_belongs_to_runtime(pid) else None


def agent_daemon_pids() -> list[int]:
    result: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if daemon_belongs_to_runtime(pid):
            result.append(pid)
    return result
