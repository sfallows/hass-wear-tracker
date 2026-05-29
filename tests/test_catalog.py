"""Catalog normalization + lookup precedence (DESIGN §7)."""
from __future__ import annotations

from _bootstrap import catalog

lookup_rated = catalog.lookup_rated


def test_corporate_suffixes_normalize_together():
    assert catalog._normalize("Signify Netherlands B.V.") == "signify"
    assert catalog._normalize("signify") == "signify"
    assert catalog._normalize("Signify") == "signify"
    assert catalog._normalize("IKEA of Sweden") == "ikea"
    assert catalog._normalize("Acme Inc.") == "acme"


def test_exact_match():
    entry = lookup_rated("Signify Netherlands B.V.", "LWA001")
    assert entry is not None and entry.rated_hours == 25000


def test_exact_match_after_normalization():
    # Different spelling of the manufacturer still resolves.
    assert lookup_rated("signify", "LWA001").rated_hours == 25000


def test_glob_model_match():
    entry = lookup_rated("TP-Link", "HS103 Kasa Smart Plug")
    assert entry is not None and entry.rated_cycles == 40000


def test_manufacturer_wide_default():
    assert lookup_rated("Sengled", "anything-at-all").rated_hours == 25000


def test_empty_manufacturer_returns_none():
    assert lookup_rated("", "LWA001") is None
    assert lookup_rated(None, "LWA001") is None
    assert lookup_rated("B.V.", "x") is None  # normalizes to empty


def test_unknown_returns_none():
    assert lookup_rated("NoSuchVendor", "ZZZ") is None


def test_exact_beats_glob_and_default():
    # Shelly Plus 1 is an exact cycles entry; an unrelated Shelly model misses
    # (no Shelly default), proving exact lookup is keyed correctly.
    assert lookup_rated("Shelly", "Shelly Plus 1").rated_cycles == 50000
    assert lookup_rated("Shelly", "Unknown Model") is None
