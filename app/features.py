"""Feature flags: which parts of p4-web this instance — and this user — run.

Two layers, and what actually happens is the AND of both:

    server policy      instance-wide, operator-controlled: code defaults
                       -> features.json -> P4WEB_FEATURE_* environment
    user preference    a row per user in sessions.db, able only to turn
                       a feature *further* off

The operator sets the ceiling; each user decides how much of what's left
they want on screen. Everything defaults to on, so an instance that
configures nothing behaves exactly as it did before this module existed.

Turning a feature off hides its UI *and* closes its endpoints — 404 when
the server disabled it (as far as a client can tell, this build doesn't
have it), 403 when the user did (a stale tab should learn why). See
require_feature() in main.py. It never deletes anything: reviews and
comments written while a feature was on come straight back when it is
switched on again.

Deployments differ — some want a read-only browser, some already have
Swarm doing reviews, some users just don't want the mention badge — and
none of that was expressible before.
"""

import json
import os
from pathlib import Path

# Flag -> default. Order is the order they appear on the Settings page.
# Everything on: an unconfigured instance keeps every feature it had.
DEFAULTS = {
    "comments": True,    # inline comment threads on files and changelists
    "mentions": True,    # @name in a comment, the topbar badge, Mentions page
    "reviews": True,     # light review state (open/approved/needs-work)
    "write": True,       # checkout/edit/upload/revert/shelve/submit
    "favorites": True,   # starred paths and the depot-root dashboard
    "index": True,       # the changelist search index behind fast Changes
}

# A flag that only makes sense while another is on. Mentions are made
# *in* comments: with comments off there is nowhere to write one, so it
# reads as off regardless of what either layer says. The stored setting
# is left alone so it returns when comments come back.
DEPENDS = {"mentions": "comments"}

# Things deliberately NOT toggleable, because they are the app: depot
# browsing, the file viewer, history/annotate/diff, changelist reading,
# labels/jobs/branches/streams/users, search, markdown rendering.

DATA_DIR = Path(
    os.environ.get("P4WEB_DATA")
    or Path(__file__).resolve().parent.parent / "data"
)

CONFIG_PATH = Path(
    os.environ.get("P4WEB_FEATURES_FILE") or DATA_DIR / "features.json"
)

ENV_PREFIX = "P4WEB_FEATURE_"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _as_bool(value, where, notes):
    """Parse a config value, or complain in `notes` and give back None."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    notes.append(f"{where}: {value!r} is not a boolean — ignored")
    return None


def _from_file(policy, notes):
    """Overlay CONFIG_PATH, if it exists.

    A broken config must not stop the server from booting: an operator
    who fat-fingers the JSON should get a UI that still works with the
    defaults and a line in the log, not an app that won't start.
    """
    try:
        raw = CONFIG_PATH.read_text()
    except FileNotFoundError:
        return
    except OSError as e:
        notes.append(f"{CONFIG_PATH}: unreadable ({e}) — using defaults")
        return
    try:
        data = json.loads(raw)
    except ValueError as e:
        notes.append(f"{CONFIG_PATH}: invalid JSON ({e}) — using defaults")
        return
    # Accept both {"reviews": false} and {"features": {"reviews": false}}.
    if isinstance(data, dict) and isinstance(data.get("features"), dict):
        data = data["features"]
    if not isinstance(data, dict):
        notes.append(f"{CONFIG_PATH}: expected an object of flags — using defaults")
        return
    for key, value in data.items():
        if key not in DEFAULTS:
            notes.append(f"{CONFIG_PATH}: unknown feature {key!r} — ignored")
            continue
        parsed = _as_bool(value, f"{CONFIG_PATH}[{key}]", notes)
        if parsed is not None:
            policy[key] = parsed


def _from_env(policy, notes):
    """Overlay P4WEB_FEATURE_<KEY> — the last word, for containers and
    launchd plists where dropping a config file is awkward."""
    for key in DEFAULTS:
        value = os.environ.get(ENV_PREFIX + key.upper())
        if value is None or value == "":
            continue
        parsed = _as_bool(value, ENV_PREFIX + key.upper(), notes)
        if parsed is not None:
            policy[key] = parsed


def _resolve():
    policy = dict(DEFAULTS)
    notes = []
    _from_file(policy, notes)
    _from_env(policy, notes)
    return policy, notes


# Resolved once at import. Changing the policy needs a restart, which
# this deployment does on every submit anyway.
SERVER, NOTES = _resolve()


def server_policy():
    return dict(SERVER)


def locked():
    """Flags the server turned off, which a user therefore cannot turn
    back on. The Settings page shows these disabled."""
    return sorted(k for k, on in SERVER.items() if not on)


def apply_depends(flags):
    """Fold DEPENDS into an already-computed map, in place."""
    for key, needs in DEPENDS.items():
        if not flags.get(needs, True):
            flags[key] = False
    return flags


def effective(prefs=None):
    """Server policy AND the user's own switches. A missing preference
    means on, so an existing user who never opens Settings is unaffected."""
    prefs = prefs or {}
    flags = {k: bool(SERVER[k]) and prefs.get(k, True) is not False for k in DEFAULTS}
    return apply_depends(flags)


def describe():
    """Startup log line: only what an operator changed, so a default
    instance stays quiet."""
    changed = [f"{k}={'on' if v else 'off'}" for k, v in SERVER.items() if v != DEFAULTS[k]]
    return ", ".join(changed)
