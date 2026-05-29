"""Make `tests/` importable regardless of pytest's import mode (so the
HA-independent tests can `import _bootstrap`), and isolate HA tests.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir):
    """Give every HA test a fresh config dir so the entity registry and the
    wear_tracker SQLite DB never leak between tests (the harness default is a
    single shared config dir)."""
    return hass_tmp_config_dir
