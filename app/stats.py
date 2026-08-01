"""Submit-activity aggregates for the Stats page.

Three axes over the same filter set — when did people submit, where in
the depot, and who — so that narrowing one narrows the others: "the
last 90 days, under //depot/tools, by author" is one question, not
three.

Everything is read out of the per-user change index (app/index.py).
That is not a shortcut: a month-by-month count from live p4 means
walking the whole submitted history on every load, and the index was
built with the user's own ticket, so these numbers already respect
Perforce protections. The consequence worth stating out loud, and the
UI does state it: two users can see different totals, because they can
see different changes.

The path axis reads `change_dirs`, the rollup written alongside the
index; folding `change_files` live measured 28s against a real index.

One definition, because it shows up the moment anyone checks these
numbers against p4: a submit here is a change that touched at least one
file. The index is built with `p4 changes //...`, which is how p4
itself counts when given a path — a changelist that recorded no files
(an emptied `p4 populate`, say) is in `p4 changes -u alice` and not in
`p4 changes -u alice //...`, and not here either.
"""

import calendar
import re
import threading
import time
from datetime import date, datetime, timedelta

from . import index as change_index

# The four bucket sizes, as the SQL that turns an epoch into a bucket
# key. The timezone modifier is bound as a parameter, never formatted
# in.
#
# Week deliberately avoids strftime('%G-W%V'): ISO week numbers landed
# in SQLite 3.46, newer than the container base image ships, and a
# feature that works on the dev box and not in Docker is worse than one
# that doesn't exist. `weekday 1` then back a week is the Monday of
# that week on every version, and the date reads better on an axis than
# a week number anyway.
_BUCKET_SQL = {
    "day": "strftime('%Y-%m-%d', c.time, 'unixepoch', ?)",
    "week": "date(c.time, 'unixepoch', ?, 'weekday 1', '-7 days')",
    "month": "strftime('%Y-%m', c.time, 'unixepoch', ?)",
    "year": "strftime('%Y', c.time, 'unixepoch', ?)",
}

# Above these the chart is unreadable and the payload silly, so the
# bucket is promoted a size and the response says which size it used.
_MAX_BUCKETS = {"day": 731, "week": 520, "month": 240, "year": 100}
_PROMOTE = {"day": "week", "week": "month", "month": "year"}

MAX_DEPTH = change_index.MAX_DIR_DEPTH
# '//depot/area' — the depth the summary tile counts and the depth the
# path chart starts at, because it is the one people name out loud.
AREA_DEPTH = 2
DEFAULT_LIMIT = 20
MAX_LIMIT = 200

# Repeated toggling of the same filters is the normal way to read this
# page, and every answer is a fresh scan. A minute of staleness is
# invisible next to that; a refresh drops the entries early.
CACHE_TTL = 60
CACHE_MAX = 64
_cache: dict = {}
_cache_lock = threading.Lock()


class StatsError(ValueError):
    """Bad input from the client; the route turns this into a 400."""


# ---------- input validation ----------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def tz_modifier(minutes):
    """A SQLite datetime modifier for the client's UTC offset.

    Perforce stores submit times as UTC epochs. Folded raw, a Korean
    morning submit lands on the previous day — so the browser sends its
    offset and the buckets are cut in the reader's own day. It is a
    fixed offset, so a DST boundary can misplace a single day; proper
    IANA handling is outside SQLite and outside this page.
    """
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise StatsError("tz must be a whole number of minutes")
    if not -1440 <= minutes <= 1440:
        raise StatsError("tz out of range")
    return f"{minutes:+d} minutes", minutes


