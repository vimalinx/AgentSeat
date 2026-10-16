"""AgentSeat command-line controller.

Each top-level name has exactly one owning module, and modules refer to each
other's names as `module.name`. `Controller` presents the whole package as one
namespace: reading a name returns it from its owner, and assigning a name sets
it on its owner, so the change is seen by every module.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import ModuleType

MODULES = (
    "i18n", "paths", "common", "transport", "procs", "desktop", "instances",
    "lifecycle", "actions", "observe", "semantic", "diagnostics", "cli",
)


def _owned_names(module: ModuleType) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(Path(module.__file__).read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.If):  # conditional assignments such as the controller ID
            names.update(item.id for item in ast.walk(node)
                         if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store))
    return names


class Controller:
    """One namespace over the package, for scripts and contract tests."""

    def __init__(self) -> None:
        owners: dict[str, ModuleType] = {}
        for name in MODULES:
            module = importlib.import_module(f"{__name__}.{name}")
            for owned in _owned_names(module):
                owners.setdefault(owned, module)
        object.__setattr__(self, "_owners", owners)

    def __getattr__(self, name: str):
        if name in self._owners:
            return getattr(self._owners[name], name)
        # Imported names such as `os` or `time` are the same object everywhere.
        for module in dict.fromkeys(self._owners.values()):
            if name in vars(module):
                return vars(module)[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        try:
            setattr(self._owners[name], name, value)
        except KeyError:
            raise AttributeError(name) from None
