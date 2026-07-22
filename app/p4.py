"""Thin wrapper around the p4 command-line client.

All server communication goes through the `p4` binary. Structured
commands use `p4 -G` (marshaled dict output); text commands (print,
diff2) capture raw stdout. Credentials are passed per-call with
`-u user -P ticket` so one process can serve many logged-in users.
"""

import marshal
import os
import re
import subprocess

P4BIN = os.environ.get("P4WEB_P4BIN", "p4")


def _detect_port():
    port = os.environ.get("P4WEB_P4PORT") or os.environ.get("P4PORT")
    if port:
        return port
    # P4PORT may live in P4ENVIRO/P4CONFIG rather than the shell env;
    # ask the p4 binary what it resolves to.
    try:
        out = subprocess.run(
            [P4BIN, "set", "-q", "P4PORT"], capture_output=True, timeout=10
        )
        line = out.stdout.decode("utf-8", errors="replace").strip()
        if line.startswith("P4PORT="):
            return line.split("=", 1)[1]
    except Exception:
        pass
    return "perforce:1666"


P4PORT = _detect_port()

# Read-only commands plus the write set used by the per-user
# workspace feature; anything else is refused as a safety net.
ALLOWED_COMMANDS = {
    "depots", "dirs", "files", "fstat", "print", "filelog",
    "annotate", "diff2", "changes", "describe", "users", "sizes",
    "grep", "labels", "label", "jobs", "job", "fixes",
    "branches", "branch", "streams", "groups",
    "login", "logout",
    # write operations (guarded by the API layer: own client/CL only)
    "client", "change", "opened", "sync", "where",
    "edit", "add", "delete", "revert", "submit",
    "shelve", "unshelve",
}


def escape_path(path):
    """Escape p4 filespec wildcard characters in a literal depot path."""
    return (
        path.replace("%", "%25")
        .replace("@", "%40")
        .replace("#", "%23")
        .replace("*", "%2A")
    )


class P4Error(Exception):
    def __init__(self, message, severity=3):
        super().__init__(message)
        self.severity = severity


class P4AuthError(P4Error):
    pass


AUTH_ERROR_MARKERS = (
    "Perforce password (P4PASSWD) invalid or unset",
    "Your session has expired",
    "Access for user",
    "User %s doesn't exist",
)


def _env():
    """Environment for p4 subprocesses. P4TICKETS is pointed at devnull
    so authentication comes only from the explicit -P ticket — otherwise
    p4 silently falls back to the server operator's own tickets file,
    which would let any web user act as the local OS user."""
    env = dict(os.environ)
    env["P4TICKETS"] = os.devnull
    # Ignore rules from the operator's environment (or a .p4ignore
    # sitting above the data directory) must not affect users' adds:
    # the p4-web workspace lives inside this project tree, whose own
    # .p4ignore silently vetoed adds during testing.
    env["P4IGNORE"] = os.devnull
    return env


def _base_cmd(user=None, ticket=None, client=None):
    cmd = [P4BIN, "-p", P4PORT]
    if user:
        cmd += ["-u", user]
    if ticket:
        cmd += ["-P", ticket]
    if client:
        cmd += ["-c", client]
    return cmd


def _check_command(args):
    if not args or args[0] not in ALLOWED_COMMANDS:
        raise P4Error(f"p4 command not allowed: {args[0] if args else '(none)'}")


def _decode(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _raise_if_auth_error(message):
    lowered = message.lower()
    if "password" in lowered and "invalid" in lowered:
        raise P4AuthError(message)
    if "session has expired" in lowered or "please login" in lowered.replace("'", ""):
        raise P4AuthError(message)
    if "access for user" in lowered:
        raise P4AuthError(message)


def run(args, user=None, ticket=None, stdin=None, collect_errors=False, client=None):
    """Run a p4 command with -G and return a list of dict records.

    Error records with severity >= 3 raise P4Error (or P4AuthError for
    login-related failures). Warning-level records are dropped.

    With collect_errors=True, returns (results, errors) instead and
    error records don't raise — for commands like grep that emit
    partial results followed by limit errors.
    """
    _check_command(args)
    cmd = _base_cmd(user, ticket, client)
    cmd.insert(1, "-G")
    cmd += args
    proc = subprocess.run(
        cmd,
        input=stdin.encode() if isinstance(stdin, str) else stdin,
        capture_output=True,
        timeout=60,
        env=_env(),
    )
    records = []
    offset = 0
    out = proc.stdout
    while offset < len(out):
        try:
            record, offset = _load_one(out, offset)
        except (ValueError, EOFError):
            break
        records.append(record)

    results = []
    errors = []
    for rec in records:
        rec = {_decode(k): _decode(v) for k, v in rec.items()}
        if rec.get("code") == "error":
            severity = int(rec.get("severity", 3))
            if severity >= 3:
                message = rec.get("data", "unknown p4 error").strip()
                _raise_if_auth_error(message)
                if collect_errors:
                    errors.append(message)
                    continue
                raise P4Error(message, severity)
            continue
        if rec.get("code") == "info" or rec.get("code") == "stat":
            results.append(rec)
    if proc.returncode != 0 and not results and not records:
        message = proc.stderr.decode("utf-8", errors="replace").strip()
        _raise_if_auth_error(message)
        raise P4Error(message or f"p4 exited with {proc.returncode}")
    if collect_errors:
        return results, errors
    return results


def _load_one(buf, offset):
    """marshal.loads doesn't report consumed length; use a stream."""
    import io
    stream = io.BytesIO(buf)
    stream.seek(offset)
    record = marshal.load(stream)
    if not isinstance(record, dict):
        raise ValueError("unexpected p4 -G record")
    return record, stream.tell()


def run_text(args, user=None, ticket=None, stdin=None, client=None):
    """Run a p4 command and return raw stdout bytes (no -G).

    Used for commands whose output is a document rather than records
    (print, diff2) and for spec forms fed via stdin (`change -i`,
    `client -i` — with -G those would expect marshaled input).
    """
    _check_command(args)
    cmd = _base_cmd(user, ticket, client) + args
    proc = subprocess.run(
        cmd,
        input=stdin.encode() if isinstance(stdin, str) else stdin,
        capture_output=True,
        timeout=120,
        env=_env(),
    )
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", errors="replace").strip()
        _raise_if_auth_error(message)
        raise P4Error(message or f"p4 exited with {proc.returncode}")
    return proc.stdout


PRINT_CHUNK = 256 * 1024


def print_open(spec, user=None, ticket=None):
    """Start `p4 print -q <spec>` and return (proc, first_chunk).

    Streams a file revision straight from p4 instead of buffering the
    whole thing in memory. The first chunk is read eagerly so that an
    immediate failure (no read permission, missing revision) still
    surfaces as a P4Error/P4AuthError — same error translation the
    buffered path gets — before any response headers are sent. The
    caller must then iterate print_drain(proc, first_chunk)."""
    _check_command(["print"])
    cmd = _base_cmd(user, ticket) + ["print", "-q", spec]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env()
    )
    first = proc.stdout.read(PRINT_CHUNK)
    if not first:
        # No content: an empty file, or an error with nothing on stdout.
        stderr = proc.stderr.read()
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()
        message = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or message:
            _raise_if_auth_error(message)
            raise P4Error(message or f"p4 exited with {proc.returncode}")
    return proc, first