def _parse_date(value, end=False, tz_minutes=0):
    """YYYY-MM-DD in the *client's* timezone -> epoch seconds.

    Deliberately not main.py's _date_epoch, which resolves the day in
    the server's timezone: a range picked in Seoul must line up with
    buckets cut in Seoul, whatever the server thinks the date is.
    """
    if not _DATE_RE.match(value or ""):
        raise StatsError(f"Invalid date: {value} (YYYY-MM-DD)")
    y, m, d = (int(part) for part in value.split("-"))
    try:
        stamp = datetime(y, m, d, 23, 59, 59) if end else datetime(y, m, d)
    except ValueError:
        raise StatsError(f"Invalid date: {value}")
    return calendar.timegm(stamp.timetuple()) - tz_minutes * 60


def _clean_path(value):
    """A depot directory, exactly as change_dirs stores it."""
    path = (value or "").strip()
    if not path:
        return None
    while path.endswith(("/", ".")):
        path = path[:-1]
    if not path.startswith("//") or len(path) < 4:
        raise StatsError("Path must start with // and name a directory")
    return path


def _clean_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise StatsError("limit must be a number")
    return max(1, min(limit, MAX_LIMIT))


def _clean_depth(value):
    try:
        depth = int(value)
    except (TypeError, ValueError):
        raise StatsError("depth must be a number")
    if not 1 <= depth <= MAX_DEPTH:
        raise StatsError(f"depth must be between 1 and {MAX_DEPTH}")
    return depth


class Filters:
    """The filter set all three axes share."""

    def __init__(self, date_from=None, date_to=None, path=None, user=None, tz=0):
        self.modifier, self.tz_minutes = tz_modifier(tz)
        self.date_from = _parse_date(date_from, tz_minutes=self.tz_minutes) if date_from else None
        self.date_to = _parse_date(date_to, end=True, tz_minutes=self.tz_minutes) if date_to else None
        self.path = _clean_path(path)
        self.user = (user or "").strip() or None

    def where(self):
        """SQL fragments and arguments against `changes c`."""
        clauses, args = [], []
        if self.date_from is not None:
            clauses.append("c.time >= ?")
            args.append(self.date_from)
        if self.date_to is not None:
            clauses.append("c.time <= ?")
            args.append(self.date_to)
        if self.user:
            clauses.append("c.user = ?")
            args.append(self.user)
        if self.path:
            # An exact match on a rolled-up directory, not a LIKE: a
            # prefix match can't use the index and would let //a/bc
            # answer for //a/b. The change's *other* directories stay
            # visible, which is what makes drill-down work.
            clauses.append(
                "EXISTS (SELECT 1 FROM change_dirs f "
                "WHERE f.change = c.change AND f.dir = ?)"
            )
            args.append(self.path)
        return clauses, args

    def key(self):
        return (self.date_from, self.date_to, self.path, self.user, self.tz_minutes)


# ---------- cache ----------


def _cached(user, name, key, produce):
    now = time.time()
    full_key = (user, name, key)
    with _cache_lock:
        hit = _cache.get(full_key)
        if hit and hit[0] > now:
            return hit[1]
    value = produce()
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            for stale in [k for k, v in _cache.items() if v[0] <= now][:CACHE_MAX]:
                _cache.pop(stale, None)
            if len(_cache) >= CACHE_MAX:
                _cache.pop(next(iter(_cache)), None)
        _cache[full_key] = (now + CACHE_TTL, value)
    return value


def invalidate(user=None):
    """Drop cached answers — called when the index gains changes."""
    with _cache_lock:
        for key in [k for k in _cache if user is None or k[0] == user]:
            _cache.pop(key, None)


# ---------- bucket arithmetic ----------


_EPOCH = datetime(1970, 1, 1)


def _local(epoch, tz_minutes):
    return _EPOCH + timedelta(seconds=epoch + tz_minutes * 60)


def _bucket_key(moment, bucket):
    if bucket == "day":
        return moment.strftime("%Y-%m-%d")
    if bucket == "week":
        monday = moment.date() - timedelta(days=moment.weekday())
        return monday.strftime("%Y-%m-%d")
    if bucket == "month":
        return moment.strftime("%Y-%m")
    return moment.strftime("%Y")


