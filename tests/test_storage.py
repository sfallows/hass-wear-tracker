"""Storage + registry: schema, atomic write, accrual fold, rename, unique_id.

Sync helpers are tested against an in-memory SQLite seeded from the real
migration; the async writer is exercised end-to-end via asyncio.run. No Home
Assistant required.
"""
from __future__ import annotations

import asyncio
import sqlite3

from _bootstrap import COMP, registry, storage
from _bootstrap import state_machine as sm

StateKind = sm.StateKind
TransitionEvent = sm.TransitionEvent


def _seed_conn():
    # isolation_level=None (autocommit) matches AsyncSqliteWriter._open_sync, which
    # is what lets the storage helpers manage transactions with explicit BEGIN/COMMIT.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    for sql in sorted((COMP / "migrations").glob("[0-9][0-9]_*.sql")):
        conn.executescript(sql.read_text("utf-8"))
    return conn


def _transition(to_state, life=0.0, conn_s=0.0, cycles=0, drops=0, ts=1000):
    return TransitionEvent(
        entity_id="light.a",
        ts=ts,
        from_state=StateKind.OFF,
        to_state=to_state,
        raw_from="off",
        raw_to="on",
        delta_s=life,
        lifetime_seconds_delta=life,
        connected_seconds_delta=conn_s,
        cycles_delta=cycles,
        drops_delta=drops,
    )


# --- registry ---------------------------------------------------------------

def test_upsert_is_idempotent_by_entity_id():
    conn = _seed_conn()
    a = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    b = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=2)
    assert a.id == b.id


def test_upsert_backfills_unique_id():
    conn = _seed_conn()
    registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    row = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1, unique_id="abc"
    )
    assert row.unique_id == "abc"


def test_upsert_follows_unique_id_across_rename_while_down():
    conn = _seed_conn()
    first = registry.upsert(
        conn, entity_id="light.old", domain="light", tracking_since=1, unique_id="uid-1"
    )
    # Same hardware, new entity_id, seen for the first time after a rename downtime.
    again = registry.upsert(
        conn, entity_id="light.new", domain="light", tracking_since=1, unique_id="uid-1"
    )
    assert again.id == first.id
    assert again.entity_id == "light.new"
    assert registry.load_by_entity_id(conn, "light.old") is None


def test_upsert_matches_legacy_null_platform_row_after_rename():
    """A pre-v5 row (platform NULL until first backfill) that was renamed during
    downtime must still be matched by identity, not forked into a new row."""
    conn = _seed_conn()
    legacy = registry.upsert(
        conn, entity_id="light.old", domain="light", tracking_since=1, unique_id="uid-1"
    )
    again = registry.upsert(
        conn, entity_id="light.new", domain="light", tracking_since=1,
        unique_id="uid-1", platform="hue",
    )
    assert again.id == legacy.id
    assert again.entity_id == "light.new"
    assert again.platform == "hue"
    assert conn.execute("SELECT COUNT(*) FROM entity_meta").fetchone()[0] == 1


def test_reconcile_rename_moves_label_and_keeps_id():
    conn = _seed_conn()
    before = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1, unique_id="uid-a"
    )
    moves = registry.reconcile_rename(conn, "light.a", "light.b", "uid-a")
    after = registry.load_by_entity_id(conn, "light.b")
    assert moves == [("light.a", "light.b")]
    assert after is not None and after.id == before.id


def test_reconcile_rename_falls_back_to_entity_id_without_uid():
    conn = _seed_conn()
    before = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    moves = registry.reconcile_rename(conn, "light.a", "light.b", None)
    assert moves == [("light.a", "light.b")]
    assert registry.load_by_entity_id(conn, "light.b").id == before.id


def test_reconcile_rename_noop_when_already_correct():
    conn = _seed_conn()
    registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1, unique_id="uid-a"
    )
    assert registry.reconcile_rename(conn, "light.a", "light.a", "uid-a") == []


