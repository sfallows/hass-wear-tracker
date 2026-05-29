"""Tracked-entity registry: `TrackedEntity` dataclass + `entity_meta` CRUD."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(slots=True)
class TrackedEntity:
    id: int
    unique_id: str | None
    entity_id: str
    domain: str
    friendly_name: str | None
    manufacturer: str | None
    model: str | None
    rated_hours: float | None
    rated_cycles: int | None
    tracking_since: int
    disabled: bool
    debounce_s: float


_COLUMNS = (
    "id, unique_id, entity_id, domain, friendly_name, manufacturer, model, "
    "rated_hours, rated_cycles, tracking_since, disabled, debounce_s"
)


def _row_to_entity(row: sqlite3.Row) -> TrackedEntity:
    return TrackedEntity(
        id=row["id"],
        unique_id=row["unique_id"],
        entity_id=row["entity_id"],
        domain=row["domain"],
        friendly_name=row["friendly_name"],
        manufacturer=row["manufacturer"],
        model=row["model"],
        rated_hours=row["rated_hours"],
        rated_cycles=row["rated_cycles"],
        tracking_since=row["tracking_since"],
        disabled=bool(row["disabled"]),
        debounce_s=row["debounce_s"],
    )


def load_all(conn: sqlite3.Connection) -> list[TrackedEntity]:
    cur = conn.execute(f"SELECT {_COLUMNS} FROM entity_meta")
    return [_row_to_entity(r) for r in cur.fetchall()]


def load_active(conn: sqlite3.Connection) -> list[TrackedEntity]:
    cur = conn.execute(f"SELECT {_COLUMNS} FROM entity_meta WHERE disabled = 0")
    return [_row_to_entity(r) for r in cur.fetchall()]


def load_by_entity_id(
    conn: sqlite3.Connection, entity_id: str
) -> TrackedEntity | None:
    cur = conn.execute(
        f"SELECT {_COLUMNS} FROM entity_meta WHERE entity_id = ?",
        (entity_id,),
    )
    row = cur.fetchone()
    return _row_to_entity(row) if row else None


def load_by_unique_id(
    conn: sqlite3.Connection, unique_id: str
) -> TrackedEntity | None:
    cur = conn.execute(
        f"SELECT {_COLUMNS} FROM entity_meta WHERE unique_id = ?",
        (unique_id,),
    )
    row = cur.fetchone()
    return _row_to_entity(row) if row else None


def _backfill(
    conn: sqlite3.Connection,
    row: TrackedEntity,
    *,
    unique_id: str | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
    rated_hours: float | None = None,
    rated_cycles: int | None = None,
) -> TrackedEntity:
    """Fill in columns that are currently NULL (never overwrite a set value)."""
    candidates = {
        "unique_id": (unique_id, row.unique_id),
        "manufacturer": (manufacturer, row.manufacturer),
        "model": (model, row.model),
        "rated_hours": (rated_hours, row.rated_hours),
        "rated_cycles": (rated_cycles, row.rated_cycles),
    }
    updates = {col: new for col, (new, cur) in candidates.items() if new is not None and cur is None}
    if not updates:
        return row
    set_clause = ", ".join(f"{col} = ?" for col in updates)
    conn.execute(
        f"UPDATE entity_meta SET {set_clause} WHERE id = ?",
        (*updates.values(), row.id),
    )
    return load_by_entity_id(conn, row.entity_id) or row


def upsert(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    domain: str,
    tracking_since: int,
    unique_id: str | None = None,
    friendly_name: str | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
    rated_hours: float | None = None,
    rated_cycles: int | None = None,
    debounce_s: float = 2.0,
) -> TrackedEntity:
    """Insert if absent; return the persisted row.

    `unique_id` is the durable key: if a row already has it (e.g. the entity was
    renamed while we weren't listening, or a Zigbee device was re-paired), follow
    that row and update its mutable `entity_id` label. Otherwise match on
    `entity_id` and backfill `unique_id` if it was previously unknown.
    """
    if unique_id is not None:
        by_uid = load_by_unique_id(conn, unique_id)
        if by_uid is not None:
            if by_uid.entity_id != entity_id:
                try:
                    conn.execute(
                        "UPDATE entity_meta SET entity_id = ? WHERE id = ?",
                        (entity_id, by_uid.id),
                    )
                except sqlite3.IntegrityError:
                    # entity_id already held by another row (rare rename swap).
                    return by_uid
                by_uid = load_by_entity_id(conn, entity_id) or by_uid
            return _backfill(
                conn, by_uid, manufacturer=manufacturer, model=model,
                rated_hours=rated_hours, rated_cycles=rated_cycles,
            )

    existing = load_by_entity_id(conn, entity_id)
    if existing is not None:
        return _backfill(
            conn, existing, unique_id=unique_id, manufacturer=manufacturer,
            model=model, rated_hours=rated_hours, rated_cycles=rated_cycles,
        )
    conn.execute(
        """
        INSERT INTO entity_meta (
            unique_id, entity_id, domain, friendly_name,
            manufacturer, model, rated_hours, rated_cycles,
            tracking_since, debounce_s
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            unique_id, entity_id, domain, friendly_name,
            manufacturer, model, rated_hours, rated_cycles,
            tracking_since, debounce_s,
        ),
    )
    loaded = load_by_entity_id(conn, entity_id)
    if loaded is None:
        raise RuntimeError(f"insert into entity_meta failed for {entity_id!r}")
    return loaded


def set_disabled(
    conn: sqlite3.Connection, entity_id: str, disabled: bool
) -> None:
    conn.execute(
        "UPDATE entity_meta SET disabled = ? WHERE entity_id = ?",
        (1 if disabled else 0, entity_id),
    )


_SENTINEL_PREFIX = "__wt_pending_"


def reconcile_rename(
    conn: sqlite3.Connection,
    old_entity_id: str,
    new_entity_id: str,
    target_unique_id: str | None,
) -> list[tuple[str, str]]:
    """Make `new_entity_id` belong to the row for `target_unique_id` (falling back
    to `old_entity_id` for rows that predate unique_id tracking).

    History rows FK to the surrogate `id`, so a rename only moves the mutable
    `entity_id` label. If another tracked row currently holds `new_entity_id`, it is
    parked under a sentinel id first — that is what lets a simultaneous A<->B swap
    (delivered as two independent registry events) resolve correctly: the second
    event finds its target free and the parked row settles onto it.

    Returns the `(from_entity_id, to_entity_id)` moves applied, in order, so the
    caller can re-key its in-memory state to match. Runs in one transaction.
    """
    row = None
    if target_unique_id is not None:
        row = load_by_unique_id(conn, target_unique_id)
    if row is None:
        row = load_by_entity_id(conn, old_entity_id)
    if row is None or row.entity_id == new_entity_id:
        return []

    moves: list[tuple[str, str]] = []
    conn.execute("BEGIN")
    try:
        occupant = load_by_entity_id(conn, new_entity_id)
        if occupant is not None and occupant.id != row.id:
            sentinel = f"{_SENTINEL_PREFIX}{occupant.id}__"
            conn.execute(
                "UPDATE entity_meta SET entity_id = ? WHERE id = ?",
                (sentinel, occupant.id),
            )
            moves.append((new_entity_id, sentinel))
        src = row.entity_id
        conn.execute(
            "UPDATE entity_meta SET entity_id = ? WHERE id = ?",
            (new_entity_id, row.id),
        )
        moves.append((src, new_entity_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return moves
