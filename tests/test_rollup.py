"""Flap/connection rates, baselines, anomaly detection and event debounce."""
from __future__ import annotations

import sqlite3

from _bootstrap import COMP, rollup

NOW = 1_700_000_000  # fixed epoch so tests are deterministic
HOUR = 3600
DAY = 86400

_THRESHOLDS = dict(
    window_days=30,
    refresh_s=3600,
    floor=0.2,
    flap_ratio=5.0,
    flap_min=6.0,
    conn_ratio=5.0,
    conn_min=3.0,
    debounce_s=3600,
)


def _seed_conn():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    for sql in sorted((COMP / "migrations").glob("[0-9][0-9]_*.sql")):
        conn.executescript(sql.read_text("utf-8"))
    return conn


def _add(conn, meta_id, ts, to_state="ON"):
    conn.execute(
        "INSERT INTO transitions (entity_meta_id, ts, from_state, to_state, raw_from, raw_to, delta_s)"
        " VALUES (?, ?, 'OFF', ?, 'off', 'on', 0.0)",
        (meta_id, ts, to_state),
    )


def test_compute_rates_counts_windows():
    conn = _seed_conn()
    for i in range(4):  # 4 transitions in the last hour, 2 of them drops
        _add(conn, 1, NOW - 60 * (i + 1), "DISCONNECTED" if i < 2 else "ON")
    _add(conn, 1, NOW - 5 * HOUR)  # within 24h, outside 1h
    rates = rollup.compute_rates(conn, 1, NOW)
    assert rates["flap_rate_1h"] == 4
    assert rates["unavail_rate_1h"] == 2
    assert rates["flap_rate_24h"] == 5 / 24.0


def test_is_anomaly_rules():
    assert rollup.is_anomaly(10, 1.0, 5.0, 6.0) is True
    assert rollup.is_anomaly(4, 1.0, 5.0, 6.0) is False  # below minimum
    assert rollup.is_anomaly(10, 3.0, 5.0, 6.0) is False  # not 5x baseline (15)


def test_baseline_floor_when_no_history():
    conn = _seed_conn()
    flap, unavail = rollup.get_baseline(
        conn, 1, 12, NOW, window_days=30, refresh_s=3600, floor=0.2
    )
    assert flap == 0.2 and unavail == 0.2


def test_baseline_caches_and_refreshes():
    conn = _seed_conn()
    rollup.get_baseline(conn, 1, 12, NOW, window_days=30, refresh_s=3600, floor=0.2)
    cached = conn.execute(
        "SELECT updated_ts FROM flap_baseline WHERE entity_meta_id=1 AND hour_of_day=12"
    ).fetchone()
    assert cached["updated_ts"] == NOW


def test_record_event_debounces():
    conn = _seed_conn()
    assert rollup.record_event_if_due(conn, 1, "flap_anomaly", "flap", NOW, 3600) is True
    assert rollup.record_event_if_due(conn, 1, "flap_anomaly", "flap", NOW + 10, 3600) is False
    assert rollup.record_event_if_due(conn, 1, "flap_anomaly", "flap", NOW + 4000, 3600) is True


def test_evaluate_flags_and_debounces_flap_anomaly():
    conn = _seed_conn()
    for i in range(8):  # spike: 8 transitions in the last hour, no history
        _add(conn, 1, NOW - 60 * (i + 1))
    first = rollup.evaluate(conn, 1, NOW, **_THRESHOLDS)
    assert first["health"] is True
    assert first["flap"]["due"] is True

    second = rollup.evaluate(conn, 1, NOW + 30, **_THRESHOLDS)
    assert second["health"] is True  # still anomalous
    assert second["flap"]["due"] is False  # but debounced


def test_evaluate_quiet_device_is_healthy():
    conn = _seed_conn()
    _add(conn, 1, NOW - 100)  # a single transition: nowhere near anomalous
    result = rollup.evaluate(conn, 1, NOW, **_THRESHOLDS)
    assert result["health"] is False
    assert result["flap"]["due"] is False


def test_wear_critical_fires_once_per_threshold():
    conn = _seed_conn()
    conn.execute(
        "INSERT INTO summary (entity_meta_id, lifetime_seconds, updated_ts) VALUES (1, ?, 0)",
        (95 * 3600,),  # 95 hours against a 100-hour rating -> 95%
    )
    wear_args = dict(rated_hours=100, wear_thresholds=(90, 95, 100), wear_debounce_s=10**12)

    first = rollup.evaluate(conn, 1, NOW, **_THRESHOLDS, **wear_args)
    assert {c["threshold"] for c in first["wear_critical"]} == {90, 95}

    second = rollup.evaluate(conn, 1, NOW + 10, **_THRESHOLDS, **wear_args)
    assert second["wear_critical"] == []  # already fired, debounced ~forever


def test_wear_critical_cycles_only_device_fires_at_all_thresholds():
    """A cycles-rated device (no rated_hours) must still get wear_critical events
    with metric 'cycles' and cycles:NN discriminators (rollup.py finding)."""
    conn = _seed_conn()
    conn.execute(
        "INSERT INTO summary (entity_meta_id, lifetime_cycles, updated_ts) VALUES (1, ?, 0)",
        (50000,),  # 50000 cycles against a 50000 rating -> 100%
    )
    wear_args = dict(rated_cycles=50000, wear_thresholds=(90, 95, 100), wear_debounce_s=10**12)

    first = rollup.evaluate(conn, 1, NOW, **_THRESHOLDS, **wear_args)
    crossed = first["wear_critical"]
    assert {c["threshold"] for c in crossed} == {90, 95, 100}
    assert all(c["metric"] == "cycles" for c in crossed)
    assert all(c["rated"] == 50000 for c in crossed)

    discriminators = [
        r[0] for r in conn.execute(
            "SELECT discriminator FROM events_fired WHERE event_kind='wear_critical' "
            "ORDER BY discriminator"
        ).fetchall()
    ]
    assert discriminators == ["cycles:100", "cycles:90", "cycles:95"]

    second = rollup.evaluate(conn, 1, NOW + 10, **_THRESHOLDS, **wear_args)
    assert second["wear_critical"] == []  # debounced


def test_wear_critical_reports_both_metrics_when_both_rated():
    conn = _seed_conn()
    conn.execute(
        "INSERT INTO summary (entity_meta_id, lifetime_seconds, lifetime_cycles, updated_ts) "
        "VALUES (1, ?, ?, 0)",
        (91 * 3600, 46000),  # 91% of 100h and 92% of 50000 cycles
    )
    result = rollup.evaluate(
        conn, 1, NOW, **_THRESHOLDS,
        rated_hours=100, rated_cycles=50000,
        wear_thresholds=(90, 95, 100), wear_debounce_s=10**12,
    )
    metrics = {(c["metric"], c["threshold"]) for c in result["wear_critical"]}
    assert metrics == {("hours", 90), ("cycles", 90)}
