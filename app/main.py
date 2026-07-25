"""p4-web: a lightweight web UI for Perforce (read-only P4V alternative)."""

import mimetypes
import os
import re
import resource
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import Cookie, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import features, p4, sessions, workspace
from . import index as change_index  # avoid clashing with the def index() route


def _raise_fd_limit(target=4096):
    """Lift the open-file soft limit toward `target`.

    launchd hands agents a soft limit of 256 descriptors. A markdown
    page with a hundred images opens that many client sockets at once
    (HTTP/2 through `tailscale serve` multiplexes them all), and every
    /api/raw adds pipes for two p4 subprocesses on top — enough to hit
    EMFILE, which showed up as the later images on a long page silently
    failing to load. The hard limit is unlimited here, so just ask for
    more."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ValueError, OSError):
        pass  # best effort: the concurrency cap below still bounds the damage


_raise_fd_limit()

# Say what an operator changed — and only that, so a default instance
# stays quiet — plus anything unparseable in the config, which is
# otherwise silently ignored.
for _note in features.NOTES:
    print(f"p4-web: feature config: {_note}", flush=True)
if features.describe():
    print(f"p4-web: features {features.describe()}", flush=True)

app = FastAPI(title="p4-web")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

COOKIE_NAME = "p4web_session"


# How often a live session re-asks Perforce how much longer its ticket
# is good for. The session then tracks that expiry, so you stay logged
# in for exactly as long as the ticket lasts — and get dropped promptly
# once it's revoked elsewhere.
TICKET_RECHECK = 15 * 60


def _ticket_ttl(user, ticket):
    """Remaining ticket lifetime in seconds, or None if unknown."""
    try:
        return p4.ticket_seconds_left(user, ticket)
    except Exception:
        return None


def require_session(sid):
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    if time.time() - session.get("checked", 0) >= TICKET_RECHECK:
        try:
            ttl = p4.ticket_seconds_left(session["user"], session["ticket"])
        except p4.P4AuthError:
            sessions.destroy(sid)
            raise HTTPException(
                status_code=401,
                detail={"message": "Your Perforce ticket expired — please log in again.", "raw": ""},
            )
        except Exception:
            # Server hiccup, not a bad ticket: keep the session and try
            # again next window rather than re-running p4 every request.
            sessions.refresh(sid)
        else:
            sessions.refresh(sid, ttl)
            session = sessions.get(sid) or session
    return session


def require_feature(name, session):
    """Gate a route on a feature flag (see app/features.py).

    Two different noes, deliberately distinguishable by a client:
    the server policy turned it off, so as far as anyone outside can
    tell this build hasn't got it (404); or this user switched it off
    for themselves, which a tab left open from before the change should
    be able to explain (403).

    This never replaces an authorization check — _require_own_change,
    the workspace path guard, the command allowlist and the rate limit
    all still run. A flag is one more gate in front of them.
    """
    if not features.SERVER.get(name, True):
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"The {name} feature is turned off on this server.",
                "raw": "", "feature": name,
            },
        )
    if not features.effective(sessions.prefs(session["user"])).get(name, True):
        raise HTTPException(
            status_code=403,
            detail={
                "message": f"You have the {name} feature turned off in Settings.",
                "raw": "", "feature": name,
            },
        )


# Common p4 error texts -> (HTTP status, message a non-admin can act on).
# The raw p4 output still travels alongside for debugging.
FRIENDLY_ERRORS = [
    (re.compile(r"password \(P4PASSWD\) invalid or unset|^Password invalid", re.I), 401,
     "Invalid user name or password."),
    (re.compile(r"User \S+ doesn't exist", re.I), 401,
     "Invalid user name or password."),
    (re.compile(r"session has expired|please login again", re.I), 401,
     "Your Perforce session expired — please log in again."),
    (re.compile(r"Connect to server failed|TCP connect .* failed", re.I), 503,
     "Cannot reach the Perforce server. Check that it is running and that P4PORT is right."),
    (re.compile(r"no such file\(s\)", re.I), 404,
     "No matching files in the depot."),
    (re.compile(r"no file\(s\) at that changelist number", re.I), 404,
     "No files at that changelist number."),
    (re.compile(r"must refer to client|no such area", re.I), 404,
     "Unknown depot path — check the spelling."),
    (re.compile(r"protected namespace|no permission for operation|access for user .* denied", re.I), 403,
     "You don't have permission for this path."),
    (re.compile(r"no such changelist|Change \d+ unknown", re.I), 404,
     "That changelist doesn't exist."),
    (re.compile(r"not under client|not in client view", re.I), 400,
     "That path is outside the visible depot view."),
    (re.compile(r"maxresults|maxscanrows|too many rows scanned", re.I), 400,
     "The query hit a server result limit — narrow the path or filters."),
]


def _friendly(message):
    for pattern, status, text in FRIENDLY_ERRORS:
        if pattern.search(message):
            return status, text
    # Unknown errors: pass the first p4 line through, it's usually terse.
    first = message.strip().splitlines()[0] if message.strip() else "Perforce command failed."
    return 400, first


def p4_call(fn, *args, **kwargs):
    """Translate p4/workspace failures into HTTP errors with friendly
    messages."""
    try:
        return fn(*args, **kwargs)
    except p4.P4AuthError as e:
        _, text = _friendly(str(e))
        raise HTTPException(status_code=401, detail={"message": text, "raw": str(e)})
    except p4.P4Error as e:
        status, text = _friendly(str(e))
        raise HTTPException(status_code=status, detail={"message": text, "raw": str(e)})
    except workspace.WorkspaceError as e:
        raise HTTPException(status_code=400, detail={"message": str(e), "raw": ""})


class LoginRequest(BaseModel):
    user: str
    password: str


# Session cookies get the Secure flag when the app is served over
# HTTPS (tailscale serve, a reverse proxy, ...).
SECURE_COOKIES = os.environ.get("P4WEB_SECURE_COOKIES", "") == "1"

# ---------- login throttle ----------
# Sliding window per client IP and per target account. In-memory is
# fine: a restart resetting counters is acceptable, and the store is
# bounded by pruning on every check.

LOGIN_WINDOW = 60          # seconds
LOGIN_MAX_PER_IP = 10      # attempts per window
LOGIN_MAX_PER_USER = 5

_login_attempts: dict[str, list[float]] = {}


def _throttle_prune(now):
    for k in list(_login_attempts):
        fresh = [t for t in _login_attempts[k] if now - t < LOGIN_WINDOW]
        if fresh:
            _login_attempts[k] = fresh
        else:
            del _login_attempts[k]


def _throttle_check(key, limit):
    now = time.time()
    # Sweep stale keys before they accumulate: a flood of logins with
    # distinct usernames would otherwise leave a map entry per name and
    # grow memory without bound.
    if len(_login_attempts) > 4096:
        _throttle_prune(now)
    attempts = [t for t in _login_attempts.get(key, []) if now - t < LOGIN_WINDOW]
    if attempts:
        _login_attempts[key] = attempts
    else:
        _login_attempts.pop(key, None)
    if len(attempts) >= limit:
        raise HTTPException(
            status_code=429,
            detail={"message": "Too many login attempts — wait a minute and try again.", "raw": ""},
        )


def _throttle_record(key):
    _login_attempts.setdefault(key, []).append(time.time())


# ---------- global request throttle ----------
# A sliding per-session (or per-IP when unauthenticated) budget over all
# /api/ endpoints, so one client can't hammer expensive p4 commands.
# Generous enough that normal UI use never trips it; login has its own,
# stricter throttle above and is excluded here.

API_RATE_WINDOW = 60
API_RATE_MAX = int(os.environ.get("P4WEB_API_RATE_MAX", "600"))  # requests/window

_api_hits: dict[str, list[float]] = {}


def _api_throttle(key, now):
    if len(_api_hits) > 8192:
        for k in list(_api_hits):
            fresh = [t for t in _api_hits[k] if now - t < API_RATE_WINDOW]
            if fresh:
                _api_hits[k] = fresh
            else:
                del _api_hits[k]
    hits = [t for t in _api_hits.get(key, []) if now - t < API_RATE_WINDOW]
    if len(hits) >= API_RATE_MAX:
        _api_hits[key] = hits
        return True
    hits.append(now)
    _api_hits[key] = hits
    return False


@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/info", "/api/login"):
        sid = request.cookies.get(COOKIE_NAME)
        ip = request.client.host if request.client else "?"
        key = f"s:{sid}" if sid else f"ip:{ip}"
        if _api_throttle(key, time.time()):
            return JSONResponse(
                status_code=429,
                content={"detail": {"message": "Too many requests — slow down a moment.", "raw": ""}},
            )
    return await call_next(request)


@app.get("/api/info")
def info():
    """Unauthenticated: which Perforce server this UI talks to."""
    return {"p4port": p4.P4PORT}


@app.post("/api/login")
def login(body: LoginRequest, request: Request, response: Response):
    user = body.user.strip()
    ip = request.client.host if request.client else "unknown"
    _throttle_check(f"ip:{ip}", LOGIN_MAX_PER_IP)
    _throttle_check(f"user:{user}", LOGIN_MAX_PER_USER)
    try:
        ticket, owned = p4.login(user, body.password)
    except p4.P4AuthError as e:
        _throttle_record(f"ip:{ip}")
        _throttle_record(f"user:{user}")
        _, text = _friendly(str(e))
        raise HTTPException(status_code=401, detail={"message": text, "raw": str(e)})
    _login_attempts.pop(f"user:{user}", None)
    ttl = _ticket_ttl(user, ticket)
    sid = sessions.create(user, ticket, owned_ticket=owned, ttl=ttl)
    _set_session_cookie(response, sid, ttl)
    # Same shape as /api/me: the SPA gates its chrome on this straight
    # after logging in, without reloading the page.
    return {
        "user": user,
        "p4port": p4.P4PORT,
        "features": features.effective(sessions.prefs(user)),
    }


def _set_session_cookie(response, sid, ttl):
    """Cookie lifetime follows the ticket, so the browser keeps the
    session across restarts for as long as Perforce would."""
    age = sessions.SESSION_TTL if ttl is None else int(ttl)
    response.set_cookie(
        COOKIE_NAME, sid, httponly=True, samesite="lax",
        max_age=min(age, sessions.MAX_COOKIE_AGE), secure=SECURE_COOKIES,
    )


@app.post("/api/logout")
def logout(response: Response, p4web_session: str | None = Cookie(default=None)):
    session = sessions.destroy(p4web_session)
    # Only invalidate tickets we minted ourselves. A pasted ticket is
    # shared with the user's other clients (P4V, CLI) and killing it
    # server-side would log those out too.
    if session and session.get("owned_ticket"):
        p4.logout(session["user"], session["ticket"])
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/me")
def me(response: Response, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    # Re-stamp the cookie on every page load: the ticket may have been
    # extended since login, and a cookie capped at 400 days would
    # otherwise lapse before a longer-lived ticket does.
    _set_session_cookie(
        response, p4web_session, max(session["expires"] - time.time(), 60)
    )
    # The effective flags ride along so the SPA can gate its chrome on
    # the first load it already makes, instead of a second round trip.
    return {
        "user": session["user"],
        "p4port": p4.P4PORT,
        "features": features.effective(sessions.prefs(session["user"])),
    }


# ---------- feature switches ----------


class PrefsBody(BaseModel):
    features: dict[str, bool]


def _features_payload(user):
    prefs = sessions.prefs(user)
    return {
        "server": features.server_policy(),
        "user": prefs,
        "effective": features.effective(prefs),
        "locked": features.locked(),
        "depends": features.DEPENDS,
    }


@app.get("/api/features")
def get_features(p4web_session: str | None = Cookie(default=None)):
    """What this instance allows, what this user chose, and the result.

    Behind the session gate: which features an instance runs is part of
    its configuration, and there's no reason to hand that to someone who
    hasn't logged in."""
    session = require_session(p4web_session)
    return _features_payload(session["user"])