def _next_bucket(key, bucket):
    if bucket == "day":
        return (date.fromisoformat(key) + timedelta(days=1)).strftime("%Y-%m-%d")
    if bucket == "week":
        return (date.fromisoformat(key) + timedelta(days=7)).strftime("%Y-%m-%d")
    if bucket == "month":
        year, month = (int(p) for p in key.split("-"))
        return f"{year + 1:04d}-01" if month == 12 else f"{year:04d}-{month + 1:02d}"
    return str(int(key) + 1)


def _bucket_span(first, last, bucket):
    """How many buckets of this size the range covers."""
    days = max((last - first) // 86400, 0) + 1
    if bucket == "day":
        return days
    if bucket == "week":
        return days // 7 + 1
    if bucket == "month":
        return days // 28 + 1
    return days // 365 + 1


def _fill(rows, bucket, first, last, tz_minutes):
    """Insert the buckets nobody submitted in.

    Without this a quiet week isn't a gap in the chart, it's a missing
    column, and the bars either side sit next to each other as if the
    time between them never happened.
    """
    counts = {key: value for key, value in rows}
    key = _bucket_key(_local(first, tz_minutes), bucket)
    end = _bucket_key(_local(last, tz_minutes), bucket)
    out = []
    guard = 0
    while guard <= max(_MAX_BUCKETS.values()) + 2:
        out.append({"bucket": key, "changes": counts.get(key, 0)})
        if key >= end:
            break
        key = _next_bucket(key, bucket)
        guard += 1
    return out


# ---------- queries ----------


def coverage(user):
    """What the index actually holds, so the page can say what these
    numbers are made of instead of implying they cover all of history."""
    conn = change_index._conn(user)
    try:
        row = conn.execute("SELECT COUNT(*), MIN(time), MAX(time) FROM changes").fetchone()
        status = change_index._status(conn)
        dirs = change_index._dirs_status(conn, user)
    finally:
        conn.close()
    return {
        "changes": int(row[0] or 0),
        "first": int(row[1] or 0),
        "last": int(row[2] or 0),
        "oldestChange": status["oldest"],
        "newestChange": status["newest"],
        "fullyBackfilled": status["fullyBackfilled"],
        "updated": status["updated"],
        "dirs": dirs,
    }


def _range(conn, filters):
    """The span actually covered by the filtered changes."""
    clauses, args = filters.where()
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    row = conn.execute(
        f"SELECT MIN(c.time), MAX(c.time), COUNT(*) FROM changes c{where}", args
    ).fetchone()
    return (int(row[0]) if row[0] is not None else None,
            int(row[1]) if row[1] is not None else None,
            int(row[2] or 0))


def timeline(user, bucket="month", filters=None):
    """Submits per day/week/month/year, empty buckets included."""
    filters = filters or Filters()
    if bucket not in _BUCKET_SQL:
        raise StatsError("bucket must be day, week, month or year")

    def produce():
        conn = change_index._conn(user)
        try:
            first, last, total = _range(conn, filters)
            if not total:
                return {"bucket": bucket, "requestedBucket": bucket,
                        "rows": [], "total": 0, "promoted": False}
            size = bucket
            while size in _PROMOTE and _bucket_span(first, last, size) > _MAX_BUCKETS[size]:
                size = _PROMOTE[size]
            clauses, args = filters.where()
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            expression = _BUCKET_SQL[size]
            rows = conn.execute(
                f"SELECT {expression} AS b, COUNT(*) FROM changes c{where} "
                "GROUP BY b ORDER BY b",
                [filters.modifier] + args,
            ).fetchall()
            return {
                "bucket": size,
                "requestedBucket": bucket,
                "promoted": size != bucket,
                "rows": _fill(rows, size, first, last, filters.tz_minutes),
                "total": total,
            }
        finally:
            conn.close()

    return _cached(user, "timeline", (bucket, filters.key()), produce)


def paths(user, depth=2, filters=None, limit=DEFAULT_LIMIT):
    """Top directories at one depth, plus what that depth cannot show."""
    filters = filters or Filters()
    depth = _clean_depth(depth)
    limit = _clean_limit(limit)

    def produce():
        conn = change_index._conn(user)
        try:
            clauses, args = filters.where()
            where = " AND ".join(["d.depth = ?"] + clauses)
            rows = conn.execute(
                "SELECT d.dir, COUNT(DISTINCT d.change) AS n "
                "FROM change_dirs d JOIN changes c ON c.change = d.change "
                f"WHERE {where} GROUP BY d.dir ORDER BY n DESC, d.dir LIMIT ?",
                [depth] + args + [limit + 1],
            ).fetchall()
            truncated = len(rows) > limit
            rows = rows[:limit]
            covered = conn.execute(
                "SELECT COUNT(DISTINCT d.change) "
                "FROM change_dirs d JOIN changes c ON c.change = d.change "
                f"WHERE {where}",
                [depth] + args,
            ).fetchone()[0]
            _, _, total = _range(conn, filters)
            return {
                "depth": depth,
                "rows": [{"dir": r[0], "changes": int(r[1])} for r in rows],
                "total": total,
                # Changes whose files all sit shallower than this depth
                # belong to no bar at all. Unreported, the chart just
                # quietly shrinks as you go deeper.
                "uncovered": max(total - int(covered or 0), 0),
                "truncated": truncated,
                "maxDepth": MAX_DEPTH,
            }
        finally:
            conn.close()

    return _cached(user, "paths", (depth, limit, filters.key()), produce)


def users(user, filters=None, limit=DEFAULT_LIMIT):
    """Top authors, most submits first."""
    filters = filters or Filters()
    limit = _clean_limit(limit)

    def produce():
        conn = change_index._conn(user)
        try:
            clauses, args = filters.where()
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT c.user, COUNT(*) AS n FROM changes c{where} "
                "GROUP BY c.user ORDER BY n DESC, c.user LIMIT ?",
                args + [limit + 1],
            ).fetchall()
            truncated = len(rows) > limit
            rows = rows[:limit]
            _, _, total = _range(conn, filters)
            return {
                "rows": [{"user": r[0] or "(unknown)", "changes": int(r[1])} for r in rows],
                "total": total,
                "truncated": truncated,
            }
        finally:
            conn.close()

    return _cached(user, "users", (limit, filters.key()), produce)


