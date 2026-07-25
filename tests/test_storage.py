"""Storage + registry: schema, atomic write, accrual fold, rename, unique_id.

Sync helpers are tested against an in-memory SQLite seeded from the real
migration; the async writer is exercised end-to-end via asyncio.run. No Home
Assistant required.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

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


def test_wear_sensor_root_platform_null_uses_bare_unique_id():
    """F13(a): a row with a unique_id but platform still NULL returns the bare
    unique_id (the pre-composite root) — so such rows need no migration and mint no
    duplicate sensors. Only once platform is known does it qualify."""
    conn = _seed_conn()
    row = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1, unique_id="uid-1"
    )
    assert row.platform is None
    assert registry.wear_sensor_root(row) == "uid-1"
    # Once platform is backfilled the root becomes the platform-qualified form.
    row = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1,
        unique_id="uid-1", platform="hue",
    )
    assert registry.wear_sensor_root(row) == "hue_light_uid-1"


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


def test_removal_disabled_row_does_not_resume_on_entity_id_only_match():
    """Finding 2: a removed (soft-deleted) row must NOT resume on a bare entity_id
    match — a plain reload upserts the stale entity_id with unique_id=None (the ER
    entry is gone). Only a confirmed identity match may resume, so a freed
    entity_id later claimed by a different device can't resurrect the old row."""
    conn = _seed_conn()
    row = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1,
        unique_id="uid-a", platform="hue",
    )
    registry.set_disabled(conn, "light.a", True, "removed")
    again = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=2)
    assert again.id == row.id  # same row, matched by entity_id
    assert again.disabled is True  # but tracking stays paused
    assert again.disabled_reason == "removed"


def test_removal_disabled_row_retired_when_different_identity_claims_entity_id():
    """F1/F7: when a DIFFERENT identity (different unique_id) claims the freed
    entity_id of a removal-disabled row, the old row is retired to a tombstone
    (history kept, still disabled, identity NOT forged) and the claimant gets a
    fresh, trackable row — rather than the claimant being untrackable (the entity_id
    UNIQUE constraint made the INSERT unreachable) or the old row being resumed."""
    conn = _seed_conn()
    old = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1,
        unique_id="uid-old", platform="hue",
    )
    storage.write_transition_sync(conn, old.id, _transition(StateKind.ON, life=10.0, cycles=1))
    registry.set_disabled(conn, "light.a", True, "removed")

    claimant = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=2,
        unique_id="uid-new", platform="hue",
    )
    assert claimant.id != old.id            # a fresh row, not the old one
    assert claimant.disabled is False       # trackable
    assert claimant.unique_id == "uid-new"
    assert claimant.entity_id == "light.a"

    retired = conn.execute(
        "SELECT entity_id, unique_id, disabled, disabled_reason FROM entity_meta WHERE id = ?",
        (old.id,),
    ).fetchone()
    assert retired["entity_id"].startswith("__wt_retired_")  # tombstone label
    assert retired["unique_id"] == "uid-old"                 # NOT forged to claimant's
    assert retired["disabled"] == 1 and retired["disabled_reason"] == "removed"
    # History intact on the retired row.
    assert conn.execute(
        "SELECT lifetime_cycles FROM summary WHERE entity_meta_id = ?", (old.id,)
    ).fetchone()[0] == 1

    # The claimant is trackable: a fresh transition lands on its own row.
    storage.write_transition_sync(conn, claimant.id, _transition(StateKind.ON, life=3.0, cycles=1))
    assert conn.execute(
        "SELECT lifetime_cycles FROM summary WHERE entity_meta_id = ?", (claimant.id,)
    ).fetchone()[0] == 1


def test_removal_disabled_row_ghost_reload_returns_unchanged():
    """F1/F7: a ghost reload (bare entity_id, no unique_id — the ER entry is gone)
    must still return the removal-disabled row unchanged, not retire it."""
    conn = _seed_conn()
    old = registry.upsert(
        conn, entity_id="light.a", domain="light", tracking_since=1,
        unique_id="uid-old", platform="hue",
    )
    registry.set_disabled(conn, "light.a", True, "removed")
    again = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=2)
    assert again.id == old.id
    assert again.disabled is True and again.disabled_reason == "removed"
    assert again.entity_id == "light.a"  # not retired to a tombstone