def print_drain(proc, first):
    """Generator yielding the rest of a print_open() stream."""
    try:
        if first:
            yield first
        while True:
            chunk = proc.stdout.read(PRINT_CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        terminate_stream(proc)


def terminate_stream(proc):
    """Idempotently tear down a print_open() subprocess.

    Safe to call whether or not the stream was fully drained, and safe to
    call more than once. This is the single cleanup path for a streamed
    `p4 print`: print_drain()'s finally calls it on the happy path, and
    the /api/raw route attaches it as a response background task so it
    also runs when a mid-stream client disconnect abandons the body
    generator.

    The kill matters. When a browser navigates away from a page full of
    image previews it cancels every in-flight /api/raw at once; the
    matching `p4 print` is then left writing into a stdout pipe nobody is
    draining. Once the 64 KiB pipe fills it blocks in write() forever,
    never exits, and keeps its p4d TCP connection ESTABLISHED — surfacing
    as a lingering IDLE command in `p4 monitor show` that holds a server
    process slot and an FD until the app is restarted. Killing the
    process (and closing our read ends, which sends it EPIPE as a
    backstop) lets it exit and release the connection."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def login(user, secret):
    """Exchange credentials for a ticket. `secret` may be the account
    password or an existing ticket value (as Swarm allows).

    Returns (ticket, owned): `owned` is False when the user pasted an
    existing ticket — that credential is shared with their other
    clients (P4V, CLI), so logout must not invalidate it server-side.
    """
    cmd = _base_cmd(user) + ["login", "-p"]
    proc = subprocess.run(
        cmd, input=secret.encode(), capture_output=True, timeout=30, env=_env()
    )
    if proc.returncode != 0:
        # Not a valid password; check whether it works as a ticket.
        check = subprocess.run(
            _base_cmd(user, secret) + ["login", "-s"],
            capture_output=True, timeout=30, env=_env(),
        )
        if check.returncode == 0:
            return secret, False
        message = proc.stderr.decode("utf-8", errors="replace").strip()
        raise P4AuthError(message or "login failed")
    # Output is the password prompt followed by the ticket on its own line.
    lines = [
        line.strip()
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip() and " " not in line.strip()
    ]
    if not lines:
        raise P4AuthError("login succeeded but no ticket was returned")
    return lines[-1], True


# "User bob ticket expires in 11 hours 59 minutes." — the unit list
# varies with how much time is left, so parse whatever units appear.
_TICKET_UNITS = {
    "second": 1, "minute": 60, "hour": 3600,
    "day": 86400, "week": 604800,
}


def ticket_seconds_left(user, ticket):
    """How long `ticket` stays valid, per `p4 login -s`.

    Returns the remaining seconds, or None when the server accepts the
    ticket but words the answer in a way we can't parse (treat that as
    "valid, unknown lifetime"). Raises P4AuthError once the ticket is
    no longer good — expired here or invalidated by a `p4 logout`
    somewhere else.
    """
    proc = subprocess.run(
        _base_cmd(user, ticket) + ["login", "-s"],
        capture_output=True, timeout=30, env=_env(),
    )
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise P4AuthError(out or "your Perforce ticket is no longer valid")
    match = re.search(r"expires in (.+?)\.", out, re.I)
    if not match:
        return None
    total = 0
    parts = re.findall(
        r"(\d+)\s*(week|day|hour|minute|second)s?", match.group(1), re.I
    )
    for amount, unit in parts:
        total += int(amount) * _TICKET_UNITS[unit.lower()]
    return total if parts else None


def logout(user, ticket):
    try:
        cmd = _base_cmd(user, ticket) + ["logout"]
        subprocess.run(cmd, capture_output=True, timeout=30, env=_env())
    except Exception:
        pass
