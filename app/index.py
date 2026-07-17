"""SQLite-backed changelist search index.

Makes the Changes view's three search axes — description text, date
range, and touched-filename — fast across the whole submitted history
instead of walking p4 live on every query.

Per-user isolation (mirrors p4v-tui's per-identity index): each user
gets their own DB under data/index/<user>.db, built with that user's
ticket, so Perforce protections are respected automatically — a user
only ever indexes the changes and files that `p4 changes`/`p4 describe`
would show them.

The index is populated newest-first with two watermarks in `meta`:
`newest_indexed` (advanced by forward catch-up of freshly submitted
changes) and `oldest_indexed` (walked downward by backfill batches).
Each batch costs exactly two p4 calls — one `changes -l` and one
`describe -s <many changes>` — so a batch of 500 changes indexes with
two subprocesses, not 500.
"""

import re
import sqlite3
import threading
import time
from pathlib import Path

from . import p4, sessions

SCHEMA_VERSION = 1

INDEX_DIR = sessions.DATA_DIR / "index"

# One batch = one `changes -l` + one `describe -s` call.
BATCH = 500

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS changes (
    change     INTEGER PRIMARY KEY,
    user       TEXT,
    client     TEXT,
    time       INTEGER,
    desc       TEXT,
    desc_lower TEXT
);
CREATE INDEX IF NOT EXISTS changes_time ON changes(time DESC);
CREATE INDEX IF NOT EXISTS changes_user ON changes(user);