def test_null_platform_fallback_does_not_steal_live_entitys_legacy_row():
    """Finding 3: a legacy (platform NULL) row must not be seized by a different
    entity that merely shares its unique_id string. With the full tracked set
    known, the fallback is refused when the row's entity_id belongs to another
    live entity, so the rightful owner keeps its history."""
    conn = _seed_conn()
    # Pre-v5 row for the entity currently at switch.a (platform never backfilled).
    legacy = registry.upsert(
        conn, entity_id="switch.a", domain="switch", tracking_since=1, unique_id="abc"
    )
    known = {"switch.a", "switch.b"}
    # A different device sharing the unique_id string upserts first.
    other = registry.upsert(
        conn, entity_id="switch.b", domain="switch", tracking_since=1,
        unique_id="abc", platform="tplink", known_entity_ids=known,
    )
    assert other.id != legacy.id  # got its own fresh row, not the legacy one
    assert registry.load_by_entity_id(conn, "switch.a").id == legacy.id
    assert registry.load_by_entity_id(conn, "switch.a").platform is None  # untouched
    # The rightful owner later upserts and reclaims its legacy row: its entity_id
    # matches the row's, so the guarded fallback accepts it.
    owner = registry.upsert(
        conn, entity_id="switch.a", domain="switch", tracking_since=2,
        unique_id="abc", platform="acme", known_entity_ids=known,
    )
    assert owner.id == legacy.id
    assert owner.platform == "acme"


def test_reconcile_rename_finds_legacy_null_platform_row_in_swap():
    """Finding 4: a pre-v5 (platform NULL) row must be found during a rename even
    when the caller supplies platform+domain, so an A<->B swap doesn't strand one
    row on a sentinel forever."""
    conn = _seed_conn()
    # Legacy rows: unique_id + entity_id known, platform never backfilled.
    a = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1, unique_id="uid-a")
    b = registry.upsert(conn, entity_id="light.b", domain="light", tracking_since=1, unique_id="uid-b")
    # Swap delivered as two events, each carrying the now-known platform+domain.
    registry.reconcile_rename(conn, "light.a", "light.b", "uid-a", "hue", "light")
    registry.reconcile_rename(conn, "light.b", "light.a", "uid-b", "hue", "light")
    assert registry.load_by_unique_id(conn, "uid-a").entity_id == "light.b"
    assert registry.load_by_unique_id(conn, "uid-b").entity_id == "light.a"
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


def test_reset_clears_wear_critical_keeps_anomaly_debounce():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    storage.write_transition_sync(conn, row.id, _transition(StateKind.ON, life=10.0, cycles=1))
    conn.execute(
        "INSERT INTO events_fired (entity_meta_id, event_kind, discriminator, fired_ts)"
        " VALUES (?, 'wear_critical', 'hours:90', 1500)",
        (row.id,),
    )
    # An in-progress flap/connection anomaly debounce that must survive the reset.
    conn.execute(
        "INSERT INTO events_fired (entity_meta_id, event_kind, discriminator, fired_ts)"
        " VALUES (?, 'flap_anomaly', 'flap', 1500), (?, 'connection_anomaly', 'connection', 1500)",
        (row.id, row.id),
    )
    # wear_critical is cleared even with keep_history so it can re-arm for a replaced
    # device, but the anomaly debounce rows stay so ongoing flaps don't re-fire.
    storage.reset_summary_sync(conn, row.id, keep_history=True, ts=2000)
    kinds = {
        r[0]
        for r in conn.execute(
            "SELECT event_kind FROM events_fired WHERE entity_meta_id=?", (row.id,)
        ).fetchall()
    }
    assert kinds == {"flap_anomaly", "connection_anomaly"}


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


