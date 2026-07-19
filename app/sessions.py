"""Session store mapping a browser cookie to a p4 ticket.

Backed by SQLite so sessions survive a server restart (the HANDOFF
"users get logged out on restart" item). Single-file DB in the data
directory; connections are opened per call, which is plenty for the
personal/small-team scale this app targets.
"""

import os
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


def comments_for(path=None, change=None):
    with _conn() as conn:
        if path:
            rows = conn.execute(
                f"SELECT {_COMMENT_COLS} FROM comments WHERE path = ? ORDER BY created",
                (path,),
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