def summary(user, filters=None):
    """The headline numbers: submits, authors, directories, per-day rate."""
    filters = filters or Filters()

    def produce():
        conn = change_index._conn(user)
        try:
            clauses, args = filters.where()
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            total, first, last, authors = conn.execute(
                "SELECT COUNT(*), MIN(c.time), MAX(c.time), COUNT(DISTINCT c.user) "
                f"FROM changes c{where}",
                args,
            ).fetchone()
            # Top-level areas, not every directory: the depot has a
            # quarter of a million of those, which is a number nobody
            # can do anything with, and counting them distinctly runs
            # ~1.5s. Depth 2 ('//depot/area') is the unit people
            # actually talk about, and the index makes it instant.
            area_clauses = " AND ".join(["d.depth = ?"] + clauses)
            dirs = conn.execute(
                "SELECT COUNT(DISTINCT d.dir) "
                "FROM change_dirs d JOIN changes c ON c.change = d.change "
                f"WHERE {area_clauses}",
                [AREA_DEPTH] + args,
            ).fetchone()[0]
            days = 0
            if total and first is not None:
                days = max((int(last) - int(first)) // 86400, 0) + 1
            return {
                "changes": int(total or 0),
                "authors": int(authors or 0),
                "dirs": int(dirs or 0),
                "first": int(first) if first is not None else 0,
                "last": int(last) if last is not None else 0,
                "days": days,
                "perDay": round(total / days, 2) if total and days else 0,
            }
        finally:
            conn.close()

    return _cached(user, "summary", filters.key(), produce)
