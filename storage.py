"""SQLite persistence for action items.

One table, ``items``. Embeddings and ticket references are stored as JSON
text -- SQLite has no array or dict type, and keeping them as JSON means the
DB stays inspectable with plain ``sqlite3`` during a demo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import config


class Status(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"

    @classmethod
    def coerce(cls, value) -> "Status":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.PENDING


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_text           TEXT    NOT NULL,
    owner               TEXT,
    source_meeting      TEXT,
    date_created        TEXT    NOT NULL,
    due_date            TEXT,
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','in_progress','blocked','done')),
    blocker_note        TEXT,
    embedding           TEXT,
    recurrence_group_id TEXT,
    recurrence_count    INTEGER NOT NULL DEFAULT 1,
    ticket_refs         TEXT    NOT NULL DEFAULT '{}',
    -- operational columns -------------------------------------------------
    -- embedding_model/_dim let check_recurrence refuse to compare vectors
    -- from different models, which is the one way an EMBEDDING_MODEL swap
    -- can silently corrupt results.
    embedding_model     TEXT,
    embedding_dim       INTEGER,
    -- without these the scheduler re-DMs every open item on every run
    last_nudged_at      TEXT,
    last_escalated_at   TEXT,
    source_context      TEXT,
    -- the team's own words justifying a recurrence match, quoted by the LLM
    -- adjudicator ("third time we've had an action item about this pipeline").
    -- This is what makes an escalation quotable instead of just a number.
    recurrence_hint     TEXT,
    -- verbatim dialogue captured at EXTRACTION time showing the team already
    -- knew this was a repeat ("this is the third time..."). Persisted because
    -- it is the evidence a later transcript's adjudication depends on.
    recurrence_signal   TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_group  ON items(recurrence_group_id);
CREATE INDEX IF NOT EXISTS idx_items_due    ON items(due_date);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add them to a database that already exists, so they are applied explicitly.
_MIGRATIONS = (("recurrence_hint", "TEXT"), ("recurrence_signal", "TEXT"))


def init_db(db_path: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
        for column, decl in _MIGRATIONS:
            if column not in existing:
                conn.execute(f"ALTER TABLE items ADD COLUMN {column} {decl}")


def reset_db(db_path: str | None = None) -> None:
    """Drop and recreate -- used by ``demo.py --reset-db``."""
    with connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS items")
        conn.executescript(SCHEMA)


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["embedding"] = json.loads(item["embedding"]) if item.get("embedding") else None
    try:
        item["ticket_refs"] = json.loads(item.get("ticket_refs") or "{}")
    except json.JSONDecodeError:
        item["ticket_refs"] = {}
    return item


def _rows(rows) -> list[dict]:
    return [r for r in (_row(r) for r in rows) if r is not None]


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


def insert_item(
    *,
    item_text: str,
    owner: str | None = None,
    source_meeting: str | None = None,
    due_date: str | None = None,
    status: Status | str = Status.PENDING,
    embedding: list[float] | None = None,
    embedding_model: str | None = None,
    recurrence_group_id: str | None = None,
    recurrence_count: int = 1,
    ticket_refs: dict | None = None,
    source_context: str | None = None,
    recurrence_hint: str | None = None,
    recurrence_signal: str | None = None,
    db_path: str | None = None,
) -> int:
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO items (
                item_text, owner, source_meeting, date_created, due_date, status,
                embedding, embedding_model, embedding_dim,
                recurrence_group_id, recurrence_count, ticket_refs, source_context,
                recurrence_hint, recurrence_signal
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item_text,
                owner,
                source_meeting,
                _now(),
                due_date,
                Status.coerce(status).value,
                json.dumps(embedding) if embedding is not None else None,
                embedding_model,
                len(embedding) if embedding is not None else None,
                recurrence_group_id,
                recurrence_count,
                json.dumps(ticket_refs or {}),
                source_context,
                recurrence_hint,
                recurrence_signal,
            ),
        )
        return int(cursor.lastrowid)


def update_status(
    item_id: int, status: Status | str, note: str | None = None, db_path: str | None = None
) -> dict | None:
    with connect(db_path) as conn:
        if note is None:
            conn.execute(
                "UPDATE items SET status = ? WHERE id = ?",
                (Status.coerce(status).value, item_id),
            )
        else:
            conn.execute(
                "UPDATE items SET status = ?, blocker_note = ? WHERE id = ?",
                (Status.coerce(status).value, note, item_id),
            )
    return get_item(item_id, db_path=db_path)


def set_ticket_refs(item_id: int, refs: dict, db_path: str | None = None) -> None:
    """Merge new platform refs into whatever is already stored."""
    with connect(db_path) as conn:
        row = conn.execute("SELECT ticket_refs FROM items WHERE id = ?", (item_id,)).fetchone()
        current = {}
        if row and row["ticket_refs"]:
            try:
                current = json.loads(row["ticket_refs"])
            except json.JSONDecodeError:
                current = {}
        current.update(refs or {})
        conn.execute("UPDATE items SET ticket_refs = ? WHERE id = ?", (json.dumps(current), item_id))


def mark_nudged(item_id: int, db_path: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE items SET last_nudged_at = ? WHERE id = ?", (_now(), item_id))


def mark_escalated(item_id: int, db_path: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE items SET last_escalated_at = ? WHERE id = ?", (_now(), item_id))


def set_recurrence_hint(item_id: int, hint: str | None, db_path: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE items SET recurrence_hint = ? WHERE id = ?", (hint, item_id))


def assign_group(item_ids: list[int], group_id: str, db_path: str | None = None) -> None:
    """Backfill a group id onto earlier items.

    The first occurrence of an issue is recorded before any repeat exists, so
    it has no group. When a repeat shows up, the group is created then and
    stamped backwards onto the originals.
    """
    if not item_ids:
        return
    placeholders = ",".join("?" * len(item_ids))
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE items SET recurrence_group_id = ? WHERE id IN ({placeholders})",
            (group_id, *item_ids),
        )


def bump_group_count(group_id: str, count: int, db_path: str | None = None) -> None:
    """Set the occurrence count on every member of a recurrence group."""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE items SET recurrence_count = ? WHERE recurrence_group_id = ?",
            (count, group_id),
        )


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


def get_item(item_id: int, db_path: str | None = None) -> dict | None:
    with connect(db_path) as conn:
        return _row(conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())


def all_items(db_path: str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        return _rows(conn.execute("SELECT * FROM items ORDER BY id").fetchall())


def all_embeddings(db_path: str | None = None) -> list[dict]:
    """Every stored vector, in the shape check_recurrence expects."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, item_text, embedding, embedding_model, embedding_dim,
                   recurrence_group_id, recurrence_count, source_meeting, ticket_refs,
                   source_context, recurrence_hint, recurrence_signal
            FROM items
            WHERE embedding IS NOT NULL
            ORDER BY id
            """
        ).fetchall()
    return _rows(rows)


def open_items(db_path: str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        return _rows(
            conn.execute("SELECT * FROM items WHERE status != 'done' ORDER BY due_date").fetchall()
        )


def items_due_within(days: int, db_path: str | None = None) -> list[dict]:
    """Open items due within ``days`` -- including anything already past due."""
    cutoff = (date.today() + timedelta(days=days)).isoformat()
    with connect(db_path) as conn:
        return _rows(
            conn.execute(
                """
                SELECT * FROM items
                WHERE status != 'done' AND due_date IS NOT NULL AND due_date <= ?
                ORDER BY due_date
                """,
                (cutoff,),
            ).fetchall()
        )


def items_overdue_by(days: int, db_path: str | None = None) -> list[dict]:
    """Open items whose due date passed more than ``days`` ago."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with connect(db_path) as conn:
        return _rows(
            conn.execute(
                """
                SELECT * FROM items
                WHERE status != 'done' AND due_date IS NOT NULL AND due_date < ?
                ORDER BY due_date
                """,
                (cutoff,),
            ).fetchall()
        )


def group_members(group_id: str, db_path: str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        return _rows(
            conn.execute(
                "SELECT * FROM items WHERE recurrence_group_id = ? ORDER BY id", (group_id,)
            ).fetchall()
        )
