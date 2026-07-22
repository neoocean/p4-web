"""Session store mapping a browser cookie to a p4 ticket.

Backed by SQLite so sessions survive a server restart (the HANDOFF
"users get logged out on restart" item). Single-file DB in the data
directory; connections are opened per call, which is plenty for the
personal/small-team scale this app targets.
"""

import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

# A session lives exactly as long as the Perforce ticket behind it, so
# the UI never logs you out while P4V/the CLI would still let you work.
# This value is only the fallback for when the server won't say how
# long the ticket has left.
SESSION_TTL = 12 * 60 * 60

# Browsers clamp cookie lifetimes to 400 days, so asking for more just
# gets silently trimmed; ask for exactly the cap instead.
MAX_COOKIE_AGE = 400 * 24 * 60 * 60

DATA_DIR = Path(
    os.environ.get("P4WEB_DATA")
    or Path(__file__).resolve().parent.parent / "data"
)
DB_PATH = DATA_DIR / "sessions.db"


def _conn():
    created = not DB_PATH.exists()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            sid TEXT PRIMARY KEY,
            user TEXT NOT NULL,
            ticket TEXT NOT NULL,
            owned_ticket INTEGER NOT NULL DEFAULT 1,
            expires REAL NOT NULL
        )"""
    )
    # Added when sessions started tracking the ticket's own expiry:
    # when we last asked the server how long the ticket has left.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "checked" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN checked REAL NOT NULL DEFAULT 0")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS favorites (
            user TEXT NOT NULL,
            path TEXT NOT NULL,
            added REAL NOT NULL,
            PRIMARY KEY (user, path)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            created REAL NOT NULL,
            updated REAL,
            path TEXT,
            rev INTEGER,
            line INTEGER,
            change_num INTEGER,
            parent INTEGER,
            resolved INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            body TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_path ON comments(path)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_change ON comments(change_num)")
    # Light reviews: one row per changelist under review, plus the log of
    # every state change so a thread reads as a history rather than a
    # single mutable flag. Perforce knows nothing about these — a review
    # is an app-side opinion about a (usually shelved) changelist.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reviews (
            change_num INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            opened_by TEXT NOT NULL,
            created REAL NOT NULL,
            updated REAL NOT NULL,
            updated_by TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS review_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            change_num INTEGER NOT NULL,
            user TEXT NOT NULL,
            created REAL NOT NULL,
            state TEXT NOT NULL,
            note TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_events_change ON review_events(change_num)"
    )
    # @mentions: one row per (comment, mentioned user), so "what was I
    # named in, and have I seen it" is a query rather than a scan of
    # every comment body.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id INTEGER NOT NULL,
            user TEXT NOT NULL,
            created REAL NOT NULL,
            seen INTEGER NOT NULL DEFAULT 0,
            UNIQUE (comment_id, user)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mentions_user ON mentions(user, seen)")
    if created:
        # The DB holds live tickets — owner-only.
        os.chmod(DB_PATH, 0o600)
    else:
        # Defensive: keep the ticket store owner-only even if the file
        # predates this check or was restored/copied with looser modes.
        try:
            if DB_PATH.stat().st_mode & 0o077:
                os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
    return conn


def create(user, ticket, owned_ticket=True, ttl=None):
    """Start a session. `ttl` is the ticket's remaining lifetime in
    seconds; None falls back to SESSION_TTL."""
    sid = secrets.token_urlsafe(32)
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO sessions (sid, user, ticket, owned_ticket, expires, checked)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (sid, user, ticket, 1 if owned_ticket else 0,
             now + (SESSION_TTL if ttl is None else ttl), now),
        )
        # Opportunistic cleanup keeps the file from growing forever.
        conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
    return sid


def get(sid):
    if not sid:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT user, ticket, owned_ticket, expires, checked FROM sessions WHERE sid = ?",
            (sid,),
        ).fetchone()
        if row is None:
            return None
        user, ticket, owned, expires, checked = row
        if expires < time.time():
            conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
            return None
    return {
        "user": user, "ticket": ticket, "owned_ticket": bool(owned),
        "expires": expires, "checked": checked,
    }


def refresh(sid, ttl=None):
    """Record that the ticket was just re-checked, and (when `ttl` is
    known) re-anchor the session to the ticket's current expiry."""
    now = time.time()
    with _conn() as conn:
        if ttl is None:
            conn.execute("UPDATE sessions SET checked = ? WHERE sid = ?", (now, sid))
        else:
            conn.execute(
                "UPDATE sessions SET checked = ?, expires = ? WHERE sid = ?",
                (now, now + ttl, sid),
            )


def destroy(sid):
    if not sid:
        return None
    session = get(sid)
    with _conn() as conn:
        conn.execute("DELETE FROM sessions WHERE sid = ?", (sid,))
    return session


