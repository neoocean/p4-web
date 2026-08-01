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

SCHEMA_VERSION = 2

INDEX_DIR = sessions.DATA_DIR / "index"

# One batch = one `changes -l` + one `describe -s` call.
BATCH = 500

# How deep the path rollup goes: '//depot/a/b/c/d/e' and no further.
# Deeper than this the drill-down stops being useful and the table just
# grows; anything below is reachable through the Changes path filter.
MAX_DIR_DEPTH = 6

# Changes per pass of the rollup backfill worker (see build_dirs).
DIRS_BATCH = 1000

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

-- Every ancestor directory a change touched, deduplicated, one row per
-- (change, directory). The Stats page groups by this; grouping over
-- change_files instead means folding 9.6M rows on every query, which
-- measured 28s against a real index -- unusable for a screen. Expanded
-- it is ~7% the size of change_files (13 directories per change) and
-- the same grouping lands in single-digit milliseconds.
CREATE TABLE IF NOT EXISTS change_dirs (
    change INTEGER,
    dir    TEXT,     -- '//depot/a/b', never a trailing slash
    depth  INTEGER,  -- '//depot' = 1, '//depot/a' = 2, ...
    PRIMARY KEY (change, dir)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS cd_dir ON change_dirs(dir);
CREATE INDEX IF NOT EXISTS cd_depth ON change_dirs(depth, dir);
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


def _dirs_for_path(path, max_depth=MAX_DIR_DEPTH):
    """Ancestor directories of a depot file, shallowest first.

    '//depot/a/b/f.txt' -> [('//depot', 1), ('//depot/a', 2), ('//depot/a/b', 3)]
    The file's own name is never a directory, and anything below
    max_depth is dropped rather than exploding the rollup.
    """
    if not path.startswith("//"):
        return []
    segments = path[2:].split("/")[:-1]  # drop the filename
    out = []
    for depth in range(1, min(len(segments), max_depth) + 1):
        out.append(("//" + "/".join(segments[:depth]), depth))
    return out


def _dir_rows(change_number, paths):
    """(change, dir, depth) rows for one change, deduplicated.

    A change that touches ten files in one directory yields that
    directory once -- the Stats page counts changes, not files.
    """
    seen = {}
    for path in paths:
        for directory, depth in _dirs_for_path(path):
            seen[directory] = depth
    return [(change_number, d, depth) for d, depth in seen.items()]


def _write_dirs(conn, rows):
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO change_dirs(change, dir, depth) VALUES(?, ?, ?)",
            rows,
        )


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
    dir_rows = []
    for rec in describe_recs:
        try:
            cn = int(rec["change"])
        except (KeyError, TypeError, ValueError):
            continue
        paths = []
        for path, action in _flatten_files(rec):
            leaf = path.rsplit("/", 1)[-1]
            file_rows.append((cn, path, path.lower(), leaf.lower(), action))
            paths.append(path)
        # Roll the directories up here, in the same transaction as the
        # files: a change is never half-indexed, so the Stats page never
        # sees a change whose paths it can't attribute.
        dir_rows.extend(_dir_rows(cn, paths))
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
        _write_dirs(conn, dir_rows)
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
    # Only `changes` is counted. Counting change_files means scanning
    # millions of rows — seconds on a cold cache — and the UI never
    # shows the number, so it was pure latency on every status call.
    total = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    return {
        "changes": int(total),
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


# ---------- directory rollup: catching up an index built before it ----------
#
# Changes indexed from here on get their directories written inline by
# _upsert_batch. An index built by schema v1 has none, and expanding
# 9.6M file rows takes ~40s -- far too long to do inside a request. So a
# background worker walks the existing changes newest-first, a batch at
# a time, and `dirs_from` records how far down it has got: every change
# numbered at or above it has its directories. The Stats page stays
# usable while that runs (time and author charts are already exact --
# only the path chart is still filling in) and says so.

_dirs_locks: dict[str, threading.Lock] = {}
_dirs_threads: dict[str, threading.Thread] = {}
_dirs_error: dict[str, str] = {}
_dirs_complete: set[str] = set()
_dirs_guard = threading.Lock()


def _dirs_lock(user):
    """Separate from _user_lock on purpose: the rollup catch-up can run
    for minutes, and it must not hold up a refresh for new changes."""
    with _dirs_guard:
        if user not in _dirs_locks:
            _dirs_locks[user] = threading.Lock()
        return _dirs_locks[user]


def _dirs_from(conn):
    value = _get_meta(conn, "dirs_from")
    return int(value) if value else None


def _dirs_status(conn, user):
    total = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    frm = _dirs_from(conn)
    if frm is None:
        built = 0
    else:
        built = conn.execute(
            "SELECT COUNT(*) FROM changes WHERE change >= ?", (frm,)
        ).fetchone()[0]
    done = total == 0 or built >= total
    if done:
        _dirs_complete.add(user)
    thread = _dirs_threads.get(user)
    return {
        "built": int(built),
        "total": int(total),
        "done": bool(done),
        "running": bool(thread and thread.is_alive()),
        "error": _dirs_error.get(user),
    }


def dirs_status(user):
    """Progress of the directory rollup. Deliberately not part of
    status(): that one is on the Changes page's critical path and this
    costs an extra count."""
    conn = _conn(user)
    try:
        return _dirs_status(conn, user)
    finally:
        conn.close()


def _build_dirs_batch(conn, user):
    """Expand one batch of older changes. Returns False when done."""
    frm = _dirs_from(conn)
    # Skip changes that already have directories -- an index built by
    # this version gets them inline, so a fresh one finishes here on the
    # first pass instead of re-expanding its whole history.
    unbuilt = (
        "NOT EXISTS (SELECT 1 FROM change_dirs d WHERE d.change = c.change)"
    )
    if frm is None:
        rows = conn.execute(
            f"SELECT c.change FROM changes c WHERE {unbuilt} "
            "ORDER BY c.change DESC LIMIT ?",
            (DIRS_BATCH,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT c.change FROM changes c WHERE c.change < ? AND {unbuilt} "
            "ORDER BY c.change DESC LIMIT ?",
            (frm, DIRS_BATCH),
        ).fetchall()
    numbers = [int(r[0]) for r in rows]
    if not numbers:
        # Walked past the oldest indexed change: everything is expanded.
        with conn:
            _set_meta(conn, "dirs_from", 1)
        _dirs_complete.add(user)
        return False

    placeholders = ",".join("?" * len(numbers))
    paths: dict[int, list] = {}
    for change_number, path in conn.execute(
        f"SELECT change, path FROM change_files WHERE change IN ({placeholders})",
        numbers,
    ):
        paths.setdefault(int(change_number), []).append(path)

    dir_rows = []
    for change_number in numbers:
        dir_rows.extend(_dir_rows(change_number, paths.get(change_number, [])))
    with conn:
        _write_dirs(conn, dir_rows)
        # The watermark moves in the same transaction as the rows it
        # describes; a crash between the two would claim expanded
        # changes that aren't.
        _set_meta(conn, "dirs_from", min(numbers))
    return True


def _build_dirs_loop(user):
    lock = _dirs_lock(user)
    if not lock.acquire(blocking=False):
        return
    try:
        conn = _conn(user)
        try:
            while _build_dirs_batch(conn, user):
                pass
            _dirs_error.pop(user, None)
        finally:
            conn.close()
    except Exception as exc:  # keep the thread's failure visible in status
        _dirs_error[user] = f"{type(exc).__name__}: {exc}"
    finally:
        lock.release()


def ensure_dirs(user):
    """Start the rollup catch-up unless it is finished or already running.

    Called from the Stats endpoints rather than at startup: an instance
    where nobody opens Stats should not spend 40s of disk on it.
    """
    if user in _dirs_complete:
        return
    with _dirs_guard:
        thread = _dirs_threads.get(user)
        if thread and thread.is_alive():
            return
        thread = threading.Thread(
            target=_build_dirs_loop, args=(user,), name=f"p4web-dirs-{user}", daemon=True
        )
        _dirs_threads[user] = thread
        thread.start()


# ---------- query ----------


def search(user, *, q=None, file=None, cl_user=None, date_from=None, date_to=None,
           before=None, max_results=200):
    """Fast changelist search over the index. Any combination of a
    description substring (`q`), a touched-filename substring (`file`),
    an author substring (`cl_user`), and an epoch date range. Newest
    first.

    `before` pages older results by limiting to changes numbered below
    it; unlike the live engine it composes with every other filter."""
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
        if before:
            where.append("c.change < ?")
            args.append(int(before))
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
