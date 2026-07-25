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
    platform: str | None
    friendly_name: str | None
    manufacturer: str | None
    model: str | None
    rated_hours: float | None
    rated_cycles: int | None
    tracking_since: int
    disabled: bool
    disabled_reason: str | None
    debounce_s: float


_COLUMNS = (
    "id, unique_id, entity_id, domain, platform, friendly_name, manufacturer, "
    "model, rated_hours, rated_cycles, tracking_since, disabled, disabled_reason, "
    "debounce_s"
)


def _row_to_entity(row: sqlite3.Row) -> TrackedEntity:
    return TrackedEntity(
        id=row["id"],
        unique_id=row["unique_id"],
        entity_id=row["entity_id"],
        domain=row["domain"],
        platform=row["platform"],
        friendly_name=row["friendly_name"],
        manufacturer=row["manufacturer"],
        model=row["model"],
        rated_hours=row["rated_hours"],
        rated_cycles=row["rated_cycles"],
        tracking_since=row["tracking_since"],
        disabled=bool(row["disabled"]),
        disabled_reason=row["disabled_reason"],
        debounce_s=row["debounce_s"],
    )


def wear_sensor_root(entity: TrackedEntity) -> str:
    """Stable, collision-free root for this entity's sensor unique_ids.

    HA unique_ids are only unique per (platform, domain); two tracked entities
    that share a unique_id string across platforms would otherwise mint the same
    sensor unique_ids and reject the second. Qualify with platform+domain (both
    stable across renames and re-pairs) — but ONLY once platform is known. A row
    with a unique_id but platform still NULL (pre-v5, before the first backfill)
    returns the bare unique_id: that is exactly the old pre-composite root, so such
    rows need no migration and mint no duplicates. When platform is later
    backfilled the root changes to the qualified form and the setup migration
    remaps the existing sensors in place. Legacy entities without a unique_id keep
    the entity_id root, which is already unique per install."""
    if entity.unique_id:
        if entity.platform:
            return f"{entity.platform}_{entity.domain}_{entity.unique_id}"
        return entity.unique_id
    return entity.entity_id


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


def load_by_identity(
    conn: sqlite3.Connection,
    platform: str | None,
    domain: str,
    unique_id: str,
) -> TrackedEntity | None:
    """Match on the composite HA guarantees unique: (platform, domain, unique_id).
    `platform IS ?` binds NULL correctly for legacy rows without a platform."""
    cur = conn.execute(
        f"SELECT {_COLUMNS} FROM entity_meta "
        "WHERE unique_id = ? AND domain = ? AND platform IS ?",
        (unique_id, domain, platform),
    )
    row = cur.fetchone()
    return _row_to_entity(row) if row else None


def _resume_if_removed(
    conn: sqlite3.Connection, row: TrackedEntity
) -> TrackedEntity:
    """Re-registering a soft-deleted row resumes tracking (DESIGN §4, Zigbee
    re-pair). Only a removal-disable is cleared; a deliberate user disable stays."""
    if row.disabled and row.disabled_reason == "removed":
        conn.execute(
            "UPDATE entity_meta SET disabled = 0, disabled_reason = NULL WHERE id = ?",
            (row.id,),
        )
        return load_by_entity_id(conn, row.entity_id) or row
    return row