def test_fold_does_not_resurrect_reset_day_pre_reset_counters():
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    day_start = int(datetime(2026, 7, 10, tzinfo=timezone.utc).timestamp())
    pre_reset_ts = day_start + 8 * 3600    # 08:00, before the reset
    reset_ts = day_start + 15 * 3600       # 15:00 reset
    post_reset_ts = day_start + 16 * 3600  # 16:00, after the reset, same day
    # Live-accumulated summary before the reset, plus the transition that made it.
    conn.execute(
        "INSERT INTO summary (entity_meta_id, lifetime_seconds, connected_seconds,"
        " lifetime_cycles, connection_drops, updated_ts) VALUES (?, 1000.0, 1000.0, 0, 1, ?)",
        (row.id, pre_reset_ts),
    )
    _raw_transition(conn, row.id, pre_reset_ts, "ON", "DISCONNECTED", 1000.0)
    storage.reset_summary_sync(conn, row.id, keep_history=True, ts=reset_ts)
    # A legitimate post-reset on-period on the same calendar day as the reset.
    _raw_transition(conn, row.id, post_reset_ts, "ON", "OFF", 500.0)
    # Retention fold ~90 days later: pre-reset rows are purged, not folded, so the
    # reset-day daily_summary row only ever holds post-reset amounts.
    cutoff = day_start + 100 * 24 * 3600
    storage.fold_and_purge_sync(conn, cutoff_ts=cutoff, debounce_s=2.0)
    assert conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] == 0
    daily = conn.execute(
        "SELECT on_seconds, drops FROM daily_summary WHERE entity_meta_id=? AND day='2026-07-10'",
        (row.id,),
    ).fetchone()
    assert daily["on_seconds"] == 500.0  # only the post-reset period, not 1500
    assert daily["drops"] == 0           # the pre-reset drop was purged, not folded
    new = storage.recompute_summary_sync(conn, row.id, 2.0)
    assert new["lifetime_seconds"] == 500.0
    assert new["connection_drops"] == 0


def test_fold_folds_prior_days_but_not_reset_day_pre_reset():
    """F2: keep_history's permanent daily archive for days BEFORE the reset day must
    still be folded; only reset-day-morning pre-reset rows are excluded from folding
    (they would let recompute's day>=reset_day filter resurrect pre-reset counters).
    Prior-day folds are kept for audit but recompute (gated at reset_ts) ignores
    them, so a reset still can't be resurrected."""
    conn = _seed_conn()
    row = registry.upsert(conn, entity_id="light.a", domain="light", tracking_since=1)
    day_start = int(datetime(2026, 7, 10, tzinfo=timezone.utc).timestamp())
    prior_day_ts = int(datetime(2026, 7, 8, 10, tzinfo=timezone.utc).timestamp())
    reset_morning_ts = day_start + 8 * 3600    # 08:00 reset day, pre-reset
    reset_ts = day_start + 15 * 3600           # 15:00 reset
    post_reset_ts = day_start + 16 * 3600      # 16:00 reset day, post-reset
    # A summary row must exist for reset_summary_sync to stamp reset_ts onto.
    conn.execute(
        "INSERT INTO summary (entity_meta_id, lifetime_seconds, connected_seconds,"
        " lifetime_cycles, connection_drops, updated_ts) VALUES (?, 0, 0, 0, 0, ?)",
        (row.id, prior_day_ts),
    )
    _raw_transition(conn, row.id, prior_day_ts, "ON", "OFF", 700.0)              # prior day -> fold
    _raw_transition(conn, row.id, reset_morning_ts, "ON", "DISCONNECTED", 300.0)  # reset day am -> NOT fold
    storage.reset_summary_sync(conn, row.id, keep_history=True, ts=reset_ts)
    _raw_transition(conn, row.id, post_reset_ts, "ON", "OFF", 500.0)             # reset day pm -> fold
    cutoff = day_start + 100 * 24 * 3600
    storage.fold_and_purge_sync(conn, cutoff_ts=cutoff, debounce_s=2.0)
    assert conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0] == 0

    prior = conn.execute(
        "SELECT on_seconds FROM daily_summary WHERE entity_meta_id=? AND day='2026-07-08'",
        (row.id,),
    ).fetchone()
    assert prior is not None and prior["on_seconds"] == 700.0  # prior-day archive kept

    reset_day = conn.execute(
        "SELECT on_seconds, drops FROM daily_summary WHERE entity_meta_id=? AND day='2026-07-10'",
        (row.id,),
    ).fetchone()
    assert reset_day["on_seconds"] == 500.0  # post-reset only, morning 300s excluded
    assert reset_day["drops"] == 0           # the reset-morning drop was purged, not folded

    new = storage.recompute_summary_sync(conn, row.id, 2.0)
    assert new["lifetime_seconds"] == 500.0  # prior-day fold not resurrected
    assert new["connection_drops"] == 0


