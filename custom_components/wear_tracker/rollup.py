"""Flap/connection rate computation, 30-day baselines, and anomaly evaluation.

Pure SQLite — no Home Assistant dependency — so it runs on the writer thread and
is unit-testable directly. The coordinator calls `evaluate()` on each rollup tick
and fires events for the `*_due` results.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

_HOUR_S = 3600
_DAY_S = 86400


def _count(conn: sqlite3.Connection, meta_id: int, since_ts: int, drops_only: bool) -> int:
    sql = "SELECT COUNT(*) FROM transitions WHERE entity_meta_id = ? AND ts >= ?"
    if drops_only:
        sql += " AND to_state = 'DISCONNECTED'"
    return conn.execute(sql, (meta_id, since_ts)).fetchone()[0]


def compute_rates(conn: sqlite3.Connection, meta_id: int, now_ts: int) -> dict[str, float]:
    return {
        "flap_rate_1h": float(_count(conn, meta_id, now_ts - _HOUR_S, False)),
        "flap_rate_24h": _count(conn, meta_id, now_ts - _DAY_S, False) / 24.0,
        "unavail_rate_1h": float(_count(conn, meta_id, now_ts - _HOUR_S, True)),
    }


def _baseline_from_transitions(
    conn: sqlite3.Connection, meta_id: int, hour_of_day: int, now_ts: int, window_days: int
) -> tuple[float, float]:
    since = now_ts - window_days * _DAY_S
    hour_filter = "CAST(strftime('%H', ts, 'unixepoch') AS INTEGER) = ?"
    flap = conn.execute(
        f"SELECT COUNT(*) FROM transitions WHERE entity_meta_id = ? AND ts >= ? AND {hour_filter}",
        (meta_id, since, hour_of_day),
    ).fetchone()[0]
    drops = conn.execute(
        f"SELECT COUNT(*) FROM transitions WHERE entity_meta_id = ? AND ts >= ? "
        f"AND to_state = 'DISCONNECTED' AND {hour_filter}",
        (meta_id, since, hour_of_day),
    ).fetchone()[0]
    return flap / float(window_days), drops / float(window_days)


def get_baseline(
    conn: sqlite3.Connection,
    meta_id: int,
    hour_of_day: int,
    now_ts: int,
    *,
    window_days: int,
    refresh_s: int,
    floor: float,
) -> tuple[float, float]:
    """Return (flap, unavail) baselines for the hour, refreshing the cache lazily."""
    row = conn.execute(
        "SELECT flap_rate, unavail_rate, updated_ts FROM flap_baseline "
        "WHERE entity_meta_id = ? AND hour_of_day = ?",
        (meta_id, hour_of_day),
    ).fetchone()
    if row is None or (now_ts - row["updated_ts"]) >= refresh_s:
        flap, unavail = _baseline_from_transitions(
            conn, meta_id, hour_of_day, now_ts, window_days
        )
        conn.execute(
            """
            INSERT INTO flap_baseline (entity_meta_id, hour_of_day, flap_rate, unavail_rate, updated_ts)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(entity_meta_id, hour_of_day) DO UPDATE SET
                flap_rate = excluded.flap_rate,
                unavail_rate = excluded.unavail_rate,
                updated_ts = excluded.updated_ts
            """,
            (meta_id, hour_of_day, flap, unavail, now_ts),
        )
    else:
        flap, unavail = row["flap_rate"], row["unavail_rate"]
    return max(flap, floor), max(unavail, floor)


def is_anomaly(rate: float, baseline: float, ratio: float, minimum: float) -> bool:
    return rate >= minimum and rate > ratio * baseline


def record_event_if_due(
    conn: sqlite3.Connection,
    meta_id: int,
    event_kind: str,
    discriminator: str,
    now_ts: int,
    min_interval: int,
) -> bool:
    """Return True (and stamp fired_ts) if this event hasn't fired within
    `min_interval`; False if still debounced."""
    row = conn.execute(
        "SELECT fired_ts FROM events_fired WHERE entity_meta_id = ? AND event_kind = ? AND discriminator = ?",
        (meta_id, event_kind, discriminator),
    ).fetchone()
    if row is not None and (now_ts - row["fired_ts"]) < min_interval:
        return False
    conn.execute(
        """
        INSERT INTO events_fired (entity_meta_id, event_kind, discriminator, fired_ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(entity_meta_id, event_kind, discriminator) DO UPDATE SET fired_ts = excluded.fired_ts
        """,
        (meta_id, event_kind, discriminator, now_ts),
    )
    return True


def _wear_critical(
    conn: sqlite3.Connection,
    meta_id: int,
    now_ts: int,
    rated_hours: float | None,
    thresholds: tuple[int, ...],
    debounce_s: int,
) -> list[dict[str, Any]]:
    if not rated_hours:
        return []
    row = conn.execute(
        "SELECT lifetime_seconds FROM summary WHERE entity_meta_id = ?", (meta_id,)
    ).fetchone()
    if row is None:
        return []
    hours = row["lifetime_seconds"] / 3600.0
    pct = hours / rated_hours * 100.0
    crossed = []
    for threshold in thresholds:
        if pct >= threshold and record_event_if_due(
            conn, meta_id, "wear_critical", f"hours:{threshold}", now_ts, debounce_s
        ):
            crossed.append({"threshold": threshold, "value": round(hours, 2), "rated": rated_hours})
    return crossed


def evaluate(
    conn: sqlite3.Connection,
    meta_id: int,
    now_ts: int,
    *,
    window_days: int,
    refresh_s: int,
    floor: float,
    flap_ratio: float,
    flap_min: float,
    conn_ratio: float,
    conn_min: float,
    debounce_s: int,
    rated_hours: float | None = None,
    wear_thresholds: tuple[int, ...] = (),
    wear_debounce_s: int = 0,
) -> dict[str, Any]:
    """Compute rates, detect anomalies, debounce events. Returns rates, a health
    flag, per-anomaly `due` flags, and any wear-critical threshold crossings."""
    rates = compute_rates(conn, meta_id, now_ts)
    hour = time.gmtime(now_ts).tm_hour
    base_flap, base_unavail = get_baseline(
        conn, meta_id, hour, now_ts, window_days=window_days, refresh_s=refresh_s, floor=floor
    )
    flap_anom = is_anomaly(rates["flap_rate_1h"], base_flap, flap_ratio, flap_min)
    conn_anom = is_anomaly(rates["unavail_rate_1h"], base_unavail, conn_ratio, conn_min)
    return {
        "rates": rates,
        "health": flap_anom or conn_anom,
        "flap": {
            "due": flap_anom
            and record_event_if_due(conn, meta_id, "flap_anomaly", "flap", now_ts, debounce_s),
            "rate": rates["flap_rate_1h"],
            "baseline": base_flap,
        },
        "connection": {
            "due": conn_anom
            and record_event_if_due(
                conn, meta_id, "connection_anomaly", "connection", now_ts, debounce_s
            ),
            "rate": rates["unavail_rate_1h"],
            "baseline": base_unavail,
        },
        "wear_critical": _wear_critical(
            conn, meta_id, now_ts, rated_hours, wear_thresholds, wear_debounce_s
        ),
    }
