"""Staged screenshots for docs/guide.html.

Drives the real SPA with playwright while intercepting every /api/ call
and answering with a fictional depot (//rocket, users alice/bob/carol/
dana, server ssl:p4.example.com:1666), so no real depot data can reach a
published image. An unmocked endpoint fails loudly rather than falling
through to the live server.

Add a fixture to handle() and a shot() call at the bottom when a new
guide step needs an image; keep NOW fixed so re-runs are reproducible.

Usage:
    .venv/bin/uvicorn app.main:app --port 8899   # any data dir; the API is mocked
    .venv/bin/python tools/mock_shots.py         # writes docs/assets/guide-*.png
"""

import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# pip's playwright wants a chromium build that isn't cached here, so the
# binary is named explicitly; override with P4WEB_CHROME.
EXE = os.environ.get("P4WEB_CHROME") or os.path.expanduser(
    "~/Library/Caches/ms-playwright/chromium_headless_shell-1223/"
    "chrome-headless-shell-mac-arm64/chrome-headless-shell")
BASE = "http://127.0.0.1:8899"
OUT = Path(__file__).resolve().parent.parent / "docs" / "assets"

DAY = 86400
NOW = 1781524800  # fixed (2026-06-15) so re-runs produce identical images

USERS = [
    {"user": "alice", "fullName": "Alice Nakamura", "email": "alice@rocket.example",
     "access": NOW - 2 * 3600, "type": "standard"},
    {"user": "bob", "fullName": "Bob Ferreira", "email": "bob@rocket.example",
     "access": NOW - DAY, "type": "standard"},
    {"user": "carol", "fullName": "Carol Whitfield", "email": "carol@rocket.example",
     "access": NOW - 3 * DAY, "type": "standard"},
    {"user": "dana", "fullName": "Dana Osei", "email": "dana@rocket.example",
     "access": NOW - 9 * DAY, "type": "operator"},
]

GROUPS = [
    {"name": "engine-devs", "users": ["alice", "bob"], "subgroups": [],
     "desc": "Flight engine maintainers"},
    {"name": "release", "users": ["dana"], "subgroups": ["engine-devs"],
     "desc": "Can submit to //rocket/release/..."},
]

JOBS = [
    {"name": "job000412", "status": "open", "user": "carol", "date": "2026/06/12",
     "desc": "Telemetry drops frames when the uplink retries"},
    {"name": "job000408", "status": "closed", "user": "alice", "date": "2026/06/09",
     "desc": "Stage separation timer off by one tick"},
    {"name": "job000401", "status": "suspended", "user": "bob", "date": "2026/06/02",
     "desc": "Ground console shows stale pressure readings"},
    {"name": "job000397", "status": "closed", "user": "dana", "date": "2026/05/28",
     "desc": "Nightly build fails to package the telemetry schema"},
]

SEARCH_CONTENT = {
    "kind": "content",
    "warnings": [],
    "results": [
        {"path": "//rocket/engine/telemetry/uplink.py", "line": 88,
         "text": "    retry_backoff = min(2 ** attempt, MAX_BACKOFF)"},
        {"path": "//rocket/engine/telemetry/uplink.py", "line": 141,
         "text": "        log.warning(\"uplink stalled, retry_backoff=%s\", retry_backoff)"},
        {"path": "//rocket/engine/telemetry/session.py", "line": 34,
         "text": "from .uplink import retry_backoff, MAX_BACKOFF"},
        {"path": "//rocket/ground/console/health.py", "line": 210,
         "text": "    # mirrors retry_backoff so the console and the vehicle agree"},
        {"path": "//rocket/tests/test_uplink.py", "line": 57,
         "text": "def test_retry_backoff_caps_at_max():"},
        {"path": "//rocket/tests/test_uplink.py", "line": 63,
         "text": "    assert retry_backoff(9) == MAX_BACKOFF"},
    ],
}

CHANGE = {
    "change": 4821,
    "status": "submitted",
    "shelved": False,
    "user": "alice",
    "client": "alice-mbp",
    "time": NOW - 4 * 3600,
    "jobs": ["job000412"],
    "desc": (
        "Back off the telemetry uplink instead of hammering the link.\n\n"
        "Why: a single dropped packet made the vehicle retry every 20ms,\n"
        "which saturated the downlink and starved the health channel —\n"
        "the console then showed stale pressure readings (job000412).\n\n"
        "What: uplink.retry_backoff() now doubles the delay per attempt up\n"
        "to MAX_BACKOFF, and session.py reuses it instead of its own timer.\n\n"
        "Tested: unit tests for the cap, plus a 40-minute soak on the bench\n"
        "rig with 5% induced packet loss — health channel stayed current."
    ),
    "files": [
        {"path": "//rocket/engine/telemetry/uplink.py", "action": "edit",
         "type": "text", "rev": 14},
        {"path": "//rocket/engine/telemetry/session.py", "action": "edit",
         "type": "text", "rev": 7},
        {"path": "//rocket/tests/test_uplink.py", "action": "edit",
         "type": "text", "rev": 3},
        {"path": "//rocket/docs/telemetry-notes.md", "action": "add",
         "type": "text", "rev": 1},
    ],
}

