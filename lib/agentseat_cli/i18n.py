"""Interface language selection and the shared gettext translator."""

from __future__ import annotations

import argparse
import gettext
import os
from pathlib import Path

from . import paths

SUPPORTED_LANGUAGES = {
    "en": "en",
    "en-us": "en",
    "en_us": "en",
    "zh": "zh_CN",
    "zh-cn": "zh_CN",
    "zh_cn": "zh_CN",
    "zh-hans": "zh_CN",
    "zh_hans": "zh_CN",
    "zh-tw": "zh_TW",
    "zh_tw": "zh_TW",
    "zh-hk": "zh_TW",
    "zh_hk": "zh_TW",
    "zh-hant": "zh_TW",
    "zh_hant": "zh_TW",
}
LANGUAGE_NAMES = {"en": "en", "zh_CN": "zh-CN", "zh_TW": "zh-TW"}
CURRENT_LANGUAGE = "en"
CURRENT_LOCALE_DIR: Path | None = None
_translation: gettext.NullTranslations = gettext.NullTranslations()


def _(message: str) -> str:
    # One stable function for every module; configure_i18n swaps the catalog.
    return _translation.gettext(message)


def normalize_language(value: str | None, *, allow_auto: bool = False) -> str:
    if value is None or not value.strip():
        return "auto" if allow_auto else "en"
    cleaned = value.strip()
    if cleaned.casefold() == "auto":
        if allow_auto:
            return "auto"
        return system_language()
    base = cleaned.split(".", 1)[0].split("@", 1)[0]
    normalized = SUPPORTED_LANGUAGES.get(base.casefold())
    if normalized:
        return normalized
    if base.casefold().startswith("en"):
        return "en"
    return "en"


def system_language() -> str:
    candidates: list[str] = []
    language = os.environ.get("LANGUAGE", "")
    if language:
        candidates.extend(item for item in language.split(":") if item)
    candidates.extend(
        value for value in (os.environ.get("LC_ALL"), os.environ.get("LC_MESSAGES"), os.environ.get("LANG"))
        if value
    )
    for candidate in candidates:
        base = candidate.split(".", 1)[0].split("@", 1)[0]
        normalized = SUPPORTED_LANGUAGES.get(base.casefold())
        if normalized:
            return normalized
        if base.casefold().startswith("en"):
            return "en"
    return "en"


def locale_directory() -> Path | None:
    configured = os.environ.get("AGENTSEAT_LOCALE_DIR")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend((paths.ROOT / "share/locale", paths.ROOT / "build/locale"))
    return next((path.resolve() for path in candidates if path.is_dir()), None)


def configure_i18n(requested: str | None) -> str:
    global CURRENT_LANGUAGE, CURRENT_LOCALE_DIR, _translation
    selected = normalize_language(requested, allow_auto=True)
    if selected == "auto":
        selected = system_language()
    CURRENT_LANGUAGE = selected
    CURRENT_LOCALE_DIR = locale_directory()
    try:
        _translation = gettext.translation(
            "agentseat",
            localedir=os.fspath(CURRENT_LOCALE_DIR) if CURRENT_LOCALE_DIR else None,
            languages=[selected],
            fallback=True,
        )
    except OSError:
        _translation = gettext.NullTranslations()
    return selected


def language_override(argv: list[str]) -> str | None:
    for index, value in enumerate(argv):
        if value == "--language" and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith("--language="):
            return value.partition("=")[2]
    return None


def language_option(value: str) -> str:
    if value.casefold() == "auto":
        return "auto"
    base = value.split(".", 1)[0].split("@", 1)[0]
    normalized = SUPPORTED_LANGUAGES.get(base.casefold())
    if not normalized:
        raise argparse.ArgumentTypeError(_("language must be auto, en, zh-CN, or zh-TW"))
    return LANGUAGE_NAMES[normalized]


configure_i18n(os.environ.get("AGENTSEAT_LANGUAGE", "auto"))

# These strings are owned by argparse but use the AgentSeat translator after
# language selection. Keeping extraction markers here gives the built-in help
# and error chrome the same language as our command descriptions.
if False:  # pragma: no cover - gettext extraction markers only
    _("%(heading)s:")
    _("usage: ")
    _(" (default: %(default)s)")
    _("argument %(argument_name)s: %(message)s")
    _("show program's version number and exit")
    _("unknown parser %(parser_name)r (choices: %(choices)s)")
    _("command '%(parser_name)s' is deprecated")
    _("argument \"-\" with mode %r")
    _("can't open '%(filename)s': %(error)s")
    _("positional arguments")
    _("options")
    _("show this help message and exit")
    _("subcommands")
    _("unrecognized arguments: %s")
    _("not allowed with argument %s")
    _("ambiguous option: %(option)s could match %(matches)s")
    _("ignored explicit argument %r")
    _("option '%(option)s' is deprecated")
    _("argument '%(argument_name)s' is deprecated")
    _("the following arguments are required: %s")
    _("one of the arguments %s is required")
    _("expected one argument")
    _("expected at most one argument")
    _("expected at least one argument")
    _("unexpected option string: %s")
    _("invalid %(type)s value: %(value)r")
    _("invalid choice: %(value)r (choose from %(choices)s)")
    _("%(prog)s: error: %(message)s\n")
    _("%(prog)s: warning: %(message)s\n")