def test_same_unique_id_across_platforms_stays_two_entities():
    """A unique_id is only unique per (platform, domain); two entities that share
    one must not merge into a single entity_meta row (registry.py finding)."""
    conn = _seed_conn()
    a = registry.upsert(
        conn, entity_id="switch.plug", domain="switch", tracking_since=1,
        unique_id="abc", platform="tplink",
    )
    b = registry.upsert(
        conn, entity_id="light.lamp", domain="light", tracking_since=1,
        unique_id="abc", platform="hue",
    )
    assert a.id != b.id
    assert conn.execute("SELECT COUNT(*) FROM entity_meta").fetchone()[0] == 2
    assert registry.load_by_entity_id(conn, "switch.plug").id == a.id
    assert registry.load_by_entity_id(conn, "light.lamp").id == b.id
    # And their sensor roots differ, so their sensor unique_ids can't collide.
    assert registry.wear_sensor_root(a) != registry.wear_sensor_root(b)


def test_wear_sensor_root_falls_back_to_entity_id_without_unique_id():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="fan.attic", domain="fan", tracking_since=1)
    assert registry.wear_sensor_root(row) == "fan.attic"


def test_removal_disabled_row_resumes_on_reupsert():
    """DESIGN §4: a soft-deleted (removed) row resumes tracking when the same
    unique_id re-registers (Zigbee re-pair)."""
    conn = _seed_conn()
    row = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1,
        unique_id="uid-a", platform="hue",
    )
    registry.set_disabled(conn, "light.a", True, "removed")
    assert registry.load_by_entity_id(conn, "light.a").disabled is True

    again = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=2,
        unique_id="uid-a", platform="hue",
    )
    assert again.id == row.id  # same history row
    assert again.disabled is False
    assert again.disabled_reason is None


def test_user_disabled_row_stays_disabled_on_reupsert():
    """A deliberate wear_tracker.disable stays sticky across a restart/reload."""
    conn = _seed_conn()
    row = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1,
        unique_id="uid-a", platform="hue",
    )
    registry.set_disabled(conn, "light.a", True, "user")
    again = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=2,
        unique_id="uid-a", platform="hue",
    )
    assert again.id == row.id
    assert again.disabled is True
    assert again.disabled_reason == "user"


def test_reconcile_rename_disambiguates_shared_unique_id():
    """When two rows share a unique_id across platforms, a rename must move the row
    matching the full (platform, domain, unique_id), not the first uid match."""
    conn = _seed_conn()
    a = registry.upsert(
        conn, entity_id="switch.plug", domain="switch", tracking_since=1,
        unique_id="abc", platform="tplink",
    )
    b = registry.upsert(
        conn, entity_id="light.lamp", domain="light", tracking_since=1,
        unique_id="abc", platform="hue",
    )
    moves = registry.reconcile_rename(
        conn, "light.lamp", "light.lamp2", "abc", "hue", "light"
    )
    assert moves == [("light.lamp", "light.lamp2")]
    assert registry.load_by_entity_id(conn, "light.lamp2").id == b.id
    assert registry.load_by_entity_id(conn, "switch.plug").id == a.id  # untouched


def test_reconcile_rename_handles_simultaneous_swap():
    """A<->B swap arrives as two events; history must follow each device."""
    conn = _seed_conn()
    a = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1, unique_id="uid-a"
    )
    b = registry.upsert(
        conn, entity_id="light.b", domain="light", tracking_since=1, unique_id="uid-b"
    )
    # Event 1: the device formerly at light.a (uid-a) is now at light.b.
    registry.reconcile_rename(conn, "light.a", "light.b", "uid-a")
    # Event 2: the device formerly at light.b (uid-b) is now at light.a.
    registry.reconcile_rename(conn, "light.b", "light.a", "uid-b")

    assert registry.load_by_unique_id(conn, "uid-a").entity_id == "light.b"
    assert registry.load_by_unique_id(conn, "uid-b").entity_id == "light.a"
    # Surrogate ids (and therefore history) are unchanged.
    assert registry.load_by_entity_id(conn, "light.b").id == a.id
    assert registry.load_by_entity_id(conn, "light.a").id == b.id