# ---------- per-user path favorites (shared across devices) ----------


def favorites(user):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT path, added FROM favorites WHERE user = ? ORDER BY added",
            (user,),
        ).fetchall()
    return [{"path": p, "added": a} for p, a in rows]


def add_favorite(user, path):
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO favorites (user, path, added) VALUES (?, ?, ?)",
            (user, path, time.time()),
        )


def remove_favorite(user, path):
    with _conn() as conn:
        conn.execute(
            "DELETE FROM favorites WHERE user = ? AND path = ?", (user, path)
        )


# ---------- inline comments (threads on file lines / changelists) ----------


_COMMENT_COLS = "id, user, created, updated, path, rev, line, change_num, parent, resolved, deleted, body"


def _comment_row(row):
    keys = ["id", "user", "created", "updated", "path", "rev", "line", "change", "parent", "resolved", "deleted", "body"]
    d = dict(zip(keys, row))
    d["resolved"] = bool(d["resolved"])
    d["deleted"] = bool(d["deleted"])
    if d["deleted"]:
        d["body"] = ""
    return d


def comments_for(path=None, change=None, files=False):
    """Comments on a file (every revision of it), or on a changelist.

    A changelist's own thread is the path-less one; `files=True` also
    returns the comments anchored to lines of files in that change, which
    is how a diff-anchored comment surfaces on the changelist page."""
    with _conn() as conn:
        if path:
            rows = conn.execute(
                f"SELECT {_COMMENT_COLS} FROM comments WHERE path = ? ORDER BY created",
                (path,),
            ).fetchall()
        elif files:
            rows = conn.execute(
                f"SELECT {_COMMENT_COLS} FROM comments WHERE change_num = ? ORDER BY created",
                (change,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_COMMENT_COLS} FROM comments WHERE change_num = ? AND path IS NULL ORDER BY created",
                (change,),
            ).fetchall()
    return [_comment_row(r) for r in rows]


def comment_get(cid):
    with _conn() as conn:
        row = conn.execute(
            f"SELECT {_COMMENT_COLS} FROM comments WHERE id = ?", (cid,)
        ).fetchone()
    return _comment_row(row) if row else None


def comment_add(user, body, path=None, rev=None, line=None, change=None, parent=None):
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO comments (user, created, path, rev, line, change_num, parent, body)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user, time.time(), path, rev, line, change, parent, body),
        )
        return cur.lastrowid


def comment_update(cid, body=None, resolved=None):
    with _conn() as conn:
        if body is not None:
            conn.execute(
                "UPDATE comments SET body = ?, updated = ? WHERE id = ?",
                (body, time.time(), cid),
            )
        if resolved is not None:
            conn.execute(
                "UPDATE comments SET resolved = ? WHERE id = ?",
                (1 if resolved else 0, cid),
            )


def comment_delete(cid):
    """Hard-delete leaf comments; tombstone roots that still have
    replies so the thread stays readable."""
    with _conn() as conn:
        has_replies = conn.execute(
            "SELECT 1 FROM comments WHERE parent = ? AND deleted = 0 LIMIT 1", (cid,)
        ).fetchone()
        if has_replies:
            conn.execute(
                "UPDATE comments SET deleted = 1, body = '' WHERE id = ?", (cid,)
            )
        else:
            conn.execute("DELETE FROM comments WHERE id = ?", (cid,))


# ---------- light reviews (a state flag on a changelist) ----------

REVIEW_STATES = ("open", "approved", "needs-work")

_REVIEW_COLS = "change_num, state, opened_by, created, updated, updated_by"


def _review_row(row):
    keys = ["change", "state", "openedBy", "created", "updated", "updatedBy"]
    return dict(zip(keys, row))


def review_get(change):
    with _conn() as conn:
        row = conn.execute(
            f"SELECT {_REVIEW_COLS} FROM reviews WHERE change_num = ?", (change,)
        ).fetchone()
    return _review_row(row) if row else None


def reviews_for(changes):
    """Reviews for a batch of changelists, keyed by change number."""
    changes = list(changes)
    if not changes:
        return {}
    marks = ",".join("?" * len(changes))
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_REVIEW_COLS} FROM reviews WHERE change_num IN ({marks})",
            changes,
        ).fetchall()
    return {r[0]: _review_row(r) for r in rows}


def reviews_list(state=None, limit=200):
    with _conn() as conn:
        if state:
            rows = conn.execute(
                f"SELECT {_REVIEW_COLS} FROM reviews WHERE state = ?"
                " ORDER BY updated DESC LIMIT ?", (state, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_REVIEW_COLS} FROM reviews ORDER BY updated DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_review_row(r) for r in rows]


def review_events(change):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, user, created, state, note FROM review_events"
            " WHERE change_num = ? ORDER BY created", (change,),
        ).fetchall()
    return [dict(zip(["id", "user", "created", "state", "note"], r)) for r in rows]