DIFF = """--- //rocket/engine/telemetry/uplink.py\t#13
+++ //rocket/engine/telemetry/uplink.py\t#14
@@ -84,9 +84,12 @@
 def send(packet, attempt=0):
     \"\"\"Push one telemetry packet at the ground station.\"\"\"
     if attempt > MAX_ATTEMPTS:
         raise UplinkGaveUp(packet.seq)
-    time.sleep(RETRY_DELAY)
+    # Doubling the wait keeps a lossy link from starving the health
+    # channel; the cap stops a long outage from stalling us forever.
+    retry_backoff = min(2 ** attempt, MAX_BACKOFF)
+    time.sleep(retry_backoff)
     try:
         return _write(packet)
     except LinkBusy:
         return send(packet, attempt + 1)
"""

COMMENTS = [
    {"id": 1, "user": "bob", "created": NOW - 3 * 3600, "updated": None,
     "path": None, "rev": None, "line": None, "change": 4821, "parent": None,
     "resolved": False, "deleted": False,
     "body": "Nice — does `MAX_BACKOFF` need to stay under the watchdog\n"
             "timeout? If the link is down for a minute we'd rather give up\n"
             "than get killed mid-sleep."},
    {"id": 2, "user": "alice", "created": NOW - 2 * 3600, "updated": None,
     "path": None, "rev": None, "line": None, "change": 4821, "parent": 1,
     "resolved": False, "deleted": False,
     "body": "Yes: `MAX_BACKOFF` is 8s and the watchdog fires at 30s, so the\n"
             "worst case is one sleep plus a retry. Added a note in\n"
             "`docs/telemetry-notes.md`."},
    {"id": 3, "user": "carol", "created": NOW - 90 * 60, "updated": None,
     "path": None, "rev": None, "line": None, "change": 4821, "parent": None,
     "resolved": True, "deleted": False,
     "body": "Confirmed on the bench rig — job000412 can close."},
]

FOLDER_DIFF = {
    "left": "//rocket/engine/telemetry@4700",
    "right": "//rocket/engine/telemetry",
    "truncated": False,
    "pairs": [
        {"status": "content", "leftFile": "//rocket/engine/telemetry/uplink.py",
         "leftRev": 11, "rightFile": "//rocket/engine/telemetry/uplink.py",
         "rightRev": 14},
        {"status": "content", "leftFile": "//rocket/engine/telemetry/session.py",
         "leftRev": 6, "rightFile": "//rocket/engine/telemetry/session.py",
         "rightRev": 7},
        {"status": "types", "leftFile": "//rocket/engine/telemetry/schema.json",
         "leftRev": 3, "rightFile": "//rocket/engine/telemetry/schema.json",
         "rightRev": 4},
        {"status": "right only", "leftFile": None, "leftRev": None,
         "rightFile": "//rocket/engine/telemetry/backoff.py", "rightRev": 2},
        {"status": "left only", "leftFile": "//rocket/engine/telemetry/retry_timer.py",
         "leftRev": 9, "rightFile": None, "rightRev": None},
    ],
}

# A thread anchored to a line of the diff above (new-side line 90, the
# `retry_backoff = ...` line), which is what the in-diff panel shows.
LINE_COMMENTS = [
    {"id": 11, "user": "carol", "created": NOW - 2 * 3600, "updated": None,
     "path": "//rocket/engine/telemetry/uplink.py", "rev": 14, "line": 90,
     "change": 4821, "parent": None, "resolved": False, "deleted": False,
     "body": "`2 ** attempt` overflows the cap only after 4 tries — worth a\n"
             "comment saying MAX_BACKOFF is the real limit, not the shift."},
    {"id": 12, "user": "alice", "created": NOW - 100 * 60, "updated": None,
     "path": "//rocket/engine/telemetry/uplink.py", "rev": 14, "line": 90,
     "change": 4821, "parent": 11, "resolved": False, "deleted": False,
     "body": "Added — see the two lines above it. cc @dana, since the ground\n"
             "console reads the same constant."},
]

REVIEW = {
    "review": {"change": 4821, "state": "approved", "openedBy": "alice",
               "created": NOW - 3 * 3600, "updated": NOW - 40 * 60,
               "updatedBy": "bob"},
    "events": [
        {"id": 1, "user": "alice", "created": NOW - 3 * 3600, "state": "open",
         "note": "Bench-rig soak is in the description; ready for eyes."},
        {"id": 2, "user": "carol", "created": NOW - 2 * 3600, "state": "needs-work",
         "note": "One naming nit on line 90, otherwise good."},
        {"id": 3, "user": "bob", "created": NOW - 40 * 60, "state": "approved",
         "note": None},
    ],
}