def test_failed_migration_rolls_back_and_releases_lock(tmp_path, monkeypatch):
    """F8: a migration that raises mid-script must roll back its own BEGIN and close
    the connection, so a ConfigEntryNotReady retry (a fresh writer on the same DB)
    surfaces the real error, not 'database is locked' from a leaked write lock."""
    db = tmp_path / "wear.db"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "01_broken.sql").write_text(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);\n"
        "CREATE TABLE good (id INTEGER);\n"
        "INSERT INTO does_not_exist (x) VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(storage, "MIGRATIONS_DIR", migrations)

    async def scenario():
        w1 = storage.AsyncSqliteWriter(db)
        err1 = None
        try:
            await w1.open()
        except Exception as e:  # noqa: BLE001
            err1 = e
        # The failed open closed its connection (released the write lock).
        assert w1._conn is None
        # Open a fresh writer on the same DB BEFORE closing w1, so a leaked lock (if
        # the fix regressed) would actually collide here.
        w2 = storage.AsyncSqliteWriter(db)
        err2 = None
        try:
            await w2.open()
        except Exception as e:  # noqa: BLE001
            err2 = e
        await w1.close()
        await w2.close()
        return err1, err2

    err1, err2 = asyncio.run(scenario())
    assert err1 is not None and "database is locked" not in str(err1).lower()
    assert err2 is not None and "database is locked" not in str(err2).lower()


def test_open_leaves_foreign_keys_on(tmp_path):
    """F8: the runner disables FK during table-rebuild migrations (04/05); the
    finally must restore enforcement (not be silently skipped inside an open
    transaction) so live cascades (purge) work afterwards."""
    async def scenario():
        writer = storage.AsyncSqliteWriter(tmp_path / "wear.db")
        await writer.open()
        try:
            return await writer.run(
                lambda c: c.execute("PRAGMA foreign_keys").fetchone()[0]
            )
        finally:
            await writer.close()

    assert asyncio.run(scenario()) == 1


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


def test_reopen_after_crash_between_apply_and_stamp_does_not_brick(tmp_path):
    """A crash after a migration applies but before schema_version is stamped must
    not brick the next boot (04's ADD COLUMN would otherwise re-run and fail with
    'duplicate column name'). Simulate: apply 04's column then leave version at 3."""
    db = tmp_path / "wear.db"
    # Bring the DB up through migration 03 only, stamped at version 3.
    conn = sqlite3.connect(db, isolation_level=None)
    for sql in sorted((COMP / "migrations").glob("[0-9][0-9]_*.sql")):
        if int(sql.name[:2]) > 3:
            break
        conn.executescript(sql.read_text("utf-8"))
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', '3')"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    )
    # 04 partially applied (column added) but its schema_version stamp never landed.
    conn.execute("ALTER TABLE summary ADD COLUMN reset_ts INTEGER")
    conn.close()

    async def scenario():
        writer = storage.AsyncSqliteWriter(db)
        await writer.open()  # must not raise 'duplicate column name'
        try:
            return await writer.run(
                lambda c: c.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
            )
        finally:
            await writer.close()

    assert asyncio.run(scenario()) == "5"