@app.put("/api/prefs")
def set_prefs(body: PrefsBody, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    wanted = {}
    for key, value in body.features.items():
        if key not in features.DEFAULTS:
            raise HTTPException(status_code=400, detail=f"Unknown feature {key!r}")
        if value and not features.SERVER.get(key, True):
            # Refuse rather than store-and-ignore: a switch that springs
            # back on the next load is worse than a clear no.
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"The {key} feature is turned off on this server.",
                    "raw": "", "feature": key,
                },
            )
        wanted[key] = bool(value)
    if wanted:
        sessions.prefs_set(session["user"], wanted)
    return _features_payload(session["user"])


# ---------- depot browsing ----------

MAX_TEXT_BYTES = 2 * 1024 * 1024  # refuse to inline files larger than this

BINARY_TYPE_MARKERS = ("binary", "ubinary", "apple", "resource")


def _normalize_dir(path):
    path = (path or "").strip()
    if path in ("", "/", "//"):
        return ""
    if not path.startswith("//"):
        raise HTTPException(status_code=400, detail="Path must start with //")
    return path.rstrip("/")


@app.get("/api/browse")
def browse(path: str = "", p4web_session: str | None = Cookie(default=None)):
    """List one directory level: subdirectories and files.

    With an empty path, lists depots as the root level.
    """
    session = require_session(p4web_session)
    user, ticket = session["user"], session["ticket"]
    path = _normalize_dir(path)

    if not path:
        depots = p4_call(p4.run, ["depots"], user, ticket)
        dirs = [
            {"path": f"//{d['name']}", "name": d["name"], "desc": d.get("desc", "").strip()}
            for d in depots
        ]
        return {"path": "", "dirs": sorted(dirs, key=lambda d: d["name"]), "files": []}

    pattern = p4.escape_path(path) + "/*"
    dirs = p4_call(p4.run, ["dirs", pattern], user, ticket)
    files = p4_call(p4.run, ["files", "-e", pattern], user, ticket)
    return {
        "path": path,
        "dirs": [
            {"path": d["dir"], "name": d["dir"].rsplit("/", 1)[-1]}
            for d in dirs
        ],
        "files": [
            {
                "path": f["depotFile"],
                "name": f["depotFile"].rsplit("/", 1)[-1],
                "rev": int(f["rev"]),
                "change": int(f["change"]),
                "action": f["action"],
                "type": f["type"],
                "time": int(f.get("time", 0)),
            }
            for f in files
        ],
    }


@app.get("/api/file")
def file_content(
    path: str,
    rev: int | None = None,
    p4web_session: str | None = Cookie(default=None),
):
    """File metadata plus inline content for reasonably sized text files."""
    session = require_session(p4web_session)
    user, ticket = session["user"], session["ticket"]
    if not path.startswith("//"):
        raise HTTPException(status_code=400, detail="Path must start with //")

    head_stats = p4_call(p4.run, ["fstat", "-Ol", p4.escape_path(path)], user, ticket)
    if not head_stats:
        raise HTTPException(status_code=404, detail="File not found")
    head_rev = int(head_stats[0].get("headRev", 0))

    spec = p4.escape_path(path) + (f"#{rev}" if rev else "")
    if rev and rev != head_rev:
        stats = p4_call(p4.run, ["fstat", "-Ol", spec], user, ticket)
        if not stats:
            raise HTTPException(status_code=404, detail="Revision not found")
        st = stats[0]
    else:
        st = head_stats[0]
    shown_rev = rev or head_rev
    ftype = st.get("headType", "text")
    action = st.get("headAction", "")
    size = int(st.get("fileSize", 0))
    is_binary = any(m in ftype for m in BINARY_TYPE_MARKERS)
    deleted = action in ("delete", "move/delete") and not rev

    result = {
        "path": path,
        "rev": shown_rev,
        "headRev": head_rev,
        "type": ftype,
        "action": action,
        "size": size,
        "change": int(st.get("headChange", 0)),
        "time": int(st.get("headTime", 0)),
        "binary": is_binary,
        "deleted": deleted,
        "truncated": False,
        "content": None,
    }
    if deleted or is_binary:
        return result
    if size > MAX_TEXT_BYTES:
        result["truncated"] = True
        return result
    raw = p4_call(p4.run_text, ["print", "-q", spec], user, ticket)
    result["content"] = raw.decode("utf-8", errors="replace")
    return result


# ---------- search ----------


def _normalize_scope(path):
    """Turn a user-supplied scope into a //.../... filespec."""
    path = (path or "").strip()
    if not path:
        return "//..."
    while path.endswith(("/", ".")):
        path = path[:-1]
    if not path.startswith("//"):
        raise HTTPException(status_code=400, detail="Scope must start with //")
    return p4.escape_path(path) + "/..."