def review_set(change, user, state, note=None):
    """Set a changelist's review state and log the transition. Creates
    the review on first use; anyone may move it to any state."""
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO reviews (change_num, state, opened_by, created, updated, updated_by)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(change_num) DO UPDATE SET"
            " state = excluded.state, updated = excluded.updated,"
            " updated_by = excluded.updated_by",
            (change, state, user, now, now, user),
        )
        conn.execute(
            "INSERT INTO review_events (change_num, user, created, state, note)"
            " VALUES (?, ?, ?, ?, ?)",
            (change, user, now, state, note),
        )
    return review_get(change)


def review_delete(change):
    with _conn() as conn:
        conn.execute("DELETE FROM review_events WHERE change_num = ?", (change,))
        conn.execute("DELETE FROM reviews WHERE change_num = ?", (change,))


# ---------- open-thread counts (badges) ----------


def _count_rows(rows):
    return {k: {"open": o, "total": t} for k, o, t in rows}


def comment_counts_for_changes(changes):
    """Open and total thread counts per changelist. A thread is a root
    comment; replies ride along with it, and a resolved thread stops
    counting as open."""
    changes = list(changes)
    if not changes:
        return {}
    marks = ",".join("?" * len(changes))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT change_num,"
            " SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END), COUNT(*)"
            f" FROM comments WHERE parent IS NULL AND deleted = 0"
            f" AND change_num IN ({marks}) GROUP BY change_num",
            changes,
        ).fetchall()
    return _count_rows(rows)


def comment_counts_for_dir(prefix):
    """Counts per file directly under a depot directory (no recursion:
    the listing only shows that level)."""
    base = prefix.rstrip("/")
    # A depot path may legitimately contain % or _, which LIKE would read
    # as wildcards.
    escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _conn() as conn:
        rows = conn.execute(
            "SELECT path,"
            " SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END), COUNT(*)"
            " FROM comments WHERE parent IS NULL AND deleted = 0"
            " AND path LIKE ? ESCAPE '\\' GROUP BY path",
            (escaped + "/%",),
        ).fetchall()
    depth = base.count("/") + 1
    return _count_rows([r for r in rows if r[0].count("/") == depth])


# ---------- @mentions ----------

# A p4 user name in a comment body. Names are letters, digits, dots,
# dashes and underscores here; the trailing set excludes a final dot so
# "@alice." at the end of a sentence mentions alice.
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]|[A-Za-z0-9_])")


def mention_names(body):
    """Candidate user names named in a comment body, in order."""
    seen = []
    for m in MENTION_RE.finditer(body or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def mentions_add(comment_id, users):
    if not users:
        return
    now = time.time()
    with _conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO mentions (comment_id, user, created, seen)"
            " VALUES (?, ?, ?, 0)",
            [(comment_id, u, now) for u in users],
        )


def mentions_replace(comment_id, users):
    """After an edit, the mention set is whatever the new body says. Rows
    that survive keep their seen flag."""
    with _conn() as conn:
        if users:
            marks = ",".join("?" * len(users))
            conn.execute(
                f"DELETE FROM mentions WHERE comment_id = ? AND user NOT IN ({marks})",
                [comment_id, *users],
            )
        else:
            conn.execute("DELETE FROM mentions WHERE comment_id = ?", (comment_id,))
    mentions_add(comment_id, users)


def mentions_drop(comment_id):
    with _conn() as conn:
        conn.execute("DELETE FROM mentions WHERE comment_id = ?", (comment_id,))


def mentions_for(user, unseen_only=False, limit=100):
    """Mentions of `user`, newest first, each joined to its comment."""
    where = "m.user = ? AND c.deleted = 0" + (" AND m.seen = 0" if unseen_only else "")
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT m.id, m.seen, m.created, {_COMMENT_COLS.replace('id,', 'c.id,').replace(', ', ', c.')}"
            " FROM mentions m JOIN comments c ON c.id = m.comment_id"
            f" WHERE {where} ORDER BY m.created DESC LIMIT ?",
            (user, limit),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "seen": bool(r[1]),
            "created": r[2],
            "comment": _comment_row(r[3:]),
        })
    return out


def mentions_unseen_count(user):
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM mentions m JOIN comments c ON c.id = m.comment_id"
            " WHERE m.user = ? AND m.seen = 0 AND c.deleted = 0", (user,),
        ).fetchone()
    return row[0] if row else 0


def mentions_mark_seen(user, ids=None):
    with _conn() as conn:
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE mentions SET seen = 1 WHERE user = ? AND id IN ({marks})",
                [user, *ids],
            )
        else:
            conn.execute("UPDATE mentions SET seen = 1 WHERE user = ?", (user,))