def _backfill(
    conn: sqlite3.Connection,
    row: TrackedEntity,
    *,
    unique_id: str | None = None,
    platform: str | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
    rated_hours: float | None = None,
    rated_cycles: int | None = None,
) -> TrackedEntity:
    """Fill in columns that are currently NULL (never overwrite a set value)."""
    candidates = {
        "unique_id": (unique_id, row.unique_id),
        "platform": (platform, row.platform),
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
    platform: str | None = None,
    friendly_name: str | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
    rated_hours: float | None = None,
    rated_cycles: int | None = None,
    debounce_s: float = 2.0,
    known_entity_ids: set[str] | None = None,
) -> TrackedEntity:
    """Insert if absent; return the persisted row.

    Identity is the composite (platform, domain, unique_id) — the tuple HA keeps
    unique — not the unique_id string alone, so two entities that share a
    unique_id across platforms/domains stay distinct. If a row already matches
    (e.g. the entity was renamed while we weren't listening, or a Zigbee device
    was re-paired), follow that row and update its mutable `entity_id` label.
    Otherwise match on `entity_id` and backfill identity if it was unknown.
    Re-registering a removal-disabled row resumes its tracking, but only on a
    confirmed identity match (DESIGN §4).

    `known_entity_ids`, when supplied by the caller (the full current tracked
    set), guards the legacy NULL-platform fallback against stealing a row that
    still belongs to a different live entity.
    """
    if unique_id is not None:
        by_id = load_by_identity(conn, platform, domain, unique_id)
        if by_id is None and platform is not None:
            # Pre-v5 rows have platform NULL until first backfill; without this a
            # rename during downtime would orphan the row and fork its history.
            # Guard: never seize a legacy row whose entity_id still belongs to a
            # different live entity — otherwise, when two entities share a
            # unique_id string across platforms, whichever upserts first would
            # steal the other's history (DESIGN §4).
            candidate = load_by_identity(conn, None, domain, unique_id)
            if candidate is not None and not (
                candidate.entity_id != entity_id
                and known_entity_ids is not None
                and candidate.entity_id in known_entity_ids
            ):
                by_id = candidate
        if by_id is not None:
            if by_id.entity_id != entity_id:
                try:
                    conn.execute(
                        "UPDATE entity_meta SET entity_id = ? WHERE id = ?",
                        (entity_id, by_id.id),
                    )
                except sqlite3.IntegrityError:
                    # entity_id already held by another row (rare rename swap).
                    return _resume_if_removed(conn, by_id)
                by_id = load_by_entity_id(conn, entity_id) or by_id
            by_id = _backfill(
                conn, by_id, platform=platform, manufacturer=manufacturer,
                model=model, rated_hours=rated_hours, rated_cycles=rated_cycles,
            )
            return _resume_if_removed(conn, by_id)

    # Bare entity_id match — identity is unconfirmed (no unique_id, or it matched
    # no row). A removal-disabled row must NOT resume here: a plain reload upserts
    # the stale entity_id with unique_id=None, and if that entity_id is later
    # claimed by a different device its activity would accrue onto the old
    # device's counters. Resume happens only on the identity path above (§4).
    existing = load_by_entity_id(conn, entity_id)
    if existing is not None:
        if (
            existing.disabled
            and existing.disabled_reason == "removed"
            and unique_id is not None
        ):
            # A different identity is claiming the freed entity_id: the identity
            # paths above already missed, so this unique_id is not the removed
            # row's. Backfilling would forge the claimant's identity onto the old
            # row (letting the next reload falsely resume it), and the entity_id
            # UNIQUE constraint makes the INSERT below unreachable while the old row
            # holds it. Retire the old row to a tombstone label (history FKs to its
            # id, so it stays intact) and fall through to INSERT a fresh row.
            conn.execute(
                "UPDATE entity_meta SET entity_id = ? WHERE id = ?",
                (f"{_RETIRED_PREFIX}{existing.id}__{entity_id}", existing.id),
            )
        else:
            return _backfill(
                conn, existing, unique_id=unique_id, platform=platform,
                manufacturer=manufacturer, model=model, rated_hours=rated_hours,
                rated_cycles=rated_cycles,
            )
    conn.execute(
        """
        INSERT INTO entity_meta (
            unique_id, entity_id, domain, platform, friendly_name,
            manufacturer, model, rated_hours, rated_cycles,
            tracking_since, debounce_s
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            unique_id, entity_id, domain, platform, friendly_name,
            manufacturer, model, rated_hours, rated_cycles,
            tracking_since, debounce_s,
        ),
    )
    loaded = load_by_entity_id(conn, entity_id)
    if loaded is None:
        raise RuntimeError(f"insert into entity_meta failed for {entity_id!r}")
    return loaded


def set_disabled(
    conn: sqlite3.Connection,
    entity_id: str,
    disabled: bool,
    reason: str | None = None,
) -> None:
    """Set the disabled flag and (when disabling) record why: 'removed' for a
    registry soft-delete that later resumes, 'user' for a deliberate disable that
    stays sticky across restarts. Re-enabling clears the reason."""
    conn.execute(
        "UPDATE entity_meta SET disabled = ?, disabled_reason = ? WHERE entity_id = ?",
        (1 if disabled else 0, reason if disabled else None, entity_id),
    )


_SENTINEL_PREFIX = "__wt_pending_"
# Tombstone label for a removal-disabled row whose entity_id a different identity
# reclaims; kept distinct from the swap sentinel so reconcile never treats it as one.
_RETIRED_PREFIX = "__wt_retired_"


def reconcile_rename(
    conn: sqlite3.Connection,
    old_entity_id: str,
    new_entity_id: str,
    target_unique_id: str | None,
    target_platform: str | None = None,
    target_domain: str | None = None,
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
        # Prefer the composite identity so a unique_id shared across platforms
        # doesn't move the wrong row; fall back to unique_id-only when the caller
        # can't supply the domain (older callers / tests).
        if target_domain is not None:
            row = load_by_identity(conn, target_platform, target_domain, target_unique_id)
            if row is None and target_platform is not None:
                # Pre-v5 rows have platform NULL, so the exact-platform lookup
                # misses them and an A<->B swap would strand one row on a sentinel
                # forever. Find them by the relaxed identity too, but only accept
                # the row we're actually renaming (currently at old_entity_id) or
                # one parked on a swap sentinel — never a different live entity
                # that merely shares this unique_id (DESIGN §4).
                candidate = load_by_identity(conn, None, target_domain, target_unique_id)
                if candidate is not None and (
                    candidate.entity_id == old_entity_id
                    or candidate.entity_id.startswith(_SENTINEL_PREFIX)
                ):
                    row = candidate
        else:
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