@app.get("/api/search")
def search(
    q: str,
    kind: str = "files",
    path: str | None = None,
    case_sensitive: bool = False,
    p4web_session: str | None = Cookie(default=None),
):
    """Search file names (p4 files pattern) or contents (p4 grep)."""
    session = require_session(p4web_session)
    user, ticket = session["user"], session["ticket"]
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Empty query")
    scope = _normalize_scope(path)

    if kind == "files":
        # Substring match on the depot path; * and ... pass through so
        # power users can write their own wildcards.
        q_esc = q.replace("%", "%25").replace("@", "%40").replace("#", "%23")
        pattern = scope[:-3] + "..." + q_esc + "..."
        records = p4_call(p4.run, ["files", "-e", "-m", "500", pattern], user, ticket)
        return {
            "kind": "files",
            "results": [
                {
                    "path": r["depotFile"],
                    "rev": int(r["rev"]),
                    "change": int(r["change"]),
                    "action": r["action"],
                    "type": r["type"],
                    "time": int(r.get("time", 0)),
                }
                for r in records
            ],
            "warnings": [],
        }

    if kind == "content":
        if scope == "//...":
            raise HTTPException(
                status_code=400,
                detail="Content search needs a path scope (e.g. //depot/project) — searching every depot would scan the whole server.",
            )
        args = ["grep", "-n"]
        if not case_sensitive:
            args.append("-i")
        args += ["-e", q, scope]
        records, errors = p4_call(
            p4.run, args, user, ticket, collect_errors=True
        )
        matches = [
            {
                "path": r["depotFile"],
                "rev": int(r["rev"]),
                "line": int(r.get("line", 0)),
                "text": r.get("matchedLine", ""),
            }
            for r in records
            if "depotFile" in r
        ]
        # Deduplicate limit warnings; p4 repeats them per file.
        warnings = sorted(set(errors))[:3]
        return {"kind": "content", "results": matches[:2000], "warnings": warnings}

    raise HTTPException(status_code=400, detail="kind must be files or content")


def _content_disposition(disposition, name):
    """Build a Content-Disposition header safely. A depot filename may
    contain quotes, backslashes, or control characters that would break
    out of the quoted `filename=` value (header spoofing / injection);
    strip those for the ASCII fallback and carry the exact name in the
    RFC 6266 `filename*` field."""
    ascii_name = "".join(c for c in name if " " <= c < "\x7f" and c not in '"\\/') or "download"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


MAX_RAW_BYTES = 100 * 1024 * 1024

# Starting a raw stream costs an fstat subprocess plus a `p4 print`.
# A page full of images fires them all at once, so start a handful at
# a time and let the rest wait their turn — queuing is invisible to the
# browser, whereas failing is a permanently broken image. The slot
# covers only the setup: a holder never needs a worker thread to make
# progress, so waiters (which do occupy one) can't starve it.
RAW_CONCURRENCY = int(os.environ.get("P4WEB_RAW_CONCURRENCY", "8"))
_raw_slots = threading.BoundedSemaphore(RAW_CONCURRENCY)