REVIEWS = {"reviews": [
    {"change": 4821, "state": "approved", "openedBy": "alice",
     "created": NOW - 3 * 3600, "updated": NOW - 40 * 60, "updatedBy": "bob",
     "user": "alice", "status": "pending", "time": NOW - 4 * 3600,
     "desc": "Back off the telemetry uplink instead of hammering the link."},
    {"change": 4816, "state": "needs-work", "openedBy": "dana",
     "created": NOW - DAY, "updated": NOW - 5 * 3600, "updatedBy": "alice",
     "user": "dana", "status": "pending", "time": NOW - 30 * 3600,
     "desc": "Ground console: retry the telemetry socket on a clean close."},
    {"change": 4809, "state": "open", "openedBy": "bob",
     "created": NOW - 2 * DAY, "updated": NOW - 2 * DAY, "updatedBy": "bob",
     "user": "bob", "status": "pending", "time": NOW - 2 * DAY,
     "desc": "Pack the flight schema into the nightly artifact."},
]}

MENTIONS = {"unseen": 2, "mentions": [
    {"id": 3, "seen": False, "created": NOW - 100 * 60,
     "comment": dict(LINE_COMMENTS[1], user="alice")},
    {"id": 2, "seen": False, "created": NOW - 5 * 3600,
     "comment": {"id": 21, "user": "bob", "created": NOW - 5 * 3600,
                 "updated": None, "path": None, "rev": None, "line": None,
                 "change": 4816, "parent": None, "resolved": False,
                 "deleted": False,
                 "body": "@dana this is the socket close you hit last week — "
                         "does the retry cover it?"}},
    {"id": 1, "seen": True, "created": NOW - 2 * DAY,
     "comment": {"id": 22, "user": "carol", "created": NOW - 2 * DAY,
                 "updated": None, "path": "//rocket/tests/test_uplink.py",
                 "rev": 3, "line": 57, "change": None, "parent": None,
                 "resolved": False, "deleted": False,
                 "body": "@dana can you confirm the cap here matches the "
                         "console's constant?"}},
]}

COUNTS = {"counts": {
    "4821": {"open": 3, "total": 4},
    "4816": {"open": 1, "total": 1},
}}

BROWSE_ROOT = {
    "path": "",
    "dirs": [{"path": "//rocket", "name": "rocket"},
             {"path": "//ground", "name": "ground"}],
    "files": [],
}

FAVORITES = {"favorites": [
    {"path": "//rocket/engine/telemetry", "added": NOW - 5 * DAY},
    {"path": "//rocket/tests", "added": NOW - 20 * DAY},
]}

FAV_CHANGES = {"changes": [
    {"change": 4821, "user": "alice", "time": NOW - 4 * 3600, "desc":
     "Back off the telemetry uplink instead of hammering the link."},
    {"change": 4814, "user": "bob", "time": NOW - 2 * DAY, "desc":
     "Ground console: show link quality next to the pressure gauge."},
    {"change": 4802, "user": "carol", "time": NOW - 4 * DAY, "desc":
     "Package the telemetry schema with the nightly build."},
], "rawCount": 3, "pageSize": 100, "oldest": 4802}


def handle(route, request):
    url = request.url[len(BASE):]
    path = url.split("?")[0]
    q = url.split("?")[1] if "?" in url else ""

    def send(payload):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload))

    if path == "/api/info":
        return send({"p4port": "ssl:p4.example.com:1666"})
    if path == "/api/login" or path == "/api/me":
        return send({"user": "alice", "p4port": "ssl:p4.example.com:1666"})
    if path == "/api/users":
        return send({"users": USERS, "groups": GROUPS})
    if path == "/api/jobs":
        return send({"jobs": JOBS})
    if path == "/api/favorites":
        return send(FAVORITES)
    if path == "/api/browse":
        return send(BROWSE_ROOT)
    if path == "/api/folderdiff":
        return send(FOLDER_DIFF)
    if path == "/api/search":
        return send(SEARCH_CONTENT)
    if path.startswith("/api/change/"):
        return send(CHANGE)
    if path == "/api/diff":
        return send({"path": CHANGE["files"][0]["path"], "spec1": "#13",
                     "spec2": "#14", "diff": DIFF})
    if path == "/api/comments/counts":
        return send(COUNTS)
    if path == "/api/comments":
        # path= asks about one file; change=…&files=1 asks about a
        # changelist and gets its file-anchored threads too.
        if "path=" in q:
            return send({"comments": LINE_COMMENTS})
        return send({"comments": COMMENTS + LINE_COMMENTS})
    if path == "/api/reviews":
        return send(REVIEWS)
    if path.startswith("/api/review/"):
        return send(REVIEW)
    if path == "/api/mentions":
        return send(MENTIONS)
    if path == "/api/mentions/seen":
        # Opening the page marks them read; the fixture stays unread so
        # re-runs produce the same picture.
        return send({"unseen": MENTIONS["unseen"]})
    if path == "/api/changes":
        return send(FAV_CHANGES)
    if path.startswith("/api/index/"):
        return send({"changes": 0, "newest": 0, "oldest": 0, "updated": 0,
                     "fullyBackfilled": False, "results": [], "changes_list": []})
    # Anything unmocked would leak real data — fail loudly instead.
    print("!! unmocked API call:", url, file=sys.stderr)
    return route.fulfill(status=500, content_type="application/json",
                         body=json.dumps({"detail": "unmocked"}))