# --- summary write / accrual ------------------------------------------------

def test_write_transition_accumulates_summary():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=10.0, cycles=1))
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=5.0, cycles=1))
    summary = storage.load_summary_sync(conn, row.id)
    assert summary["lifetime_seconds"] == 15.0
    assert summary["lifetime_cycles"] == 2
    assert summary["last_state"] == "ON"


def test_apply_accruals_adds_without_transition_rows():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=0.0))
    before = conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
    storage.apply_accruals_sync(conn, [(row.id, 30.0, 30.0)], ts=2000)
    after = conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]
    summary = storage.load_summary_sync(conn, row.id)
    assert after == before  # no new transition rows
    assert summary["lifetime_seconds"] == 30.0
    assert summary["connected_seconds"] == 30.0
    assert summary["updated_ts"] == 2000


def test_apply_accruals_empty_is_noop():
    conn = _seed_conn()
    storage.apply_accruals_sync(conn, [], ts=1)  # must not raise


# --- async writer end to end ------------------------------------------------

def test_async_writer_roundtrip(tmp_path):
    async def scenario():
        writer = storage.AsyncSqliteWriter(tmp_path / "wear.db")
        await writer.open()
        try:
            row = await writer.run(
                lambda c: registry.upsert(c, entity_id="light.a", domain="light", tracking_since=1)
            )
            await writer.write_transition(row.id, _transition(StateKind.ON, life=12.0, cycles=1))
            await writer.apply_accruals([(row.id, 8.0, 20.0)], 1234)
            await writer.heartbeat(5555)
            summary = await writer.load_summary(row.id)
            last_alive = await writer.load_last_alive()
            return summary, last_alive
        finally:
            await writer.close()

    summary, last_alive = asyncio.run(scenario())
    assert summary["lifetime_seconds"] == 20.0  # 12 + 8
    assert summary["connected_seconds"] == 20.0
    assert last_alive == 5555


def _raw_transition(conn, meta_id, ts, from_state, to_state, delta_s):
    conn.execute(
        "INSERT INTO transitions (entity_meta_id, ts, from_state, to_state, raw_from, raw_to, delta_s)"
        " VALUES (?, ?, ?, ?, '', '', ?)",
        (meta_id, ts, from_state, to_state, delta_s),
    )


def test_recompute_replays_transitions():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=0.0))
    _raw_transition(conn, row.id, 100, "OFF", "ON", 9.0)   # off-period 9s -> a cycle
    _raw_transition(conn, row.id, 200, "ON", "OFF", 50.0)  # on-period 50s -> lifetime
    _raw_transition(conn, row.id, 300, "OFF", "DISCONNECTED", 30.0)  # a drop
    new = storage.recompute_summary_sync(conn, row.id, 2.0)
    assert new["lifetime_seconds"] == 50.0
    assert new["lifetime_cycles"] == 1
    assert new["connection_drops"] == 1


def test_recompute_never_decreases():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    # Inflated existing summary, no transitions to back it up.
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=999.0))
    new = storage.recompute_summary_sync(conn, row.id, 2.0)
    assert new["lifetime_seconds"] == 999.0  # floored at the prior value


def test_fold_and_purge_moves_old_transitions_to_daily_summary():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    old = 1_600_000_000  # well in the past
    _raw_transition(conn, row.id, old, "ON", "OFF", 40.0)
    _raw_transition(conn, row.id, old + 10, "OFF", "ON", 5.0)
    purged = storage.fold_and_purge_sync(conn, cutoff_ts=old + 100, debounce_s=2.0)
    assert purged == 2
    assert conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] == 0
    daily = conn.execute(
        "SELECT SUM(on_seconds) s, SUM(cycles) c FROM daily_summary WHERE entity_meta_id=?",
        (row.id,),
    ).fetchone()
    assert daily["s"] == 40.0
    assert daily["c"] == 1