@app.get("/api/raw")
def raw_content(
    path: str,
    rev: int | None = None,
    download: bool = False,
    p4web_session: str | None = Cookie(default=None),
):
    """Stream a file revision's bytes — image previews and downloads."""
    session = require_session(p4web_session)
    user, ticket = session["user"], session["ticket"]
    if not path.startswith("//"):
        raise HTTPException(status_code=400, detail="Path must start with //")
    spec = p4.escape_path(path) + (f"#{rev}" if rev else "")
    _raw_slots.acquire()
    try:
        stats = p4_call(p4.run, ["fstat", "-Ol", spec], user, ticket)
        if not stats:
            raise HTTPException(status_code=404, detail="File not found")
        # The cap guards the inline-preview path: an <img>/preview embedded in
        # the page would pull the whole file into the browser. An explicit
        # download streams straight to disk, so its size doesn't matter here —
        # the response never buffers in server memory either (see below).
        if not download and int(stats[0].get("fileSize", 0)) > MAX_RAW_BYTES:
            raise HTTPException(status_code=413, detail="File too large")
        # Stream straight from `p4 print` rather than buffering the whole
        # file (up to MAX_RAW_BYTES) in memory — a handful of concurrent
        # large downloads would otherwise be an easy memory-exhaustion DoS.
        proc, first = p4_call(p4.print_open, spec, user, ticket)
    finally:
        _raw_slots.release()
    # print_open() handed back a live subprocess. From here it MUST be
    # reaped on every exit path, so build the response inside a guard: if
    # anything below throws before the StreamingResponse owns the process,
    # kill it here rather than leak it.
    try:
        name = path.rsplit("/", 1)[-1]
        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
        disposition = "attachment" if download else "inline"
        return StreamingResponse(
            p4.print_drain(proc, first),
            media_type=media,
            # Guaranteed reaper. Starlette runs the background task after
            # the response completes AND after a mid-stream client
            # disconnect cancels it, so a browser that navigates away from
            # a page of image previews can no longer strand `p4 print`
            # processes as lingering IDLE p4d connections. Idempotent with
            # print_drain()'s own finally on the happy path.
            background=BackgroundTask(p4.terminate_stream, proc),
            headers={
                "Content-Disposition": _content_disposition(disposition, name),
                # Never execute depot content in the app's origin.
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except Exception:
        p4.terminate_stream(proc)
        raise


# ---------- history / diff / annotate ----------


@app.get("/api/filelog")
def filelog(path: str, p4web_session: str | None = Cookie(default=None)):
    """Revision history. Returns one segment per depot path the file
    has lived at (renames/branches produce multiple filelog records)."""
    session = require_session(p4web_session)
    if not path.startswith("//"):
        raise HTTPException(status_code=400, detail="Path must start with //")
    records = p4_call(
        p4.run, ["filelog", "-l", "-t", "-i", p4.escape_path(path)],
        session["user"], session["ticket"],
    )
    segments = []
    for rec in records:
        revs = []
        i = 0
        while f"rev{i}" in rec:
            integrations = []
            j = 0
            while f"how{i},{j}" in rec:
                integrations.append({
                    "how": rec[f"how{i},{j}"],
                    "file": rec.get(f"file{i},{j}", ""),
                    "srev": rec.get(f"srev{i},{j}", ""),
                    "erev": rec.get(f"erev{i},{j}", ""),
                })
                j += 1
            revs.append({
                "rev": int(rec[f"rev{i}"]),
                "change": int(rec[f"change{i}"]),
                "action": rec[f"action{i}"],
                "user": rec.get(f"user{i}", ""),
                "time": int(rec.get(f"time{i}", 0)),
                "type": rec.get(f"type{i}", ""),
                "size": int(rec[f"fileSize{i}"]) if rec.get(f"fileSize{i}") else None,
                "desc": rec.get(f"desc{i}", "").rstrip(),
                "integrations": integrations,
            })
            i += 1
        segments.append({"depotFile": rec.get("depotFile", path), "revs": revs})
    return {"path": path, "segments": segments}


# #rev, @change, @=shelved-change, @date, @label-ish names
REV_SPEC_RE = re.compile(r"^(#\d+|@=?\d+|@\d{4}/\d{2}/\d{2}(:\d{2}:\d{2}:\d{2})?|@[\w.-]+)$")


def _validate_spec(spec):
    if not REV_SPEC_RE.match(spec):
        raise HTTPException(status_code=400, detail=f"Invalid revision spec: {spec}")
    return spec


@app.get("/api/diff")
def diff(
    path: str,
    rev1: int | None = None,
    rev2: int | None = None,
    spec1: str | None = None,
    spec2: str | None = None,
    path2: str | None = None,
    p4web_session: str | None = Cookie(default=None),
):
    """Unified diff between two revision specs of one file (or across
    two depot paths, for renamed/branched files). Specs may be given
    as plain revision numbers (rev1/rev2) or as raw p4 specs like
    "#3", "@12345", "@=12345" (shelved) via spec1/spec2."""
    session = require_session(p4web_session)
    # Both paths must be real depot filespecs. Without this guard a value
    # like "-S" would reach `p4 diff2` as a flag-like token (argument
    # injection); every peer endpoint enforces the same "//" prefix.
    if not path.startswith("//"):
        raise HTTPException(status_code=400, detail="Path must start with //")
    if path2 is not None and not path2.startswith("//"):
        raise HTTPException(status_code=400, detail="Path must start with //")
    s1 = _validate_spec(spec1) if spec1 else (f"#{rev1}" if rev1 else None)
    s2 = _validate_spec(spec2) if spec2 else (f"#{rev2}" if rev2 else None)
    if not s1 or not s2:
        raise HTTPException(status_code=400, detail="Two revision specs required")
    left = p4.escape_path(path) + s1
    right = p4.escape_path(path2 or path) + s2
    raw = p4_call(
        p4.run_text, ["diff2", "-du", left, right],
        session["user"], session["ticket"],
    )
    return {
        "path": path, "spec1": s1, "spec2": s2,
        "diff": raw.decode("utf-8", errors="replace"),
    }


@app.get("/api/folderdiff")
def folderdiff(
    left: str,
    right: str,
    p4web_session: str | None = Cookie(default=None),
):
    """Compare two depot directories (optionally with @change / @date
    suffixes) via `p4 diff2 -q`: which files differ, or exist on only
    one side."""
    session = require_session(p4web_session)

    def to_spec(p):
        p = p.strip()
        base, at, rev = p.partition("@")
        while base.endswith(("/", ".")):
            base = base[:-1]
        if not base.startswith("//"):
            raise HTTPException(status_code=400, detail=f"Path must start with //: {p}")
        spec = p4.escape_path(base) + "/..."
        if at:
            _validate_spec("@" + rev)
            spec += "@" + rev
        return spec

    records = p4_call(
        p4.run, ["diff2", "-q", to_spec(left), to_spec(right)],
        session["user"], session["ticket"],
    )
    pairs = [
        {
            "status": r.get("status", ""),
            "leftFile": r.get("depotFile"),
            "leftRev": int(r["rev"]) if r.get("rev") else None,
            "rightFile": r.get("depotFile2"),
            "rightRev": int(r["rev2"]) if r.get("rev2") else None,
        }
        for r in records
        if r.get("status") != "identical"
    ]
    truncated = len(pairs) > 2000
    return {"left": left, "right": right, "pairs": pairs[:2000], "truncated": truncated}


@app.get("/api/annotate")
def annotate(
    path: str,
    rev: int | None = None,
    p4web_session: str | None = Cookie(default=None),
):
    """Per-line last-changed info (blame). Changelist numbers via -c."""
    session = require_session(p4web_session)
    user, ticket = session["user"], session["ticket"]
    head = p4_call(p4.run, ["fstat", p4.escape_path(path)], user, ticket)
    head_rev = int(head[0].get("headRev", 0)) if head else 0
    spec = p4.escape_path(path) + (f"#{rev}" if rev else "")
    records = p4_call(p4.run, ["annotate", "-c", "-u", spec], user, ticket)
    lines = [
        {
            "change": int(rec["lower"]),
            "user": rec.get("user", ""),
            "data": rec["data"].rstrip("\n"),
        }
        for rec in records
        if "data" in rec
    ]
    return {"path": path, "rev": rev or head_rev, "headRev": head_rev, "lines": lines}


# ---------- changelists ----------


DATE_RE = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}$")


def _p4_date(d):
    if not DATE_RE.match(d):
        raise HTTPException(status_code=400, detail=f"Invalid date: {d} (YYYY-MM-DD)")
    return d.replace("-", "/")


@app.get("/api/changes")
def changes(
    status: str = "submitted",
    user: str | None = None,
    path: str | None = None,
    text: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    max: int = 100,
    before: int | None = None,
    p4web_session: str | None = Cookie(default=None),
):
    """List changelists, newest first.

    `before` pages older results by limiting to changes numbered below
    it (submitted only, mutually exclusive with a date range).
    `text` is a case-insensitive description filter applied after the
    fetch; the response's `oldest`/`rawCount` let the client keep
    paging even when a page filters down to nothing.
    """
    session = require_session(p4web_session)
    if status not in ("submitted", "pending", "shelved"):
        raise HTTPException(status_code=400, detail="Invalid status")
    max = min(max, 500)
    args = ["changes", "-l", "-t", "-m", str(max), "-s", status]
    if user:
        args += ["-u", user]
    spec = None
    if path:
        # Accept "//depot/foo", "//depot/foo/" or "//depot/foo/..."
        path = path.strip()
        while path.endswith(("/", ".")):
            path = path[:-1]
        if not path.startswith("//"):
            raise HTTPException(status_code=400, detail="Path must start with //")
        spec = p4.escape_path(path) + "/..."
    if (date_from or date_to) and status == "submitted":
        lo = _p4_date(date_from) if date_from else "1970/01/01"
        # A bare @date means midnight; extend to end of day so the
        # "to" date is inclusive.
        hi = _p4_date(date_to) + ":23:59:59" if date_to else "now"
        spec = (spec or "//...") + f"@{lo},@{hi}"
    elif before and status == "submitted":
        spec = (spec or "//...") + f"@<{before}"
    if spec:
        args.append(spec)
    records = p4_call(p4.run, args, session["user"], session["ticket"])
    results = [
        {
            "change": int(r["change"]),
            "user": r.get("user", ""),
            "client": r.get("client", ""),
            "time": int(r.get("time", 0)),
            "status": r.get("status", status),
            "desc": r.get("desc", "").rstrip(),
        }
        for r in records
    ]
    raw_count = len(results)
    oldest = min((c["change"] for c in results), default=None)
    if text:
        needle = text.lower()
        results = [c for c in results if needle in c["desc"].lower()]
    return {"changes": results, "oldest": oldest, "rawCount": raw_count, "pageSize": max}


@app.get("/api/change/{change}")
def change_detail(change: int, p4web_session: str | None = Cookie(default=None)):
    """Changelist metadata plus its file list (submitted revisions,
    opened files for pending, or shelved files as a fallback)."""
    session = require_session(p4web_session)
    user, ticket = session["user"], session["ticket"]
    records = p4_call(p4.run, ["describe", "-s", str(change)], user, ticket)
    if not records:
        raise HTTPException(status_code=404, detail="Change not found")
    r = records[0]

    def flatten(rec):
        out = []
        i = 0
        while f"depotFile{i}" in rec:
            out.append({
                "path": rec[f"depotFile{i}"],
                "action": rec.get(f"action{i}", ""),
                "type": rec.get(f"type{i}", ""),
                "rev": int(rec[f"rev{i}"]) if rec.get(f"rev{i}") else None,
            })
            i += 1
        return out

    files = flatten(r)
    shelved = False
    if r.get("status") == "pending" and not files:
        shelf = p4_call(p4.run, ["describe", "-s", "-S", str(change)], user, ticket)
        if shelf:
            files = flatten(shelf[0])
            shelved = bool(files)
    fixes = p4_call(p4.run, ["fixes", "-c", str(change)], user, ticket)
    return {
        "change": int(r["change"]),
        "user": r.get("user", ""),
        "client": r.get("client", ""),
        "time": int(r.get("time", 0)),
        "status": r.get("status", ""),
        "desc": r.get("desc", "").rstrip(),
        "shelved": shelved,
        "files": files,
        "jobs": [f.get("Job", "") for f in fixes],
    }


# ---------- changelist search index ----------
#
# A per-user SQLite index (data/index/<user>.db) makes description /
# date-range / touched-filename search fast across the whole submitted
# history. Built with the requesting user's ticket, so it only ever
# holds changes that user is allowed to see.


def _date_epoch(d, end=False):
    """YYYY-MM-DD -> epoch seconds (local), start or end of the day."""
    if not DATE_RE.match(d):
        raise HTTPException(status_code=400, detail=f"Invalid date: {d} (YYYY-MM-DD)")
    y, m, day = (int(x) for x in re.split(r"[-/]", d))
    t = (23, 59, 59) if end else (0, 0, 0)
    return int(time.mktime((y, m, day, *t, 0, 0, -1)))


@app.get("/api/index/status")
def index_status(p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    require_feature("index", session)
    return change_index.status(session["user"])


@app.post("/api/index/refresh")
def index_refresh(
    request: Request,
    backfill: bool = True,
    p4web_session: str | None = Cookie(default=None),
):
    session = require_session(p4web_session)
    require_feature("index", session)
    return p4_call(change_index.refresh, session["user"], session["ticket"], backfill=backfill)


@app.get("/api/index/search")
def index_search(
    q: str | None = None,
    file: str | None = None,
    user: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    max: int = 200,
    p4web_session: str | None = Cookie(default=None),
):
    session = require_session(p4web_session)
    require_feature("index", session)
    q = (q or "").strip() or None
    file = (file or "").strip() or None
    user = (user or "").strip() or None
    # No filters is valid: it's the default Changes browse, served from
    # the index as the newest N submitted changes.
    results = change_index.search(
        session["user"],
        q=q,
        file=file,
        cl_user=user,
        date_from=_date_epoch(date_from) if date_from else None,
        date_to=_date_epoch(date_to, end=True) if date_to else None,
        max_results=min(max, 500),
    )
    return {"changes": results, "count": len(results)}


# ---------- metadata browsers: labels / jobs / branches / streams / users ----------


# No wildcards or slashes, and no leading '-' so a name can never be
# read as a p4 command-line flag (argument injection).
NAME_RE = re.compile(r"^(?!-)[^/@#*\x00-\x1f]{1,256}$")


def _validate_name(name):
    """Spec names (labels, jobs, branches) — no wildcards or slashes."""
    if not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid name")
    return name


@app.get("/api/labels")
def labels(max: int = 200, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    records = p4_call(
        p4.run, ["labels", "-t", "-m", str(min(max, 500))],
        session["user"], session["ticket"],
    )
    return {
        "labels": [
            {
                "name": r["label"],
                "owner": r.get("Owner", ""),
                "update": int(r.get("Update", 0)),
                "revision": r.get("Revision"),
                "desc": r.get("Description", "").strip(),
            }
            for r in records
        ]
    }


@app.get("/api/label/{name}")
def label_detail(name: str, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    user, ticket = session["user"], session["ticket"]
    _validate_name(name)
    specs = p4_call(p4.run, ["label", "-o", name], user, ticket)
    if not specs:
        raise HTTPException(status_code=404, detail="Label not found")
    spec = specs[0]
    views = []
    i = 0
    while f"View{i}" in spec:
        views.append(spec[f"View{i}"])
        i += 1
    files, errors = p4_call(
        p4.run, ["files", "-m", "500", f"//...@{name}"], user, ticket,
        collect_errors=True,
    )
    return {
        "name": spec.get("Label", name),
        "owner": spec.get("Owner", ""),
        "desc": spec.get("Description", "").strip(),
        "options": spec.get("Options", ""),
        "revision": spec.get("Revision"),
        "update": spec.get("Update", ""),
        "views": views,
        "files": [
            {"path": f["depotFile"], "rev": int(f["rev"]), "action": f["action"]}
            for f in files
        ],
        "truncated": len(files) >= 500,
    }


@app.get("/api/jobs")
def jobs(max: int = 200, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    records = p4_call(
        p4.run, ["jobs", "-l", "-m", str(min(max, 500))],
        session["user"], session["ticket"],
    )
    return {
        "jobs": [
            {
                "name": r.get("Job", ""),
                "status": r.get("Status", ""),
                "user": r.get("User", ""),
                "date": r.get("Date", ""),
                "desc": r.get("Description", "").strip(),
            }
            for r in records
        ]
    }


@app.get("/api/job/{name}")
def job_detail(name: str, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    user, ticket = session["user"], session["ticket"]
    _validate_name(name)
    specs = p4_call(p4.run, ["job", "-o", name], user, ticket)
    if not specs:
        raise HTTPException(status_code=404, detail="Job not found")
    spec = specs[0]
    fixes = p4_call(p4.run, ["fixes", "-j", name], user, ticket)
    return {
        "name": spec.get("Job", name),
        "status": spec.get("Status", ""),
        "user": spec.get("User", ""),
        "date": spec.get("Date", ""),
        "desc": spec.get("Description", "").strip(),
        "fixes": [
            {
                "change": int(f["Change"]),
                "date": int(f.get("Date", 0)),
                "user": f.get("User", ""),
                "status": f.get("Status", ""),
            }
            for f in fixes
        ],
    }


@app.get("/api/branches")
def branches(max: int = 200, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    records = p4_call(
        p4.run, ["branches", "-t", "-m", str(min(max, 500))],
        session["user"], session["ticket"],
    )
    return {
        "branches": [
            {
                "name": r["branch"],
                "owner": r.get("Owner", ""),
                "update": int(r.get("Update", 0)),
                "desc": r.get("Description", "").strip(),
            }
            for r in records
        ]
    }


@app.get("/api/branch/{name}")
def branch_detail(name: str, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    _validate_name(name)
    specs = p4_call(
        p4.run, ["branch", "-o", name], session["user"], session["ticket"]
    )
    if not specs:
        raise HTTPException(status_code=404, detail="Branch not found")
    spec = specs[0]
    views = []
    i = 0
    while f"View{i}" in spec:
        views.append(spec[f"View{i}"])
        i += 1
    return {
        "name": spec.get("Branch", name),
        "owner": spec.get("Owner", ""),
        "desc": spec.get("Description", "").strip(),
        "update": spec.get("Update", ""),
        "views": views,
    }


@app.get("/api/streams")
def streams(max: int = 200, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    records = p4_call(
        p4.run, ["streams", "-m", str(min(max, 500))],
        session["user"], session["ticket"],
    )
    return {
        "streams": [
            {
                "stream": r.get("Stream", ""),
                "name": r.get("Name", ""),
                "type": r.get("Type", ""),
                "parent": r.get("Parent", ""),
                "owner": r.get("Owner", ""),
            }
            for r in records
        ]
    }


@app.get("/api/users")
def users(p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    user, ticket = session["user"], session["ticket"]
    user_records = p4_call(p4.run, ["users"], user, ticket)
    group_records = p4_call(p4.run, ["groups"], user, ticket)
    groups = {}
    for g in group_records:
        entry = groups.setdefault(
            g.get("group", ""),
            {"name": g.get("group", ""), "desc": g.get("description", "").strip(), "users": [], "subgroups": []},
        )
        member = g.get("user", "")
        if g.get("isSubGroup") == "1":
            entry["subgroups"].append(member)
        elif member:
            entry["users"].append(member)
    return {
        "users": [
            {
                "user": r["User"],
                "fullName": r.get("FullName", ""),
                "email": r.get("Email", ""),
                "access": int(r.get("Access", 0)),
                "type": r.get("Type", "standard"),
            }
            for r in user_records
        ],
        "groups": sorted(groups.values(), key=lambda g: g["name"]),
    }


# ---------- favorites ----------


class FavoriteBody(BaseModel):
    path: str


@app.get("/api/favorites")
def list_favorites(p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    require_feature("favorites", session)
    return {"favorites": sessions.favorites(session["user"])}


@app.post("/api/favorites")
def add_favorite(body: FavoriteBody, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    require_feature("favorites", session)
    path = body.path.rstrip("/")
    if not path.startswith("//") or len(path) < 3:
        raise HTTPException(status_code=400, detail="Path must start with //")
    sessions.add_favorite(session["user"], path)
    return {"ok": True}


@app.delete("/api/favorites")
def remove_favorite(path: str, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    require_feature("favorites", session)
    sessions.remove_favorite(session["user"], path.rstrip("/"))
    return {"ok": True}


# ---------- inline comments ----------

MAX_COMMENT_BYTES = 64 * 1024


class CommentBody(BaseModel):
    body: str
    path: str | None = None
    rev: int | None = None
    line: int | None = None
    change: int | None = None
    parent: int | None = None


class CommentPatch(BaseModel):
    body: str | None = None
    resolved: bool | None = None


@app.get("/api/comments")
def list_comments(
    path: str | None = None,
    change: int | None = None,
    files: bool = False,
    p4web_session: str | None = Cookie(default=None),
):
    session = require_session(p4web_session)
    require_feature("comments", session)
    if not path and change is None:
        raise HTTPException(status_code=400, detail="path or change filter required")
    if path and not path.startswith("//"):
        raise HTTPException(status_code=400, detail="Path must start with //")
    return {"comments": sessions.comments_for(path=path, change=change, files=files)}


@app.post("/api/comments")
def add_comment(body: CommentBody, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    require_feature("comments", session)
    text = body.body.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty comment")
    if len(text.encode()) > MAX_COMMENT_BYTES:
        raise HTTPException(status_code=413, detail="Comment too long")
    mentioned = _resolve_mentions(text, session["user"], session["user"], session["ticket"])
    if body.parent is not None:
        parent = sessions.comment_get(body.parent)
        if not parent or parent.get("parent"):
            raise HTTPException(status_code=400, detail="Can only reply to a top-level comment")
        cid = sessions.comment_add(
            session["user"], text,
            path=parent["path"], rev=parent["rev"], line=parent["line"],
            change=parent["change"], parent=body.parent,
        )
        sessions.mentions_add(cid, mentioned)
        return {"id": cid, "mentioned": mentioned}
    if body.path:
        if not body.path.startswith("//"):
            raise HTTPException(status_code=400, detail="Path must start with //")
        if body.line is not None and body.line < 1:
            raise HTTPException(status_code=400, detail="Invalid line")
        # A comment made inside a changelist's diff carries both anchors,
        # so it reads on the file page and on the changelist page.
        cid = sessions.comment_add(
            session["user"], text, path=body.path, rev=body.rev, line=body.line,
            change=body.change,
        )
        sessions.mentions_add(cid, mentioned)
        return {"id": cid, "mentioned": mentioned}
    if body.change is not None:
        cid = sessions.comment_add(session["user"], text, change=body.change)
        sessions.mentions_add(cid, mentioned)
        return {"id": cid, "mentioned": mentioned}
    raise HTTPException(status_code=400, detail="A comment needs a path, change, or parent anchor")


@app.patch("/api/comments/{cid}")
def edit_comment(
    cid: int, body: CommentPatch,
    p4web_session: str | None = Cookie(default=None),
):
    session = require_session(p4web_session)
    require_feature("comments", session)
    comment = sessions.comment_get(cid)
    if not comment or comment["deleted"]:
        raise HTTPException(status_code=404, detail="Comment not found")
    if body.body is not None:
        # Edit: author only.
        if comment["user"] != session["user"]:
            raise HTTPException(status_code=403, detail="You can only edit your own comments")
        text = body.body.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Empty comment")
        if len(text.encode()) > MAX_COMMENT_BYTES:
            raise HTTPException(status_code=413, detail="Comment too long")
        sessions.comment_update(cid, body=text)
        # The edited body is the whole truth about who is mentioned:
        # names added get a mention, names dropped lose theirs.
        sessions.mentions_replace(
            cid, _resolve_mentions(text, session["user"], session["user"], session["ticket"])
        )
    if body.resolved is not None:
        # Resolve/reopen: anyone, thread roots only.
        if comment["parent"]:
            raise HTTPException(status_code=400, detail="Resolve the thread's top comment")
        sessions.comment_update(cid, resolved=body.resolved)
    return {"ok": True}


@app.delete("/api/comments/{cid}")
def delete_comment(cid: int, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    require_feature("comments", session)
    comment = sessions.comment_get(cid)
    if not comment or comment["deleted"]:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment["user"] != session["user"]:
        raise HTTPException(status_code=403, detail="You can only delete your own comments")
    sessions.comment_delete(cid)
    sessions.mentions_drop(cid)
    return {"ok": True}


@app.get("/api/comments/counts")
def comment_counts(
    changes: str | None = None,
    dir: str | None = None,
    p4web_session: str | None = Cookie(default=None),
):
    """Open/total thread counts, for the badges on listing rows.

    `changes` is a comma-separated changelist list; `dir` is a depot
    directory whose immediate files are counted. Both read only the
    app's own comment store — a comment is visible to anyone who can
    reach the app, exactly as the panels already are."""
    session = require_session(p4web_session)
    require_feature("comments", session)
    if changes:
        try:
            nums = [int(c) for c in changes.split(",") if c.strip()][:500]
        except ValueError:
            raise HTTPException(status_code=400, detail="changes must be numbers")
        return {"counts": sessions.comment_counts_for_changes(nums)}
    if dir:
        if not dir.startswith("//"):
            raise HTTPException(status_code=400, detail="Path must start with //")
        return {"counts": sessions.comment_counts_for_dir(dir)}
    raise HTTPException(status_code=400, detail="changes or dir required")


# ---------- @mentions ----------
#
# Naming someone in a comment gives them a row in `mentions`, which is
# what the topbar badge counts. Names are checked against the Perforce
# user list so a typo (or an email-looking string) doesn't create a
# mention nobody will ever read; the list is cached briefly because a
# busy thread would otherwise run `p4 users` per comment.

_USER_CACHE = {"names": None, "at": 0.0}
_USER_CACHE_TTL = 300


def _known_users(user, ticket):
    now = time.time()
    if _USER_CACHE["names"] is None or now - _USER_CACHE["at"] > _USER_CACHE_TTL:
        records = p4_call(p4.run, ["users"], user, ticket)
        _USER_CACHE["names"] = {r["User"] for r in records if r.get("User")}
        _USER_CACHE["at"] = now
    return _USER_CACHE["names"]


def _resolve_mentions(body, author, user, ticket):
    """Real users named in `body`, minus the author (nobody needs a badge
    for their own comment)."""
    if not features.SERVER.get("mentions", True):
        return []  # no inbox to land in on this instance — don't record rows
    names = sessions.mention_names(body)
    if not names:
        return []
    try:
        known = _known_users(user, ticket)
    except HTTPException:
        return []
    return [n for n in names if n in known and n != author]


class MentionsSeen(BaseModel):
    ids: list[int] | None = None


@app.get("/api/mentions")
def list_mentions(
    unseen: bool = False,
    max: int = 100,
    p4web_session: str | None = Cookie(default=None),
):
    session = require_session(p4web_session)
    require_feature("mentions", session)
    return {
        "mentions": sessions.mentions_for(
            session["user"], unseen_only=unseen, limit=min(max, 500)
        ),
        "unseen": sessions.mentions_unseen_count(session["user"]),
    }


@app.post("/api/mentions/seen")
def mark_mentions_seen(
    body: MentionsSeen, p4web_session: str | None = Cookie(default=None),
):
    session = require_session(p4web_session)
    require_feature("mentions", session)
    sessions.mentions_mark_seen(session["user"], body.ids)
    return {"unseen": sessions.mentions_unseen_count(session["user"])}


# ---------- light reviews ----------
#
# A review is a state flag plus a history on top of a changelist —
# usually a pending one with shelved files, which is how you circulate
# work here before it is submitted. Perforce stores none of it; the
# state lives beside the comments in the app DB.
#
# Anyone who can see the changelist can move it to any state, including
# its author. The event log makes that legible: every transition records
# who made it, so "approved" always says approved-by-whom.

MAX_REVIEW_NOTE_BYTES = 4 * 1024


class ReviewBody(BaseModel):
    state: str
    note: str | None = None


def _change_summary(change: int, user: str, ticket: str):
    """Changelist header for a review row, via the requesting user's own
    ticket — a change they cannot see 404s here just like anywhere else."""
    records = p4_call(p4.run, ["describe", "-s", str(change)], user, ticket)
    if not records:
        raise HTTPException(status_code=404, detail="Change not found")
    r = records[0]
    return {
        "change": int(r["change"]),
        "user": r.get("user", ""),
        "status": r.get("status", ""),
        "time": int(r.get("time", 0)),
        "desc": r.get("desc", "").rstrip(),
    }


@app.get("/api/reviews")
def list_reviews(
    state: str | None = None,
    max: int = 100,
    p4web_session: str | None = Cookie(default=None),
):
    """Reviews, newest activity first, each with its changelist header.

    The headers come from one `p4 describe -s` over the whole batch, so
    the list costs a single p4 call — and changes the user cannot see
    drop out of it, since describe simply returns nothing for them."""
    session = require_session(p4web_session)
    require_feature("reviews", session)
    if state is not None and state not in sessions.REVIEW_STATES:
        raise HTTPException(status_code=400, detail="Unknown review state")
    max = min(max, 500)
    reviews = sessions.reviews_list(state=state, limit=max)
    if not reviews:
        return {"reviews": []}
    args = ["describe", "-s"] + [str(r["change"]) for r in reviews]
    records = p4_call(p4.run, args, session["user"], session["ticket"])
    heads = {
        int(r["change"]): {
            "user": r.get("user", ""),
            "status": r.get("status", ""),
            "time": int(r.get("time", 0)),
            "desc": r.get("desc", "").rstrip(),
        }
        for r in records if r.get("change")
    }
    out = [dict(r, **heads[r["change"]]) for r in reviews if r["change"] in heads]
    return {"reviews": out}


@app.get("/api/review/{change}")
def get_review(change: int, p4web_session: str | None = Cookie(default=None)):
    session = require_session(p4web_session)
    require_feature("reviews", session)
    _change_summary(change, session["user"], session["ticket"])
    return {
        "review": sessions.review_get(change),
        "events": sessions.review_events(change),
    }


@app.post("/api/review/{change}")
def set_review(
    change: int, body: ReviewBody,
    p4web_session: str | None = Cookie(default=None),
):
    session = require_session(p4web_session)
    require_feature("reviews", session)
    if body.state not in sessions.REVIEW_STATES:
        raise HTTPException(status_code=400, detail="Unknown review state")
    note = (body.note or "").strip() or None
    if note and len(note.encode()) > MAX_REVIEW_NOTE_BYTES:
        raise HTTPException(status_code=413, detail="Note too long")
    # Proves the changelist exists and that this user can see it.
    _change_summary(change, session["user"], session["ticket"])
    review = sessions.review_set(change, session["user"], body.state, note)
    return {"review": review, "events": sessions.review_events(change)}


@app.delete("/api/review/{change}")
def drop_review(change: int, p4web_session: str | None = Cookie(default=None)):
    """Withdraw a review entirely (the flag and its history). Available
    to whoever opened it."""
    session = require_session(p4web_session)
    require_feature("reviews", session)
    review = sessions.review_get(change)
    if not review:
        raise HTTPException(status_code=404, detail="No review on this change")
    if review["openedBy"] != session["user"]:
        raise HTTPException(
            status_code=403,
            detail="Only the person who opened the review can withdraw it",
        )
    sessions.review_delete(change)
    return {"ok": True}


# ---------- write operations (per-user server-side workspace) ----------
#
# Policy enforced here, on top of p4 protections:
#   * every mutation runs in the user's own managed client
#     (p4web-<user>), never the operator's;
#   * only pending changelists owned by that user AND that client can
#     be touched;
#   * every mutation is appended to data/audit.log.


def _client_ip(request):
    return request.client.host if request.client else "unknown"


def _write_ctx(p4web_session, request):
    session = require_session(p4web_session)
    # Every write route comes through here, so this is the one place the
    # "write" flag has to be checked — and it sits before ensure_client,
    # so a read-only instance never even provisions a workspace.
    require_feature("write", session)
    user, ticket = session["user"], session["ticket"]
    client = p4_call(workspace.ensure_client, user, ticket)
    return user, ticket, client, _client_ip(request)


def _require_own_change(user, ticket, client, change):
    recs = p4_call(p4.run, ["change", "-o", str(change)], user, ticket)
    if not recs:
        raise HTTPException(status_code=404, detail="Change not found")
    spec = recs[0]
    if (
        spec.get("Status") != "pending"
        or spec.get("User") != user
        or spec.get("Client") != client
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Only your own p4-web pending changelists can be modified here.",
                "raw": f"change {change}: status={spec.get('Status')} user={spec.get('User')} client={spec.get('Client')}",
            },
        )
    return spec


def _indent_desc(desc):
    desc = (desc or "").strip() or "(no description)"
    return "".join(f"\t{line}\n" for line in desc.splitlines())


class PendingBody(BaseModel):
    description: str


class OpenBody(BaseModel):
    path: str
    action: str  # edit | delete


class SaveBody(BaseModel):
    path: str
    content: str


class RevertBody(BaseModel):
    path: str | None = None


def _depot_file_arg(path):
    if not path.startswith("//"):
        raise HTTPException(status_code=400, detail="Path must start with //")
    return p4.escape_path(path)


@app.get("/api/my/pending")
def my_pending(request: Request, p4web_session: str | None = Cookie(default=None)):
    user, ticket, client, _ = _write_ctx(p4web_session, request)
    changes = p4_call(
        p4.run, ["changes", "-l", "-s", "pending", "-c", client], user, ticket
    )
    opened = p4_call(p4.run, ["opened"], user, ticket, client=client)
    shelved = {
        int(r["change"])
        for r in p4_call(p4.run, ["changes", "-s", "shelved", "-c", client], user, ticket)
    }
    files_by_change = {}
    for r in opened:
        files_by_change.setdefault(int(r.get("change", 0)), []).append({
            "path": r["depotFile"],
            "action": r.get("action", ""),
            "type": r.get("type", ""),
            "rev": int(r["rev"]) if r.get("rev", "").isdigit() else None,
        })
    return {
        "client": client,
        "changes": [
            {
                "change": int(c["change"]),
                "desc": c.get("desc", "").rstrip(),
                "time": int(c.get("time", 0)),
                "shelved": int(c["change"]) in shelved,
                "files": files_by_change.get(int(c["change"]), []),
            }
            for c in changes
        ],
    }


@app.post("/api/my/pending")
def create_pending(
    body: PendingBody, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    spec = (
        f"Change:\tnew\nClient:\t{client}\nUser:\t{user}\nStatus:\tnew\n"
        "Description:\n" + _indent_desc(body.description)
    )
    out = p4_call(
        p4.run_text, ["change", "-i"], user, ticket, stdin=spec, client=client
    ).decode("utf-8", errors="replace")
    m = re.search(r"Change (\d+) created", out)
    if not m:
        raise HTTPException(status_code=500, detail=f"Unexpected p4 output: {out.strip()}")
    change = int(m.group(1))
    workspace.audit(user, ip, "create_pending", change=change, desc=body.description[:200])
    return {"change": change}


@app.patch("/api/my/pending/{change}")
def update_pending(
    change: int, body: PendingBody, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)
    spec_text = p4_call(
        p4.run_text, ["change", "-o", str(change)], user, ticket, client=client
    ).decode("utf-8", errors="replace")
    new_block = "Description:\n" + _indent_desc(body.description)
    spec_text = re.sub(r"Description:\n(?:[ \t].*\n?)*", new_block, spec_text, count=1)
    p4_call(p4.run_text, ["change", "-i"], user, ticket, stdin=spec_text, client=client)
    workspace.audit(user, ip, "update_pending", change=change, desc=body.description[:200])
    return {"ok": True}


@app.delete("/api/my/pending/{change}")
def delete_pending(
    change: int, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)
    p4_call(p4.run, ["change", "-d", str(change)], user, ticket, client=client)
    workspace.audit(user, ip, "delete_pending", change=change)
    return {"ok": True}


@app.post("/api/my/pending/{change}/open")
def open_file(
    change: int, body: OpenBody, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    """Open a depot file in the changelist: edit (sync + p4 edit,
    returns editable content) or delete (p4 delete -v)."""
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)
    if body.action not in ("edit", "delete"):
        raise HTTPException(status_code=400, detail="action must be edit or delete")
    spec = _depot_file_arg(body.path)
    stats = p4_call(p4.run, ["fstat", "-Ol", spec], user, ticket)
    if not stats:
        raise HTTPException(status_code=404, detail="File not found in depot")
    st = stats[0]
    if st.get("headAction") in ("delete", "move/delete"):
        raise HTTPException(status_code=400, detail="File is deleted at head")

    with workspace.user_lock(user):
        if body.action == "delete":
            p4_call(p4.run, ["delete", "-c", str(change), "-v", spec], user, ticket, client=client)
            workspace.audit(user, ip, "open_delete", change=change, path=body.path)
            return {"path": body.path, "action": "delete"}

        p4_call(p4.run, ["sync", spec], user, ticket, client=client)
        p4_call(p4.run, ["edit", "-c", str(change), spec], user, ticket, client=client)
        workspace.audit(user, ip, "open_edit", change=change, path=body.path)
        ftype = st.get("headType", "text")
        binary = any(m in ftype for m in BINARY_TYPE_MARKERS)
        size = int(st.get("fileSize", 0))
        content = None
        if not binary and size <= MAX_TEXT_BYTES:
            local = p4_call(workspace.local_path, user, ticket, body.path)
            content = local.read_bytes().decode("utf-8", errors="replace")
        return {
            "path": body.path, "action": "edit", "type": ftype,
            "binary": binary, "size": size,
            "tooLarge": size > MAX_TEXT_BYTES, "content": content,
        }


def _opened_entry(user, ticket, client, change, path):
    recs = p4_call(p4.run, ["opened", _depot_file_arg(path)], user, ticket, client=client)
    for r in recs:
        if int(r.get("change", 0)) == change:
            return r
    return None


@app.get("/api/my/pending/{change}/file")
def opened_file_content(
    change: int, path: str, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    user, ticket, client, _ = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)
    entry = _opened_entry(user, ticket, client, change, path)
    if not entry:
        raise HTTPException(status_code=404, detail="File is not open in this changelist")
    local = p4_call(workspace.local_path, user, ticket, path)
    if not local.exists():
        raise HTTPException(status_code=404, detail="No local content (file opened without content?)")
    data = local.read_bytes()
    if len(data) > MAX_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="File too large for the web editor")
    return {
        "path": path,
        "action": entry.get("action", ""),
        "content": data.decode("utf-8", errors="replace"),
    }


@app.put("/api/my/pending/{change}/file")
def save_file(
    change: int, body: SaveBody, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    """Save editor content. Writes an already-open file, or performs
    `p4 add` when the path doesn't exist in the depot yet."""
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)
    data = body.content.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise HTTPException(status_code=413, detail="Content too large for the web editor")

    with workspace.user_lock(user):
        entry = _opened_entry(user, ticket, client, change, body.path)
        local = p4_call(workspace.local_path, user, ticket, body.path)
        if entry:
            if entry.get("action") not in ("edit", "add", "move/add"):
                raise HTTPException(status_code=409, detail=f"File is open for {entry.get('action')}, not editable")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            workspace.audit(user, ip, "save", change=change, path=body.path, bytes=len(data))
            return {"ok": True, "action": entry.get("action")}
        stats = p4_call(p4.run, ["fstat", _depot_file_arg(body.path)], user, ticket)
        if stats and stats[0].get("headAction") not in ("delete", "move/delete"):
            raise HTTPException(status_code=409, detail="File exists in the depot — open it for edit first")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        p4_call(p4.run, ["add", "-c", str(change), "-f", str(local)], user, ticket, client=client)
        # p4 refusals below error severity (e.g. ignore rules) don't
        # raise — confirm the file actually opened.
        if not _opened_entry(user, ticket, client, change, body.path):
            raise HTTPException(status_code=500, detail="p4 add did not open the file — check server ignore/protect rules")
        workspace.audit(user, ip, "add", change=change, path=body.path, bytes=len(data))
        return {"ok": True, "action": "add"}


MAX_UPLOAD_BYTES = 100 * 1024 * 1024


@app.post("/api/my/pending/{change}/upload")
async def upload_file(
    change: int, request: Request,
    path: str = Form(...), file: UploadFile = File(...),
    p4web_session: str | None = Cookie(default=None),
):
    """Upload bytes as a new file (add) or as replacement content for
    an existing depot file (sync + edit + overwrite). Binary-safe."""
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)

    # Spool the upload to a bounded temp file before touching p4, so a
    # 100 MB (or unbounded chunked) upload is never held whole in memory
    # and no p4 side effects happen if it turns out to be oversized.
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > MAX_UPLOAD_BYTES + 1024 * 1024:
        raise HTTPException(status_code=413, detail="Upload too large (100 MB max)")
    sessions.DATA_DIR.mkdir(parents=True, exist_ok=True)
    spool = tempfile.NamedTemporaryFile(delete=False, dir=str(sessions.DATA_DIR))
    size = 0
    try:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Upload too large (100 MB max)")
            spool.write(chunk)
        spool.close()

        with workspace.user_lock(user):
            entry = _opened_entry(user, ticket, client, change, path)
            local = p4_call(workspace.local_path, user, ticket, path)
            if entry:
                if entry.get("action") not in ("edit", "add", "move/add"):
                    raise HTTPException(status_code=409, detail=f"File is open for {entry.get('action')}")
                action = entry.get("action")
            else:
                stats = p4_call(p4.run, ["fstat", _depot_file_arg(path)], user, ticket)
                exists = bool(stats) and stats[0].get("headAction") not in ("delete", "move/delete")
                if exists:
                    p4_call(p4.run, ["sync", _depot_file_arg(path)], user, ticket, client=client)
                    p4_call(p4.run, ["edit", "-c", str(change), _depot_file_arg(path)], user, ticket, client=client)
                    action = "edit"
                else:
                    action = "add"
            local.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(spool.name, local)
            if action == "add" and not entry:
                p4_call(p4.run, ["add", "-c", str(change), "-f", str(local)], user, ticket, client=client)
            if not _opened_entry(user, ticket, client, change, path):
                raise HTTPException(status_code=500, detail="p4 did not open the file — check server ignore/protect rules")
            workspace.audit(user, ip, "upload", change=change, path=path, bytes=size, action=action)
            return {"ok": True, "action": action, "bytes": size}
    finally:
        try:
            spool.close()
        except Exception:
            pass
        if os.path.exists(spool.name):
            os.unlink(spool.name)


@app.post("/api/my/pending/{change}/revert")
def revert_files(
    change: int, body: RevertBody, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)
    target = _depot_file_arg(body.path) if body.path else "//..."
    with workspace.user_lock(user):
        records = p4_call(
            p4.run, ["revert", "-c", str(change), target], user, ticket, client=client
        )
        reverted = [r.get("depotFile") for r in records if r.get("depotFile")]
        # Free disk: drop local copies of what we reverted.
        for f in reverted:
            p4_call(
                p4.run, ["sync", p4.escape_path(f) + "#none"], user, ticket,
                client=client, collect_errors=True,
            )
    workspace.audit(user, ip, "revert", change=change, paths=reverted)
    return {"reverted": reverted}


@app.post("/api/my/pending/{change}/submit")
def submit_pending(
    change: int, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)
    with workspace.user_lock(user):
        records = p4_call(p4.run, ["submit", "-c", str(change)], user, ticket, client=client)
        submitted = None
        files = []
        for r in records:
            if r.get("submittedChange"):
                submitted = int(r["submittedChange"])
            if r.get("depotFile"):
                files.append(r["depotFile"])
        # Free disk: submitted content doesn't need to stay synced.
        for f in files:
            p4_call(
                p4.run, ["sync", p4.escape_path(f) + "#none"], user, ticket,
                client=client, collect_errors=True,
            )
    if submitted is None:
        raise HTTPException(status_code=500, detail="Submit did not return a change number")
    workspace.audit(user, ip, "submit", change=change, submitted=submitted, files=files)
    return {"submittedChange": submitted, "files": files}


@app.post("/api/my/pending/{change}/shelve")
def shelve_pending(
    change: int, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    """Shelve (or refresh the shelf of) every file open in the CL."""
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)
    with workspace.user_lock(user):
        p4_call(p4.run, ["shelve", "-f", "-c", str(change)], user, ticket, client=client)
    workspace.audit(user, ip, "shelve", change=change)
    return {"ok": True}


@app.delete("/api/my/pending/{change}/shelve")
def delete_shelf(
    change: int, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)
    with workspace.user_lock(user):
        p4_call(p4.run, ["shelve", "-d", "-c", str(change)], user, ticket, client=client)
    workspace.audit(user, ip, "delete_shelf", change=change)
    return {"ok": True}


@app.post("/api/my/pending/{change}/unshelve")
def unshelve_pending(
    change: int, request: Request,
    p4web_session: str | None = Cookie(default=None),
):
    """Restore the CL's shelved files into the workspace. Shelf wins:
    files currently open in the CL are reverted first (p4 refuses to
    unshelve onto an open file even with -f — it skips at warning
    severity, observed in testing)."""
    user, ticket, client, ip = _write_ctx(p4web_session, request)
    _require_own_change(user, ticket, client, change)
    with workspace.user_lock(user):
        p4_call(
            p4.run, ["revert", "-c", str(change), "//..."],
            user, ticket, client=client, collect_errors=True,
        )
        records = p4_call(
            p4.run, ["unshelve", "-f", "-s", str(change), "-c", str(change)],
            user, ticket, client=client,
        )
    files = [r.get("depotFile") for r in records if r.get("depotFile")]
    if not files:
        raise HTTPException(status_code=400, detail="Nothing was unshelved — is the shelf empty?")
    workspace.audit(user, ip, "unshelve", change=change, files=files)
    return {"files": files}


def _asset_version(name):
    """A short mtime-based tag so a redeployed asset busts the browser
    cache. StaticFiles serves etag/last-modified but no Cache-Control,
    so browsers heuristic-cache app.js and can keep running a stale copy
    across a deploy; a changing ?v= query forces the refetch."""
    try:
        return str(int((STATIC_DIR / name).stat().st_mtime))
    except OSError:
        return "0"


def _index_html():
    html = (STATIC_DIR / "index.html").read_text()
    # Version the app's own JS/CSS (vendored libs are pinned, so leave
    # them cacheable). Rewrites the exact hrefs in index.html.
    for asset in ("app.js", "style.css"):
        html = html.replace(
            f'"/static/{asset}"', f'"/static/{asset}?v={_asset_version(asset)}"'
        )
    return html


@app.get("/")
def index():
    # no-store on the tiny HTML shell so the versioned asset URLs it
    # carries are always the current ones; the assets themselves stay
    # cacheable behind their ?v= tag.
    return HTMLResponse(
        _index_html(), headers={"Cache-Control": "no-store, must-revalidate"}
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