CREATE TABLE IF NOT EXISTS change_files (
    change     INTEGER,
    path       TEXT,
    path_lower TEXT,
    leaf_lower TEXT,
    action     TEXT,
    PRIMARY KEY (change, path)
);
CREATE INDEX IF NOT EXISTS cf_path ON change_files(path_lower);
CREATE INDEX IF NOT EXISTS cf_leaf ON change_files(leaf_lower);
CREATE INDEX IF NOT EXISTS cf_change ON change_files(change);
"""

# One build lock per user so two refreshes don't fight over the same DB.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _user_lock(user):
    with _locks_guard:
        if user not in _locks:
            _locks[user] = threading.Lock()
        return _locks[user]


def _safe_user(user):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", user) or "user"


def db_path(user):
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    return INDEX_DIR / f"{_safe_user(user)}.db"


def _conn(user):
    path = db_path(user)
    fresh = not path.exists()
    conn = sqlite3.connect(str(path), timeout=30)
    conn.executescript(_SCHEMA_SQL)
    if fresh:
        import os
        os.chmod(path, 0o600)  # descriptions of protected changes may live here
    return conn


def _get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# ---------- ingest ----------


def _flatten_files(rec):
    out = []
    i = 0
    while f"depotFile{i}" in rec:
        path = rec[f"depotFile{i}"]
        out.append((path, rec.get(f"action{i}", "")))
        i += 1
    return out


def _upsert_batch(conn, change_recs, describe_recs):
    """Write one batch: change metadata + the files each change touched."""
    changes_rows = []
    for r in change_recs:
        try:
            cn = int(r["change"])
        except (KeyError, TypeError, ValueError):
            continue
        desc = (r.get("desc") or "").rstrip()
        changes_rows.append((
            cn, r.get("user", ""), r.get("client", ""),
            int(r.get("time", 0) or 0), desc, desc.lower(),
        ))
    file_rows = []
    for rec in describe_recs:
        try:
            cn = int(rec["change"])
        except (KeyError, TypeError, ValueError):
            continue
        for path, action in _flatten_files(rec):
            leaf = path.rsplit("/", 1)[-1]
            file_rows.append((cn, path, path.lower(), leaf.lower(), action))
    with conn:
        conn.executemany(
            "INSERT INTO changes(change, user, client, time, desc, desc_lower) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(change) DO UPDATE SET user=excluded.user, "
            "client=excluded.client, time=excluded.time, desc=excluded.desc, "
            "desc_lower=excluded.desc_lower",
            changes_rows,
        )
        if file_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO change_files"
                "(change, path, path_lower, leaf_lower, action) VALUES(?, ?, ?, ?, ?)",
                file_rows,
            )
    return changes_rows


def _fetch_changes(user, ticket, spec):
    args = ["changes", "-l", "-t", "-s", "submitted", "-m", str(BATCH)]
    if spec:
        args.append(spec)
    return p4.run(args, user, ticket)


def _describe(user, ticket, change_numbers):
    if not change_numbers:
        return []
    args = ["describe", "-s"] + [str(c) for c in change_numbers]
    return p4.run(args, user, ticket)


def refresh(user, ticket, backfill=True):
    """Bring the user's index up to date.

    Always catches up on newly-submitted changes (forward). When
    `backfill` is set and older history remains unindexed, also pulls
    one batch of older changes so repeated calls walk backward. Returns
    a status dict.
    """
    lock = _user_lock(user)
    if not lock.acquire(blocking=False):
        # A refresh is already running for this user; report current state.
        return status(user)
    try:
        conn = _conn(user)
        try:
            newest = int(_get_meta(conn, "newest_indexed", "0") or 0)
            oldest = int(_get_meta(conn, "oldest_indexed", "0") or 0)
            indexed = 0

            # Forward: keep pulling batches of changes above `newest`
            # until caught up. New submits are usually a handful.
            while True:
                spec = f"//...@>{newest}" if newest else None
                recs = _fetch_changes(user, ticket, spec)
                if not recs:
                    break
                nums = [int(r["change"]) for r in recs if r.get("change")]
                describe_recs = _describe(user, ticket, nums)
                rows = _upsert_batch(conn, recs, describe_recs)
                indexed += len(rows)
                batch_max = max(nums)
                batch_min = min(nums)
                newest = max(newest, batch_max)
                oldest = batch_min if not oldest else min(oldest, batch_min)
                _set_meta(conn, "newest_indexed", newest)
                _set_meta(conn, "oldest_indexed", oldest)
                if len(recs) < BATCH:
                    break

            # Backfill: one batch below `oldest`, so the UI can extend
            # coverage backward on demand without a giant first build.
            if backfill and oldest > 1:
                recs = _fetch_changes(user, ticket, f"//...@<{oldest}")
                if recs:
                    nums = [int(r["change"]) for r in recs if r.get("change")]
                    describe_recs = _describe(user, ticket, nums)
                    rows = _upsert_batch(conn, recs, describe_recs)
                    indexed += len(rows)
                    oldest = min(oldest, min(nums))
                    _set_meta(conn, "oldest_indexed", oldest)

            _set_meta(conn, "updated", int(time.time()))
            _set_meta(conn, "schema_version", SCHEMA_VERSION)
            # Commit the trailing meta writes; without this the last
            # watermark/updated values would roll back on close().
            conn.commit()
            st = _status(conn)
            st["indexed_now"] = indexed
            return st
        finally:
            conn.close()
    finally:
        lock.release()


# ---------- status ----------


def _status(conn):
    total = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    files = conn.execute("SELECT COUNT(*) FROM change_files").fetchone()[0]
    return {
        "changes": int(total),
        "files": int(files),
        "newest": int(_get_meta(conn, "newest_indexed", "0") or 0),
        "oldest": int(_get_meta(conn, "oldest_indexed", "0") or 0),
        "updated": int(_get_meta(conn, "updated", "0") or 0),
        "fullyBackfilled": int(_get_meta(conn, "oldest_indexed", "0") or 0) <= 1,
    }


def status(user):
    conn = _conn(user)
    try:
        return _status(conn)
    finally:
        conn.close()


# ---------- query ----------


def search(user, *, q=None, file=None, cl_user=None, date_from=None, date_to=None, max_results=200):
    """Fast changelist search over the index. Any combination of a
    description substring (`q`), a touched-filename substring (`file`),
    an author substring (`cl_user`), and an epoch date range. Newest
    first."""
    conn = _conn(user)
    try:
        where = []
        args = []
        join = ""
        if file:
            join = "JOIN change_files cf ON cf.change = c.change"
            where.append("cf.path_lower LIKE '%' || ? || '%'")
            args.append(file.lower())
        if q:
            where.append("c.desc_lower LIKE '%' || ? || '%'")
            args.append(q.lower())
        if cl_user:
            where.append("LOWER(c.user) LIKE '%' || ? || '%'")
            args.append(cl_user.lower())
        if date_from is not None:
            where.append("c.time >= ?")
            args.append(int(date_from))
        if date_to is not None:
            where.append("c.time <= ?")
            args.append(int(date_to))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            "SELECT DISTINCT c.change, c.user, c.client, c.time, c.desc "
            f"FROM changes c {join}{clause} "
            "ORDER BY c.change DESC LIMIT ?"
        )
        args.append(int(max_results))
        rows = conn.execute(sql, args).fetchall()
        return [
            {
                "change": int(r[0]),
                "user": r[1] or "",
                "client": r[2] or "",
                "time": int(r[3] or 0),
                "desc": r[4] or "",
            }
            for r in rows
        ]
    finally:
        conn.close()
