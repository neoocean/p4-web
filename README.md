# p4-web

A lightweight web UI for Perforce — a P4V / Swarm alternative sized
for individuals and small teams. Browse the depot, view files with
syntax highlighting, inspect history/diffs/annotations, search, and
edit + submit changes through per-user server-side workspaces, all
from a browser.

No database, no build step, no external services: a single FastAPI
process that shells out to the `p4` command-line client.

## Features

- **Login with your Perforce account** — credentials are exchanged for
  a `p4 login` ticket; the password itself is never stored. A ticket
  value can be pasted in place of the password (like Swarm).
- **Depot browser** — navigate depots/directories with breadcrumbs;
  file listings show revision, changelist, type and modified time.
- **File viewer** — syntax-highlighted content (highlight.js, vendored,
  works offline) with a line-number gutter and a revision dropdown.
  Binary and oversized (>2 MB) files are detected and skipped.
- **History** — full `filelog` including renames/branches (one section
  per depot path), action badges, per-revision "diff prev".
- **Diff viewer** — unified `diff2` output rendered GitHub-style.
- **Annotate (blame)** — per-line changelist + author gutter, linked to
  the change view.
- **Changelists** — submitted/pending lists with user/path/text/date
  filters, keyset paging ("Load more"), and a detail view with the
  full description, linked jobs, and per-file inline diffs
  (lazy-loaded; shelved files diff against their base revision).
- **Search** — file-name search across depots and content search
  (`p4 grep`) within a scope; matches jump to the highlighted line.
- **Diff tools** — unified and side-by-side views everywhere,
  arbitrary revision-pair selection from history, folder diff between
  two directory specs (optionally @change/@date).
- **Time-lapse annotate** — a revision slider re-blames the file at
  any point of its history; history shows integration provenance
  (branch/copy/merge/move) per revision.
- **Depot tree sidebar** — collapsible lazy-loading tree synced with
  deep links, P4V-style.
- **Metadata browsers** — labels, jobs (with fixes), branch mappings,
  streams, users & groups under the "More" menu.
- **Images & raw** — inline image previews, raw view and download for
  any revision (served with CSP sandbox).
- **Mobile-friendly** — responsive layout below 768px, whole-row tap
  targets, slide-over depot tree.
- **Editing (My Changes)** — per-user server-side workspaces
  (`p4web-<user>`): create pending changelists, edit text files in a
  web editor, upload new/replacement files (binary-safe), mark files
  for delete, revert, shelve/unshelve, and submit behind a
  confirmation dialog. Users can only touch their own p4-web pending
  changelists; every write lands in `data/audit.log`.
- **Dark mode** — follows the system by default with a manual
  auto/dark/light toggle.
- **Login throttling** — per-account and per-IP sliding-window limits
  on the login endpoint.

## Requirements

- Python 3.10+
- The `p4` command-line client on `PATH` (or set `P4WEB_P4BIN`)
- Network access to your Perforce server

## Running

```sh
./run.sh                       # creates .venv on first run, then serves
```

which is equivalent to:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Then open http://127.0.0.1:8080 and log in with a Perforce account.

### Configuration (environment variables)

| Variable       | Default                                  | Meaning                        |
| -------------- | ---------------------------------------- | ------------------------------ |
| `P4WEB_P4PORT` | `P4PORT` env, then `p4 set`, then `perforce:1666` | Perforce server address |
| `P4WEB_P4BIN`  | `p4`                                     | Path to the p4 CLI             |
| `P4WEB_HOST`   | `127.0.0.1`                              | Bind address (run.sh)          |
| `P4WEB_PORT`   | `8080`                                   | HTTP port (run.sh)             |

## Deploying

Sessions persist in `data/sessions.db` (override the location with
`P4WEB_DATA`), so restarts don't log anyone out.

**Docker** — the image bundles the p4 CLI; sessions and the SSL trust
file live on the `/data` volume:

```sh
docker compose up -d        # edit P4WEB_P4PORT in docker-compose.yml first
```

For `ssl:` servers set `P4WEB_AUTO_TRUST=1` to accept the fingerprint
on first connect (trust-on-first-use), or run `p4 trust` against the
volume yourself.

**macOS (launchd)** — edit the paths in
`deploy/com.p4web.launchd.plist`, copy it to `~/Library/LaunchAgents/`
and `launchctl load` it.

**Linux (systemd)** — edit `deploy/p4web.service`, copy to
`/etc/systemd/system/`, then `systemctl enable --now p4web`.

## Security notes

- Each API call runs `p4` with the session's own user + ticket, so
  Perforce protections apply exactly as they would in P4V.
- `P4TICKETS` is pointed at `/dev/null` for every subprocess: only the
  explicit per-session ticket authenticates, never the tickets file of
  the OS user running the server.
- Sessions are in-memory and expire after 12 hours; restart logs
  everyone out.
- The p4 wrapper whitelists read-only commands (`dirs`, `files`,
  `print`, `filelog`, `annotate`, `diff2`, `changes`, `describe`, …) —
  nothing in the app can submit, edit or delete server state.
- If you expose this beyond localhost, put it behind HTTPS (a reverse
  proxy such as Caddy or nginx) — the login form posts the password in
  the request body.

## Architecture

```
app/
  main.py      FastAPI routes (auth, browse, file, filelog, diff,
               annotate, changes, describe)
  p4.py        p4 CLI wrapper: -G marshal parsing, raw text commands,
               login, path escaping, error -> HTTP mapping
  sessions.py  in-memory cookie-sid -> {user, ticket} store
static/
  index.html   login screen + app shell
  app.js       hash-routed SPA (no framework, no build)
  style.css
  vendor/      highlight.js + github theme (offline)
```

API summary (all JSON, cookie-authenticated except `/api/info`):

```
POST /api/login            {user, password|ticket} -> session cookie
POST /api/logout
GET  /api/me
GET  /api/info             which P4PORT this instance targets
GET  /api/browse?path=     one directory level (empty path = depots)
GET  /api/file?path=&rev=  metadata + inline content for text files
GET  /api/filelog?path=    history incl. renames (segments)
GET  /api/diff?path=&rev1=&rev2=[&path2=]   unified diff
GET  /api/annotate?path=[&rev=]             blame lines
GET  /api/changes?status=&user=&path=&max=&before=
GET  /api/change/{n}       describe: meta + file list (+shelved flag)
```
