"""Root conftest: make `custom_components.wear_tracker` discoverable in tests.

The HA test harness points the config dir at its own `testing_config`, whose
`custom_components` is a *regular* package (has `__init__.py`) — so it shadows
ours instead of merging as a namespace package. Splicing our directory onto that
package's `__path__` lets Home Assistant's loader find wear_tracker.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    import custom_components

    _ours = str(ROOT / "custom_components")
    if _ours not in list(custom_components.__path__):
        custom_components.__path__.append(_ours)
except Exception:  # pragma: no cover - only relevant under the HA test harness
    pass
