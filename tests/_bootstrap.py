"""Load the HA-independent wear_tracker modules without importing the package.

`custom_components/wear_tracker/__init__.py` pulls in Home Assistant at import
time, but `const`, `state_machine`, `registry` and `storage` have no runtime HA
dependency (only `TYPE_CHECKING`). This loads just those four under a synthetic
package so their relative imports resolve, letting the bulk of the test suite run
with plain pytest — no Home Assistant install required.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

COMP = pathlib.Path(__file__).resolve().parent.parent / "custom_components" / "wear_tracker"
_PKG = "wear_tracker_under_test"


def _ensure_package() -> None:
    if _PKG in sys.modules:
        return
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(COMP)]
    pkg.__package__ = _PKG
    sys.modules[_PKG] = pkg


def _load(name: str):
    full = f"{_PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, COMP / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _PKG
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


_ensure_package()
const = _load("const")
state_machine = _load("state_machine")  # registered before storage (which imports it)
registry = _load("registry")
storage = _load("storage")
catalog = _load("catalog")
rollup = _load("rollup")
