"""Async wrapper over the wear_tracker SQLite store.

All operations run on a dedicated single-worker thread so that writes are
serialized and the asyncio event loop is never blocked on disk. WAL mode
lets concurrent reads on other connections proceed without blocking writes.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, TypeVar

from .state_machine import TransitionEvent

_LOG = logging.getLogger(__name__)
_T = TypeVar("_T")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class AsyncSqliteWriter:
    """Owns the SQLite connection. Run callables on its single worker thread."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="wear_tracker_db"
        )
        self._conn: sqlite3.Connection | None = None
        self._closed = False

    async def open(self) -> None:
        await asyncio.get_running_loop().run_in_executor(
            self._executor, self._open_sync
        )

    def _open_sync(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self._db_path, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        self._conn = conn
        try:
            self._apply_migrations_sync()
        except Exception:
            # A failed migration must not leak the open connection: it holds the
            # write lock, so a ConfigEntryNotReady retry (a fresh writer on the same
            # DB file) would fail with 'database is locked' instead of surfacing the
            # real error. Close it here so the lock is released before we re-raise.
            conn.close()
            self._conn = None
            raise

    def _apply_migrations_sync(self) -> None:
        assert self._conn is not None
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        )
        if cur.fetchone() is None:
            current_version = 0
        else:
            cur = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            )
            row = cur.fetchone()
            current_version = int(row[0]) if row else 0

        migrations = sorted(MIGRATIONS_DIR.glob("[0-9][0-9]_*.sql"))
        pending = [p for p in migrations if int(p.name[:2]) > current_version]
        if not pending:
            return
        # Disable FK enforcement while migrating: a table-rebuild migration must be
        # able to DROP the old parent table without cascade-deleting history rows.
        # Toggle in autocommit (a PRAGMA is a no-op inside a transaction), then
        # restore ON so live cascades (purge) work. Surrogate ids are preserved
        # across rebuilds so the restored constraint stays satisfied.
        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for path in pending:
                num = int(path.name[:2])
                _LOG.info("wear_tracker: applying migration %s", path.name)
                # Apply the migration and stamp schema_version in one transaction so
                # a crash between the two can't leave a half-applied version that
                # re-runs (and fails) on the next boot. executescript adds no
                # transaction control of its own, so the migration files carry none
                # and the wrapping BEGIN/COMMIT is supplied here.
                try:
                    self._conn.executescript(
                        "BEGIN;\n"
                        + path.read_text("utf-8")
                        + "\nINSERT INTO meta(key, value) VALUES "
                        f"('schema_version', '{num}')\n"
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value;\n"
                        "COMMIT;\n"
                    )
                except Exception:
                    # A mid-script failure leaves the explicit BEGIN open. Roll it
                    # back before re-raising, otherwise the finally's PRAGMA (a no-op
                    # inside a transaction) is silently dropped and the connection
                    # holds the write lock.
                    if self._conn.in_transaction:
                        self._conn.execute("ROLLBACK")
                    raise
        finally:
            # Defensive: never restore FK enforcement while a transaction is still
            # open (the PRAGMA would be silently ignored there).
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            self._conn.execute("PRAGMA foreign_keys = ON")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.get_running_loop().run_in_executor(
            self._executor, self._close_sync
        )
        self._executor.shutdown(wait=True)

    def _close_sync(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    async def run(self, func: Callable[[sqlite3.Connection], _T]) -> _T:
        """Execute `func(connection)` on the writer thread."""
        return await asyncio.get_running_loop().run_in_executor(
            self._executor, self._call, func
        )

    def _call(self, func: Callable[[sqlite3.Connection], _T]) -> _T:
        if self._conn is None:
            raise RuntimeError("AsyncSqliteWriter is not open")
        return func(self._conn)

    async def write_transition(
        self, entity_meta_id: int, event: TransitionEvent
    ) -> None:
        await self.run(
            lambda conn: write_transition_sync(conn, entity_meta_id, event)
        )

    async def load_summary(self, entity_meta_id: int) -> dict[str, Any] | None:
        return await self.run(
            lambda conn: load_summary_sync(conn, entity_meta_id)
        )

    async def apply_accruals(
        self, accruals: list[tuple[int, float, float]], ts: int
    ) -> None:
        await self.run(lambda conn: apply_accruals_sync(conn, accruals, ts))

    async def heartbeat(self, ts: int) -> None:
        await self.run(lambda conn: _heartbeat_sync(conn, ts))

    async def load_last_alive(self) -> int | None:
        return await self.run(_load_last_alive_sync)


def write_transition_sync(
    conn: sqlite3.Connection,
    entity_meta_id: int,
    event: TransitionEvent,
) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute(
            """
            INSERT INTO transitions (
                entity_meta_id, ts, from_state, to_state,
                raw_from, raw_to, delta_s
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_meta_id,
                event.ts,
                str(event.from_state),
                str(event.to_state),
                event.raw_from,
                event.raw_to,
                event.delta_s,
            ),
        )
        conn.execute(
            """
            INSERT INTO summary (
                entity_meta_id, lifetime_seconds, connected_seconds,
                lifetime_cycles, connection_drops,
                last_state, last_change_ts, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_meta_id) DO UPDATE SET
                lifetime_seconds  = lifetime_seconds  + excluded.lifetime_seconds,
                connected_seconds = connected_seconds + excluded.connected_seconds,
                lifetime_cycles   = lifetime_cycles   + excluded.lifetime_cycles,
                connection_drops  = connection_drops  + excluded.connection_drops,
                last_state        = excluded.last_state,
                last_change_ts    = excluded.last_change_ts,
                updated_ts        = excluded.updated_ts
            """,
            (
                entity_meta_id,
                event.lifetime_seconds_delta,
                event.connected_seconds_delta,
                event.cycles_delta,
                event.drops_delta,
                str(event.to_state),
                event.ts,
                event.ts,
            ),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def apply_accruals_sync(
    conn: sqlite3.Connection,
    accruals: list[tuple[int, float, float]],
    ts: int,
) -> None:
    """Fold in-progress on/connected time into `summary`; no transition rows.

    Each tuple is `(entity_meta_id, lifetime_seconds_delta, connected_seconds_delta)`.
    `last_state` / `last_change_ts` are left untouched — no state change occurred.
    """
    if not accruals:
        return
    conn.execute("BEGIN")
    try:
        for entity_meta_id, lifetime_delta, connected_delta in accruals:
            conn.execute(
                """
                UPDATE summary SET
                    lifetime_seconds  = lifetime_seconds  + ?,
                    connected_seconds = connected_seconds + ?,
                    updated_ts        = ?
                WHERE entity_meta_id = ?
                """,
                (lifetime_delta, connected_delta, ts, entity_meta_id),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def load_summary_sync(
    conn: sqlite3.Connection, entity_meta_id: int
) -> dict[str, Any] | None:
    cur = conn.execute(
        """
        SELECT lifetime_seconds, connected_seconds, lifetime_cycles,
               connection_drops, last_state, last_change_ts, updated_ts
        FROM summary WHERE entity_meta_id = ?
        """,
        (entity_meta_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _heartbeat_sync(conn: sqlite3.Connection, ts: int) -> None:
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES ('last_alive', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(ts),),
    )


def _load_last_alive_sync(conn: sqlite3.Connection) -> int | None:
    cur = conn.execute("SELECT value FROM meta WHERE key = 'last_alive'")
    row = cur.fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


# --- v0.4: services (reset / set_rated / recompute / purge) + retention -----

def write_audit_sync(
    conn: sqlite3.Connection,
    entity_meta_id: int | None,
    action: str,
    payload_json: str | None,
    ts: int,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (entity_meta_id, action, ts, payload) VALUES (?, ?, ?, ?)",
        (entity_meta_id, action, ts, payload_json),
    )


def reset_summary_sync(
    conn: sqlite3.Connection, entity_meta_id: int, keep_history: bool, ts: int
) -> dict[str, Any] | None:
    """Zero the summary (the one legitimate counter decrease). Optionally drop
    history. Returns the prior summary for the audit log."""
    prior = load_summary_sync(conn, entity_meta_id)
    conn.execute("BEGIN")
    try:
        conn.execute(
            """
            UPDATE summary SET lifetime_seconds = 0, connected_seconds = 0,
                lifetime_cycles = 0, connection_drops = 0,
                last_state = NULL, last_change_ts = NULL,
                reset_ts = ?, updated_ts = ?
            WHERE entity_meta_id = ?
            """,
            (ts, ts, entity_meta_id),
        )
        # Re-arm wear_critical only: its debounce is effectively infinite, so a stale
        # fired_ts would suppress the alert for the replaced device. Leave the
        # flap/connection anomaly debounce rows intact so an in-progress flap doesn't
        # immediately re-fire on the first rollup after a reset.
        conn.execute(
            "DELETE FROM events_fired WHERE entity_meta_id = ? AND event_kind = 'wear_critical'",
            (entity_meta_id,),
        )
        if not keep_history:
            conn.execute("DELETE FROM transitions WHERE entity_meta_id = ?", (entity_meta_id,))
            conn.execute("DELETE FROM daily_summary WHERE entity_meta_id = ?", (entity_meta_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return prior


def set_rated_sync(
    conn: sqlite3.Connection,
    entity_meta_id: int,
    rated_hours: float | None,
    rated_cycles: int | None,
) -> None:
    sets, params = [], []
    if rated_hours is not None:
        sets.append("rated_hours = ?")
        params.append(rated_hours)
    if rated_cycles is not None:
        sets.append("rated_cycles = ?")
        params.append(rated_cycles)
    if sets:
        params.append(entity_meta_id)
        conn.execute(f"UPDATE entity_meta SET {', '.join(sets)} WHERE id = ?", params)


def recompute_summary_sync(
    conn: sqlite3.Connection, entity_meta_id: int, debounce_s: float
) -> dict[str, Any]:
    """Rebuild summary from daily_summary + remaining transitions. Never lowers a
    counter below its current value (a total_increasing series mustn't drop).
    History before the entity's last reset is ignored so a reset survives recompute."""
    reset_row = conn.execute(
        "SELECT COALESCE(reset_ts, 0) FROM summary WHERE entity_meta_id = ?",
        (entity_meta_id,),
    ).fetchone()
    reset_ts = reset_row[0] if reset_row else 0
    daily = conn.execute(
        """
        SELECT COALESCE(SUM(on_seconds), 0) AS life, COALESCE(SUM(connected_seconds), 0) AS conn,
               COALESCE(SUM(cycles), 0) AS cyc, COALESCE(SUM(drops), 0) AS drops
        FROM daily_summary WHERE entity_meta_id = ?
          AND day >= strftime('%Y-%m-%d', ?, 'unixepoch')
        """,
        (entity_meta_id, reset_ts),
    ).fetchone()
    life = daily["life"] + conn.execute(
        "SELECT COALESCE(SUM(delta_s), 0) FROM transitions WHERE entity_meta_id = ? AND from_state = 'ON' AND ts >= ?",
        (entity_meta_id, reset_ts),
    ).fetchone()[0]
    connected = daily["conn"] + conn.execute(
        "SELECT COALESCE(SUM(delta_s), 0) FROM transitions WHERE entity_meta_id = ? AND from_state != 'DISCONNECTED' AND ts >= ?",
        (entity_meta_id, reset_ts),
    ).fetchone()[0]
    cycles = daily["cyc"] + conn.execute(
        "SELECT COUNT(*) FROM transitions WHERE entity_meta_id = ? AND to_state = 'ON' "
        "AND from_state IN ('OFF', 'DISCONNECTED') AND delta_s >= ? AND ts >= ?",
        (entity_meta_id, debounce_s, reset_ts),
    ).fetchone()[0]
    # from_state != 'DISCONNECTED' excludes restart seed rows (D->D, drops_delta=0),
    # which the live counter never counts as a connection drop.
    drops = daily["drops"] + conn.execute(
        "SELECT COUNT(*) FROM transitions WHERE entity_meta_id = ? AND to_state = 'DISCONNECTED' "
        "AND from_state != 'DISCONNECTED' AND ts >= ?",
        (entity_meta_id, reset_ts),
    ).fetchone()[0]
    current = load_summary_sync(conn, entity_meta_id) or {}
    new = {
        "lifetime_seconds": max(life, current.get("lifetime_seconds", 0.0)),
        "connected_seconds": max(connected, current.get("connected_seconds", 0.0)),
        "lifetime_cycles": max(int(cycles), int(current.get("lifetime_cycles", 0))),
        "connection_drops": max(int(drops), int(current.get("connection_drops", 0))),
    }
    conn.execute(
        """
        UPDATE summary SET lifetime_seconds = ?, connected_seconds = ?,
            lifetime_cycles = ?, connection_drops = ? WHERE entity_meta_id = ?
        """,
        (
            new["lifetime_seconds"],
            new["connected_seconds"],
            new["lifetime_cycles"],
            new["connection_drops"],
            entity_meta_id,
        ),
    )
    return new


def fold_and_purge_sync(
    conn: sqlite3.Connection, cutoff_ts: int, debounce_s: float
) -> int:
    """Fold transitions older than cutoff into daily_summary, then delete them.
    Returns the number of transition rows purged."""
    conn.execute("BEGIN")
    try:
        # Only the reset DAY is dangerous to fold: recompute's transition replay is
        # gated on ts >= reset_ts, but its daily_summary sum is gated on day >=
        # reset_day, so a folded reset-day-morning row (ts < reset_ts, same day)
        # would resurrect pre-reset counters. Prior days are the permanent archive
        # keep_history exists to protect and must still fold. So exclude from folding
        # only rows with ts < reset_ts that fall ON the reset day; fold everything
        # else. (These rows are still purged below.)
        rows = conn.execute(
            """
            SELECT t.entity_meta_id AS entity_meta_id,
                   strftime('%Y-%m-%d', t.ts, 'unixepoch') AS day,
                   COALESCE(SUM(CASE WHEN t.from_state = 'ON' THEN t.delta_s ELSE 0 END), 0) AS on_seconds,
                   COALESCE(SUM(CASE WHEN t.from_state != 'DISCONNECTED' THEN t.delta_s ELSE 0 END), 0) AS connected_seconds,
                   COALESCE(SUM(CASE WHEN t.to_state = 'ON' AND t.from_state IN ('OFF', 'DISCONNECTED') AND t.delta_s >= ? THEN 1 ELSE 0 END), 0) AS cycles,
                   COALESCE(SUM(CASE WHEN t.to_state = 'DISCONNECTED' AND t.from_state != 'DISCONNECTED' THEN 1 ELSE 0 END), 0) AS drops
            FROM transitions t
            LEFT JOIN summary s ON s.entity_meta_id = t.entity_meta_id
            WHERE t.ts < ?
              AND (
                  t.ts >= COALESCE(s.reset_ts, 0)
                  OR strftime('%Y-%m-%d', t.ts, 'unixepoch')
                     < strftime('%Y-%m-%d', COALESCE(s.reset_ts, 0), 'unixepoch')
              )
            GROUP BY t.entity_meta_id, day
            """,
            (debounce_s, cutoff_ts),
        ).fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO daily_summary (entity_meta_id, day, on_seconds, connected_seconds, cycles, drops)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_meta_id, day) DO UPDATE SET
                    on_seconds = on_seconds + excluded.on_seconds,
                    connected_seconds = connected_seconds + excluded.connected_seconds,
                    cycles = cycles + excluded.cycles,
                    drops = drops + excluded.drops
                """,
                (row["entity_meta_id"], row["day"], row["on_seconds"],
                 row["connected_seconds"], row["cycles"], row["drops"]),
            )
        purged = conn.execute("DELETE FROM transitions WHERE ts < ?", (cutoff_ts,)).rowcount
        conn.execute("COMMIT")
        return purged
    except Exception:
        conn.execute("ROLLBACK")
        raise


def purge_entity_sync(conn: sqlite3.Connection, entity_meta_id: int) -> None:
    conn.execute("BEGIN")
    try:
        for table in ("transitions", "daily_summary", "summary", "events_fired", "flap_baseline"):
            conn.execute(f"DELETE FROM {table} WHERE entity_meta_id = ?", (entity_meta_id,))
        conn.execute("DELETE FROM entity_meta WHERE id = ?", (entity_meta_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def purge_all_sync(conn: sqlite3.Connection) -> int:
    """Hard-delete every tracked entity and its history. Keeps audit_log."""
    conn.execute("BEGIN")
    try:
        count = conn.execute("SELECT COUNT(*) FROM entity_meta").fetchone()[0]
        for table in ("transitions", "daily_summary", "summary", "events_fired",
                      "flap_baseline", "entity_meta"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("COMMIT")
        return count
    except Exception:
        conn.execute("ROLLBACK")
        raise


def export_rows_sync(
    conn: sqlite3.Connection, entity_meta_id: int, start_ts: int, end_ts: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT ts, from_state, to_state, raw_from, raw_to, delta_s
        FROM transitions WHERE entity_meta_id = ? AND ts >= ? AND ts <= ? ORDER BY ts
        """,
        (entity_meta_id, start_ts, end_ts),
    ).fetchall()