SHOTS = []


def shot(page, name, height=None):
    """Screenshot trimmed to the content: a fixed viewport leaves a slab
    of empty page under short views."""
    page.wait_for_timeout(400)
    if height is None:
        # The app scrolls inside its own container, so the page never
        # grows: open it tall, measure where the content actually ends,
        # then shrink the window to exactly that.
        page.set_viewport_size({"width": 1180, "height": 2200})
        page.wait_for_timeout(250)
        # The pane itself stretches to fill the window; its children do
        # not, so the last child's bottom is where content really ends.
        height = page.evaluate(
            "Math.ceil(Math.max(...[...document.querySelectorAll('#view .pane > *')]"
            ".map(e => e.getBoundingClientRect().bottom))) + 28")
        height = max(320, min(int(height), 2200))
    page.set_viewport_size({"width": 1180, "height": height})
    page.wait_for_timeout(250)
    out = OUT / f"guide-{name}.png"
    page.screenshot(path=str(out))
    SHOTS.append(out.name)
    print("wrote", out, out.stat().st_size, "bytes")


with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE)
    page = b.new_page(viewport={"width": 1180, "height": 780},
                      device_scale_factor=2)
    page.route(re.compile(r"/api/"), handle)
    page.goto(BASE + "/")
    # /api/me answers as alice, so the shell comes up already signed in.
    page.wait_for_selector("#app:not(.hidden)", timeout=15000)

    # --- search: content matches grouped by file ---
    page.evaluate("location.hash = '#/search?q=retry_backoff&kind=content&path=//rocket'")
    page.wait_for_selector(".grep-group", timeout=15000)
    shot(page, "search")

    page.evaluate("document.querySelector('#search-q').value = ''")

    # --- change detail: description, files, expanded diff, comments ---
    page.evaluate("location.hash = '#/change/4821'")
    page.wait_for_selector(".chg-head", timeout=15000)
    page.wait_for_selector(".cmt-panel", timeout=15000)
    page.click(".disclosure")
    page.wait_for_selector(".diff-row:not(.hidden) .diff-line, .diff-row:not(.hidden) pre",
                           timeout=15000)
    shot(page, "change")

    # --- a thread opened on a line of that same diff ---
    # The diff above is still expanded: re-setting the same hash would not
    # re-route, and clicking the disclosure again would collapse it. The
    # gutter buttons only fade in on hover, so click through the fade.
    page.wait_for_selector(".dl-cmt.has-threads", state="attached", timeout=15000)
    page.evaluate("document.querySelector('.dl-cmt.has-threads').click()")
    page.wait_for_selector(".dl-panel", timeout=15000)
    shot(page, "inline-comment")

    # --- reviews list ---
    page.evaluate("location.hash = '#/reviews'")
    page.wait_for_selector(".listing tbody tr", timeout=15000)
    shot(page, "reviews")

    # --- mentions of you ---
    page.evaluate("location.hash = '#/mentions'")
    page.wait_for_selector(".mn-item", timeout=15000)
    shot(page, "mentions")

    # --- folder diff: one directory against its own older state ---
    page.evaluate(
        "location.hash = '#/folderdiff?left=' + "
        "encodeURIComponent('//rocket/engine/telemetry@4700') + '&right=' + "
        "encodeURIComponent('//rocket/engine/telemetry')")
    page.wait_for_selector(".listing tbody tr", timeout=15000)
    shot(page, "folderdiff")

    # --- More menu open over the Jobs browser ---
    page.evaluate("location.hash = '#/jobs'")
    page.wait_for_selector(".listing tbody tr", timeout=15000)
    page.hover("#more-menu .dropdown-toggle")
    page.wait_for_timeout(300)
    shot(page, "more")

    b.close()

print("done:", ", ".join(SHOTS))