def test_reset_zeroes_summary_and_drops_history():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=10.0, cycles=1))
    prior = storage.reset_summary_sync(conn, row.id, keep_history=False, ts=2000)
    assert prior["lifetime_seconds"] == 10.0
    summary = storage.load_summary_sync(conn, row.id)
    assert summary["lifetime_seconds"] == 0.0 and summary["lifetime_cycles"] == 0
    assert conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] == 0


def test_reset_clears_events_fired():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=10.0, cycles=1))
    conn.execute(
        "INSERT INTO events_fired (entity_meta_id, event_kind, discriminator, fired_ts)"
        " VALUES (?, 'wear_critical', 'hours:90', 1500)",
        (row.id,),
    )
    # Cleared even with keep_history so wear_critical can re-arm for a replaced device.
    storage.reset_summary_sync(conn, row.id, keep_history=True, ts=2000)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM events_fired WHERE entity_meta_id=?", (row.id,)
    ).fetchone()[0]
    assert remaining == 0


def test_recompute_after_keep_history_reset_does_not_resurrect():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=50.0, cycles=1))
    _raw_transition(conn, row.id, 300, "OFF", "DISCONNECTED", 30.0)  # a pre-reset drop
    prior = storage.reset_summary_sync(conn, row.id, keep_history=True, ts=2000)
    assert prior["lifetime_seconds"] == 50.0
    # History (all ts < 2000) is retained but must not be replayed into the summary.
    assert conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] > 0
    new = storage.recompute_summary_sync(conn, row.id, 2.0)
    assert new["lifetime_seconds"] == 0.0
    assert new["lifetime_cycles"] == 0
    assert new["connection_drops"] == 0


def test_recompute_ignores_restart_seed_disconnect_rows():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=0.0))
    _raw_transition(conn, row.id, 100, "ON", "DISCONNECTED", 5.0)  # one real drop
    # Three restart-while-unavailable seed rows (D->D, zero delta) — not drops.
    _raw_transition(conn, row.id, 200, "DISCONNECTED", "DISCONNECTED", 0.0)
    _raw_transition(conn, row.id, 300, "DISCONNECTED", "DISCONNECTED", 0.0)
    _raw_transition(conn, row.id, 400, "DISCONNECTED", "DISCONNECTED", 0.0)
    new = storage.recompute_summary_sync(conn, row.id, 2.0)
    assert new["connection_drops"] == 1


def test_fold_ignores_restart_seed_disconnect_rows():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    old = 1_600_000_000
    _raw_transition(conn, row.id, old, "ON", "DISCONNECTED", 5.0)  # one real drop
    _raw_transition(conn, row.id, old + 1, "DISCONNECTED", "DISCONNECTED", 0.0)  # seed
    _raw_transition(conn, row.id, old + 2, "DISCONNECTED", "DISCONNECTED", 0.0)  # seed
    purged = storage.fold_and_purge_sync(conn, cutoff_ts=old + 100, debounce_s=2.0)
    assert purged == 3
    daily = conn.execute(
        "SELECT SUM(drops) d FROM daily_summary WHERE entity_meta_id=?", (row.id,)
    ).fetchone()
    assert daily["d"] == 1


def test_purge_entity_removes_all_rows():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=10.0))
    storage.purge_entity_sync(conn, row.id)
    assert registry.load_by_entity_id(conn, "light.a") is None
    assert conn.execute("SELECT COUNT(*) FROM summary WHERE entity_meta_id=?", (row.id,)).fetchone()[0] == 0


def test_migrations_set_schema_version(tmp_path):
    async def scenario():
        writer = storage.AsyncSqliteWriter(tmp_path / "wear.db")
        await writer.open()
        try:
            return await writer.run(
                lambda c: c.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
            )
        finally:
            await writer.close()

    assert asyncio.run(scenario()) == "5"
