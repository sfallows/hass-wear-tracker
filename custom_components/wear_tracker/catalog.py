"""Built-in catalog of manufacturer-rated lifetimes (DESIGN §7).

Data-only and PR-able. `lookup_rated()` normalizes manufacturer/model, then tries
exact match → model glob → manufacturer-wide default. Misses are logged once at
DEBUG so users know what to contribute.
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    rated_hours: float | None = None
    rated_cycles: int | None = None


CATALOG: dict[tuple[str, str], CatalogEntry] = {
    ("Signify Netherlands B.V.", "LWA001"): CatalogEntry(rated_hours=25000),  # Hue White
    ("Signify Netherlands B.V.", "LTA001"): CatalogEntry(rated_hours=25000),  # Hue White Ambiance
    ("LIFX", "LIFX Mini"): CatalogEntry(rated_hours=22500),
    ("Sengled", "*"): CatalogEntry(rated_hours=25000),
    ("Shelly", "Shelly Plus 1"): CatalogEntry(rated_cycles=50000),
    ("TP-Link", "*Kasa*"): CatalogEntry(rated_cycles=40000),
}

# Longest / multi-word suffixes first so "netherlands b.v." strips before "b.v.".
_SUFFIXES = (
    "netherlands b.v.",
    "of sweden",
    "b.v.",
    "gmbh",
    "corp.",
    "corp",
    "inc.",
    "inc",
    "llc",
    "ltd.",
    "ltd",
    "co.",
    "co",
)


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    text = " ".join(value.lower().split())
    changed = True
    while changed:
        changed = False
        text = text.strip().strip(",").strip()
        for suffix in _SUFFIXES:
            if text == suffix:
                return ""
            if text.endswith(" " + suffix):
                text = text[: -len(suffix)]
                changed = True
                break
    return text


def _build_index() -> tuple[dict, dict, dict]:
    exact: dict[tuple[str, str], CatalogEntry] = {}
    globs: dict[tuple[str, str], CatalogEntry] = {}
    defaults: dict[str, CatalogEntry] = {}
    for (manufacturer, model), entry in CATALOG.items():
        nm = _normalize(manufacturer)
        nmodel = _normalize(model)
        if nmodel == "*":
            defaults[nm] = entry
        elif "*" in nmodel:
            globs[(nm, nmodel)] = entry
        else:
            exact[(nm, nmodel)] = entry
    return exact, globs, defaults


_EXACT, _GLOBS, _DEFAULTS = _build_index()
_LOGGED_MISSES: set[tuple[str, str]] = set()


def lookup_rated(manufacturer: str | None, model: str | None) -> CatalogEntry | None:
    """Return the rated lifetime for a device, or None if unknown.

    None for entities without an identifiable manufacturer — wear_pct is then
    `unknown` rather than a misleading guess.
    """
    nm = _normalize(manufacturer)
    if not nm:
        return None
    nmodel = _normalize(model)

    entry = _EXACT.get((nm, nmodel))
    if entry is not None:
        return entry

    for (gm, gmodel), candidate in _GLOBS.items():
        if gm == nm and fnmatch.fnmatchcase(nmodel, gmodel):
            return candidate

    entry = _DEFAULTS.get(nm)
    if entry is not None:
        return entry

    key = (nm, nmodel)
    if key not in _LOGGED_MISSES:
        _LOGGED_MISSES.add(key)
        _LOG.debug(
            "wear_tracker: no catalog entry for manufacturer=%r model=%r "
            "(contributions welcome)",
            manufacturer,
            model,
        )
    return None
