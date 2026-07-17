"""Per-user server-side Perforce workspaces for write operations.

Each logged-in user gets a managed client named p4web-<user> rooted
under data/workspaces/<user>. Files are synced sparsely (only what is
being edited) and cleaned up (#none) after submit/revert, so disk use
stays proportional to open work, not depot size.

All mutating entry points funnel through the API layer in main.py,
which enforces the safety policy: a user may only touch pending
changelists that belong to their own p4web client.
"""

import json
import threading
import time
from pathlib import Path

from . import p4, sessions

WORKSPACES_DIR = sessions.DATA_DIR / "workspaces"
AUDIT_LOG = sessions.DATA_DIR / "audit.log"

_ensured = set()          # users whose client spec is known current
_ensure_lock = threading.Lock()
_user_locks: dict[str, threading.Lock] = {}


def user_lock(user):
    """Serialize mutating operations per workspace."""
    with _ensure_lock:
        if user not in _user_locks:
            _user_locks[user] = threading.Lock()
        return _user_locks[user]


def client_name(user):
    return f"p4web-{user}"


class WorkspaceError(Exception):
    pass


def ensure_client(user, ticket):
    """Create or refresh the user's managed client. Idempotent; the
    spec is re-pushed once per process lifetime per user (covers new
    depots appearing)."""
    name = client_name(user)
    if user in _ensured:
        return name
    root = WORKSPACES_DIR / user
    root.mkdir(parents=True, exist_ok=True)
    depots = p4.run(["depots"], user, ticket)
    views = [
        f"\t//{d['name']}/... //{name}/{d['name']}/..."
        for d in depots
        if d.get("type") in ("local", "stream")
    ]
    if not views:
        raise WorkspaceError("No depots visible to build a workspace view")
    spec = (
        f"Client:\t{name}\n"
        f"Owner:\t{user}\n"
        f"Root:\t{root}\n"
        "Options:\tnoallwrite noclobber nocompress unlocked nomodtime rmdir\n"
        "SubmitOptions:\tleaveunchanged\n"
        "LineEnd:\tlocal\n"
        "Description:\n\tp4-web managed workspace. Do not edit by hand.\n"
        "View:\n" + "\n".join(views) + "\n"
    )
    p4.run_text(["client", "-i"], user, ticket, stdin=spec)
    with _ensure_lock:
        _ensured.add(user)
    return name


def local_path(user, ticket, depot_path):
    """Map a depot path to its absolute location inside the user's
    workspace root, refusing anything that escapes it."""
    name = client_name(user)
    records = p4.run(
        ["where", p4.escape_path(depot_path)], user, ticket, client=name
    )
    mapped = [r for r in records if r.get("path") and "unmap" not in r]
    if not mapped:
        raise WorkspaceError(f"Path not mapped in workspace: {depot_path}")
    path = Path(mapped[-1]["path"]).resolve()
    root = (WORKSPACES_DIR / user).resolve()
    if not path.is_relative_to(root):
        raise WorkspaceError(f"Mapped path escapes workspace root: {path}")
    return path


def audit(user, ip, op, **detail):
    """Append one JSON line per write operation to data/audit.log."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "user": user,
        "ip": ip,
        "op": op,
        **detail,
    }
    sessions.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
