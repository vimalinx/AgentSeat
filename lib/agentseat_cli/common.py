"""Errors, preferences, private storage and session state files."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import time
import tomllib
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import i18n, paths
from .i18n import _

DEFAULT_PREFERENCES: dict[str, Any] = {
    "default_workspace": 1,
    "human_priority_grace_ms": 1500,
    "eye_hud": False,
    "language": "auto",
}
PREFERENCES = dict(DEFAULT_PREFERENCES)


class AgentSeatError(RuntimeError):
    pass


def run(argv: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, text=True, capture_output=True, env=env, check=False)
    if check and result.returncode:
        raise AgentSeatError(
            result.stderr.strip() or result.stdout.strip() or _("command failed: {command}").format(command=argv[0])
        )
    return result


def output_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def storage_paths() -> dict[str, Any]:
    return {
        "instance": paths.SELECTED_INSTANCE or "default",
        "python_sdk": str((paths.ROOT / "share/python/agentseat_client.py") if (paths.ROOT / "share/python/agentseat_client.py").exists() else paths.ROOT / "python/agentseat_client.py"),
        "config": str(paths.CONFIG_HOME),
        "config_file": str(paths.CONFIG_FILE),
        "data": str(paths.DATA_HOME),
        "state": str(paths.STATE_HOME),
        "logs": str(paths.LOG_DIR),
        "captures": str(paths.EVIDENCE),
        "cache": str(paths.CACHE_HOME),
        "runtime": str(paths.RUNTIME),
        "privacy": {
            "chat": _("memory-only; cleared when the daemon exits"),
            "runtime": _("volatile; removed by the user session runtime manager"),
            "captures": _("written only when observe is requested"),
        },
    }


def load_preferences() -> dict[str, Any]:
    preferences = dict(DEFAULT_PREFERENCES)
    if not paths.CONFIG_FILE.exists():
        return preferences
    try:
        raw = tomllib.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentSeatError(
            _("cannot read AgentSeat configuration {path}: {error}").format(path=paths.CONFIG_FILE, error=exc)
        ) from exc
    general = raw.get("general", {})
    if not isinstance(general, dict):
        raise AgentSeatError(_("AgentSeat configuration [general] must be a table"))
    for name in preferences:
        if name in general:
            preferences[name] = general[name]
    workspace = preferences["default_workspace"]
    grace_ms = preferences["human_priority_grace_ms"]
    eye_hud = preferences["eye_hud"]
    language = preferences["language"]
    if not isinstance(workspace, int) or isinstance(workspace, bool) or workspace < 1:
        raise AgentSeatError(_("general.default_workspace must be a positive integer"))
    if not isinstance(grace_ms, int) or isinstance(grace_ms, bool) or not 0 <= grace_ms <= 60_000:
        raise AgentSeatError(_("general.human_priority_grace_ms must be an integer from 0 to 60000"))
    if not isinstance(eye_hud, bool):
        raise AgentSeatError(_("general.eye_hud must be true or false"))
    if not isinstance(language, str):
        raise AgentSeatError(_("general.language must be auto, en, zh-CN, or zh-TW"))
    if language.casefold() != "auto":
        base = language.split(".", 1)[0].split("@", 1)[0]
        if base.casefold() not in i18n.SUPPORTED_LANGUAGES:
            raise AgentSeatError(_("general.language must be auto, en, zh-CN, or zh-TW"))
        preferences["language"] = i18n.LANGUAGE_NAMES[i18n.SUPPORTED_LANGUAGES[base.casefold()]]
    return preferences


def configuration_status() -> dict[str, Any]:
    return {
        "path": str(paths.CONFIG_FILE),
        "exists": paths.CONFIG_FILE.is_file(),
        "effective": dict(PREFERENCES),
        "resolved_language": i18n.LANGUAGE_NAMES[i18n.CURRENT_LANGUAGE],
        "locale_dir": str(i18n.CURRENT_LOCALE_DIR) if i18n.CURRENT_LOCALE_DIR else None,
    }


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def ensure_runtime_storage() -> None:
    for path in (paths.RUNTIME, paths.DATA_HOME, paths.STATE_HOME, paths.CACHE_HOME, paths.LOG_DIR, paths.EVIDENCE):
        ensure_private_directory(path)


@contextmanager
def lifecycle_lock() -> Any:
    """Serialize start/stop transactions for one runtime namespace."""
    ensure_private_directory(paths.RUNTIME)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(paths.LIFECYCLE_LOCK, flags, 0o600)
    except OSError as exc:
        message = _("refusing to use an unexpected AgentSeat lifecycle lock: {path}").format(
            path=paths.LIFECYCLE_LOCK
        )
        raise AgentSeatError(f"{message}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise AgentSeatError(
                _("refusing to use an unexpected AgentSeat lifecycle lock: {path}").format(path=paths.LIFECYCLE_LOCK)
            )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def atomic_write_private_text(path: Path, content: str) -> None:
    ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def open_private_log(path: Path) -> Any:
    ensure_private_directory(path.parent)
    stream = path.open("ab", buffering=0)
    os.chmod(path, 0o600)
    return stream


def load_state() -> dict[str, Any]:
    if not paths.STATE.exists():
        return {}
    try:
        return json.loads(paths.STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(value: dict[str, Any]) -> None:
    atomic_write_private_text(paths.STATE, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def wait_for(predicate: Any, timeout: float, message: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AgentSeatError(message)


def normalize_argv(values: list[str]) -> list[str]:
    argv = list(values)
    if argv and argv[0] == "--":
        argv.pop(0)
    if not argv or any(not value or "\0" in value for value in argv):
        raise AgentSeatError(_("run requires a non-empty command after --"))
    return argv


def normalized_cwd(value: str | None) -> str:
    path = Path.cwd() if value is None else Path(value).expanduser()
    path = path.resolve()
    if not path.is_dir():
        raise AgentSeatError(_("working directory does not exist: {path}").format(path=path))
    return str(path)
