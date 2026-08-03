"""Setup mode: a loopback-only server that writes config.toml, and the TOML
emitter behind it.

It runs only when the loop does not. There is no shared state with the loop
at all, which is why this can write a file the dashboard's own rules would
forbid.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import threading
import tomllib
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import web, worktree
from .config import DEFAULT_CONFIG, HOME, LOOPBACK_HOSTS, SCHEMA, Config, _compose_jql, validate
from .jira import JiraClient, JiraError, compose_jql

log = logging.getLogger("claudeloop.setup")

STATIC = Path(__file__).parent / "static"

STEPS = (
    {"id": "repository", "title": "Repository"},
    {"id": "source", "title": "Task source"},
    {"id": "dashboard", "title": "Dashboard"},
    {"id": "instructions", "title": "Instructions"},
    {"id": "plugins", "title": "Plugins"},
    {"id": "advanced", "title": "Advanced"},
    {"id": "review", "title": "Review and save"},
)

MAX_BODY_BYTES = 256 * 1024
"""A whole config is a few kilobytes. This bounds what an unauthenticated
peer can make the process allocate before the token check has even run."""

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _scalar(value: object) -> str:
    """One TOML value.

    Strings go through json.dumps: TOML's basic string accepts JSON's escape
    set, so quotes, backslashes, tabs and control characters are all handled
    by the stdlib rather than by hand.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    # JSON's escape set covers TOML's basic string for ", \ and U+0000..U+001F,
    # but the two disagree at both ends of the range. ensure_ascii=True encodes
    # a non-BMP character as a surrogate pair, which TOML rejects outright as
    # not a Unicode scalar value -- and one emoji in a [session_env] value then
    # makes tomllib fail on the whole file, so the config the wizard just wrote
    # cannot be read back. ensure_ascii=False fixes that and opens the other
    # end: it emits U+007F raw, which TOML forbids in a basic string. That one
    # character is the entire remaining gap.
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def _key(name: str) -> str:
    """A bare key where TOML allows one, a quoted key otherwise.

    [session_env] names are operator input, not schema data: a space or a
    quote in one would break the whole file, and a dot would silently parse
    as a nested table instead of the name the operator typed.
    """
    return name if _BARE_KEY.match(name) else _scalar(name)


def _blank(value: object) -> bool:
    """Whether to leave the key out entirely.

    An emitted `settings_file = ""` reads back as a path that does not exist,
    and load_config would then refuse the file this module just wrote. False
    and 0 are real values, not blanks.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def _wrap(text: str, width: int = 74) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def dump_toml(data: dict) -> str:
    """`data` in the config.toml shape, as the text of that file.

    Key order and the comments both come from SCHEMA, so a key added to the
    table is documented in every file written afterwards for free. Any key
    in `data` not named in SCHEMA, and any section other than the top level,
    "jira" and "session_env", is silently dropped -- SCHEMA is the single
    source of truth for what gets written.
    """
    out: list[str] = ["# Written by ClaudeLoop's setup wizard.",
                      "# Re-run it with: python -m claudeloop --setup", ""]
    for section in ("", "jira"):
        fields = [f for f in SCHEMA if f.section == section]
        table = data.get(section) if section else data
        if not isinstance(table, dict):
            continue
        emitted = [f for f in fields if not _blank(table.get(f.name))]
        if not emitted:
            continue
        if section:
            out.append(f"[{section}]")
        for field in emitted:
            for line in _wrap(field.help):
                out.append(f"# {line}")
            out.append(f"{field.name} = {_scalar(table[field.name])}")
            out.append("")
    env = data.get("session_env")
    if isinstance(env, dict) and env:
        out.append("# Extra environment variables for every session.")
        out.append("[session_env]")
        for name, value in env.items():
            out.append(f"{_key(name)} = {_scalar(value)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def field_payload(field) -> dict:
    return {
        "key": field.key,
        "name": field.name,
        "section": field.section,
        "type": field.type,
        "default": str(field.default) if isinstance(field.default, Path) else field.default,
        "step": field.step,
        "label": field.label,
        "help": field.help,
        "secret": field.secret,
        "choices": list(field.choices),
        # Unconditional only. A required_if field (tasks_file, web_token,
        # every jira.* key) is only required in some states -- web_token at
        # the loopback default is not one of them -- and the page has no way
        # to re-evaluate the condition itself, so folding it in here marked
        # "Dashboard token *" as required even when it plainly is not.
        "required": field.required,
    }


def schema_payload(existing: dict) -> dict:
    """The table, plus whatever is already configured -- minus every secret.

    Secret values never leave this process. The browser is told only which
    ones are set, so the form can say "leave blank to keep".
    """
    values: dict = {}
    secrets_set: list[str] = []
    for field in SCHEMA:
        table = existing.get(field.section) if field.section else existing
        if not isinstance(table, dict) or field.name not in table:
            continue
        if field.secret:
            if str(table[field.name]).strip():
                secrets_set.append(field.key)
            continue
        target = values.setdefault(field.section, {}) if field.section else values
        target[field.name] = table[field.name]
    env = existing.get("session_env")
    return {
        "fields": [field_payload(field) for field in SCHEMA],
        "steps": list(STEPS),
        "values": values,
        "secrets_set": secrets_set,
        # Names, never values: a [session_env] entry is a credential by
        # definition -- that is what the table is for.
        "session_env": {name: "" for name in env} if isinstance(env, dict) else {},
        "editing": bool(existing),
    }


CHECK_TIMEOUT_S = 30
"""Bounds the claude subprocess. An operator is watching a spinner; a probe
that can hang forever is worse than one that reports a timeout."""


def check_repo(values: dict) -> tuple[bool, str]:
    """The same probe main() runs at startup, run early enough to matter."""
    repo = str(values.get("repo", "")).strip()
    if not repo:
        return False, "no repository set"
    problem = worktree.probe(Path(repo).expanduser())
    return (False, problem) if problem else (
        True, f"{repo} is usable: git worktrees work and the default branch resolves",
    )


def _unsafe_jira_site(site: str) -> str | None:
    """None when it is safe to send Basic-auth credentials to `site`.

    Same reasoning as config._https_site: urllib forwards the Authorization
    header across a redirect, so a plain http:// site puts the Jira API
    token on the wire in cleartext the first time Jira redirects it. The one
    exception is a loopback address -- it cannot leave the machine, so no
    credential reaches a wire -- which is what lets this check run at all
    against tests/jira_fake.FakeJira, a plain-HTTP stand-in for Jira.
    """
    parsed = urlparse(site)
    if parsed.scheme == "https":
        return None
    if parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS:
        return None
    return (
        f"Jira site {site!r} must start with https:// -- urllib forwards the"
        " Authorization header across a redirect, so an http:// site puts"
        " the Basic-auth API token on the wire in cleartext the first time"
        " Jira redirects it."
    )


def check_jira(values: dict) -> tuple[bool, str]:
    """One authenticated search with the composed query.

    A bad token, an unreachable site and a JQL Jira rejects are otherwise
    indistinguishable from an empty backlog, forever, with nothing saying why
    -- the loop is built to idle rather than fail on all three. This is the
    only place that difference is ever visible.
    """
    table = values.get("jira")
    if not isinstance(table, dict):
        # "jira": "oops" -- validate() reports this properly a moment later
        # (see merge_secrets); this only has to not crash getting here.
        table = {}
    site = str(table.get("site", "")).strip()
    if not site:
        return False, "no Jira site set"
    unsafe = _unsafe_jira_site(site)
    if unsafe:
        return False, unsafe
    jql = str(table.get("jql", "")).strip() or _compose_jql(
        str(table.get("project", "")).strip(), str(table.get("status", "")).strip()
    )
    client = JiraClient(site, str(table.get("email", "")), str(table.get("token", "")),
                        retries=1)
    try:
        data = client.search(compose_jql(jql), max_results=50)
    except JiraError as error:
        return False, str(error)
    issues = data.get("issues")
    if not isinstance(issues, list):
        return False, f"Jira answered without an issue list: {str(data)[:200]}"
    return True, (
        f"{len(issues)} issue(s) match on the first page. Query: {compose_jql(jql)}"
    )


def check_claude(values: dict) -> tuple[bool, str]:
    """`claude auth status --json`, with [session_env] applied.

    Applied because that is what a session gets: an ANTHROPIC_API_KEY or a
    CLAUDE_CODE_USE_BEDROCK in that table quietly moves every session off
    subscription billing, and the rate_limit_events the entire recovery path
    is built on then stop arriving, with nothing on the dashboard to say so.
    Reported here as a changed authMethod.
    """
    env = {**os.environ}
    session_env = values.get("session_env")
    if isinstance(session_env, dict):
        env.update({str(k): str(v) for k, v in session_env.items()})
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=CHECK_TIMEOUT_S,
            stdin=subprocess.DEVNULL, env=env,
        )
    except FileNotFoundError:
        return False, "claude is not on PATH. Install the Claude Code CLI first."
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"could not run claude: {error}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, (result.stderr or result.stdout or "claude said nothing").strip()[:400]
    if not isinstance(payload, dict):
        # json.loads is happy with null, a list or a bare string, and none of
        # them have .get -- the same hazard jira.py's _once already guards.
        # Reachable from a `claude` that is really a wrapper or an alias.
        return False, f"claude answered with {type(payload).__name__}, not an object"
    if not payload.get("loggedIn"):
        return False, "claude is installed but not signed in. Run: claude setup-token"
    method = payload.get("authMethod", "unknown")
    plan = payload.get("subscriptionType", "")
    return True, f"signed in via {method}{f' ({plan})' if plan else ''}"


CHECKS = {"repo": check_repo, "jira": check_jira, "claude": check_claude}


class Handler(web.Handler):
    """Subclassed, not rewritten. `_host_allowed`, `_authorized`, `_json` and
    -- the load-bearing one -- do_POST's `close_connection = True` all come
    from web.Handler. That line is the request-smuggling fix; a hand-rolled
    second handler is how a project loses it."""

    server_version = "ClaudeLoopSetup"

    def do_GET(self) -> None:
        if not self._host_allowed():
            self._json(403, {"error": "bad host"})
            return
        parsed = urlparse(self.path)
        if not self._authorized(parsed.query):
            self._json(403, {"error": "bad or missing token"})
            return
        if parsed.path == "/":
            self._file(STATIC / "setup.html", "text/html; charset=utf-8")
        elif parsed.path in ("/logo.png", "/favicon.ico"):
            self._file(STATIC / "logo.png", "image/png", cache=True)
        elif parsed.path == "/api/setup/schema":
            self._json(200, schema_payload(self.server.existing))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        self.close_connection = True
        if not self._host_allowed():
            self._json(403, {"error": "bad host"})
            return
        parsed = urlparse(self.path)
        if not self._authorized(parsed.query):
            self._json(403, {"error": "bad or missing token"})
            return
        route = parsed.path
        if route == "/api/setup/validate":
            self._validate()
        elif route == "/api/setup/save":
            self._save()
        elif route == "/api/setup/test":
            self._test()
        else:
            self._json(404, {"error": "not found"})

    def _submitted(self) -> dict | None:
        payload = self._read_json()
        if payload is None:
            return None
        values = payload.get("values")
        if not isinstance(values, dict):
            self._json(400, {"error": 'expected a JSON object with a "values" table'})
            return None
        return merge_secrets(values, self.server.existing)

    def _validate(self) -> None:
        values = self._submitted()
        if values is None:
            return
        _, errors = validate(values)
        self._json(200, {"errors": dict(errors)})

    def _save(self) -> None:
        merged = self._submitted()
        if merged is None:
            return
        values, errors = validate(merged)
        if errors:
            self._json(400, {"errors": dict(errors)})
            return
        try:
            write_config(self.server.path, _typed(merged, values))
        except OSError as error:
            self._json(500, {"error": f"could not write the config: {error}"})
            return
        self._json(200, {"ok": True})
        # Released after the response is written, so the operator's browser
        # gets its answer before run_setup returns and the server shuts down.
        self.server.saved.set()

    def _test(self) -> None:
        """A live check reports what the network/CLI says; it deliberately
        does not run validate() first -- see check_jira's docstring for why
        conflating the two would make the Jira check untestable and, worse,
        would require a perfect form before any check could run at all."""
        payload = self._read_json()
        if payload is None:
            return
        check = CHECKS.get(str(payload.get("what", "")))
        if check is None:
            self._json(400, {"error": f"unknown check {payload.get('what')!r}"})
            return
        values = payload.get("values")
        if not isinstance(values, dict):
            values = {}
        values = merge_secrets(values, self.server.existing)
        ok, message = check(values)
        self._json(200, {"ok": ok, "message": message})

    def _read_json(self) -> dict | None:
        """The request body, or None having already answered with an error."""
        if self.headers.get_content_type() != "application/json":
            # A cross-origin fetch with this content type triggers a CORS
            # preflight this server never answers, so the browser does not
            # send the POST at all; an HTML form cannot set it either.
            self._json(415, {"error": "expected application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "bad content length"})
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": f"the body must be 1..{MAX_BODY_BYTES} bytes"})
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "expected a JSON object"})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": "expected a JSON object"})
            return None
        return payload


def merge_secrets(submitted: dict, existing: dict) -> dict:
    """Put the stored value back wherever a secret came back blank.

    The browser is never sent a secret, so a blank secret field means
    "unchanged", not "clear it". A [session_env] name the operator deleted is
    absent rather than blank, and is genuinely removed.
    """
    merged = {key: dict(value) if isinstance(value, dict) else value
              for key, value in submitted.items()}
    for field in SCHEMA:
        if not field.secret:
            continue
        # setdefault, not .get: a submission with no [jira] table at all --
        # the ordinary shape when source = "file" -- is "unchanged" for the
        # same reason a blank field is, so a stored jira.token survives a
        # save the operator never mentioned Jira in.
        table = merged.setdefault(field.section, {}) if field.section else merged
        if not isinstance(table, dict):
            # A submitted section that is not a table at all -- "jira": "oops".
            # setdefault does not replace an existing non-dict value, so this
            # would otherwise reach .get() and take down the request thread
            # with no response at all. validate() reports it properly a moment
            # later; this only has to not crash first.
            continue
        old = existing.get(field.section, {}) if field.section else existing
        if not isinstance(old, dict):
            continue
        if not str(table.get(field.name, "")).strip() and str(old.get(field.name, "")).strip():
            table[field.name] = old[field.name]
    env = merged.get("session_env")
    old_env = existing.get("session_env")
    if isinstance(env, dict) and isinstance(old_env, dict):
        for name, value in env.items():
            if not str(value).strip() and str(old_env.get(name, "")).strip():
                env[name] = old_env[name]
    return merged


def _typed(submitted: dict, coerced: dict) -> dict:
    """The submitted config, with every value replaced by validate()'s coerced one.

    A browser form posts every field as a string, so writing the submission
    verbatim puts `web_port = "9999"` and `strict_mcp = "false"` in the file --
    quoted strings that load_config only survives because its coercion is
    lenient, and that come back to the wizard as JS-truthy strings on the next
    --setup. Only keys the operator actually gave are written: `coerced` also
    carries every default, and writing those out would pin this version's
    defaults into the operator's file forever.
    """
    out: dict = {}
    for field in SCHEMA:
        table = submitted.get(field.section) if field.section else submitted
        if not isinstance(table, dict) or _blank(table.get(field.name)):
            continue
        target = out.setdefault(field.section, {}) if field.section else out
        target[field.name] = coerced[field.key]
    env = submitted.get("session_env")
    if isinstance(env, dict) and env:
        out["session_env"] = dict(env)
    return out


def write_config(path: Path, values: dict) -> None:
    """Write config.toml at 0600, whatever the umask is.

    load_config refuses a config readable beyond its owner, so a file written
    at the default 0644 would be one the loop starting seconds later cannot
    read. fchmod on the open fd, not a chmod afterwards: O_CREAT's mode
    applies only to a file this call creates. Rewriting an existing config --
    which is the --setup path, and may well be sitting at the 0644 the
    operator opened the wizard to escape -- would otherwise put a Jira API
    token on disk world-readable for the length of the write. fchmod on the
    open fd closes that window for both cases.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dump_toml(values)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


class _SetupServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, path: Path, home: Path, token: str):
        # Setup mode binds loopback unconditionally, whatever an existing
        # config says: with no config there is no web_token to authenticate
        # against, so the network barrier cannot be the only one.
        self.cfg = Config(repo=Path("."), web_host="127.0.0.1", web_token=token, home=home)
        self.path = path
        self.home = home
        self.saved = threading.Event()
        self.existing = _read_existing(path)
        super().__init__(address, handler)


def _read_existing(path: Path) -> dict:
    """The current config as raw TOML data, or {} on a first run.

    Read as data, not through load_config: a config that no longer validates
    -- a repo that moved, a settings_file that was deleted -- is exactly the
    one an operator is opening the wizard to fix.
    """
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def serve(path: Path, home: Path, port: int, token: str) -> _SetupServer:
    server = _SetupServer(("127.0.0.1", port), Handler, path, home, token)
    threading.Thread(
        target=server.serve_forever, name="claudeloop-setup", daemon=True
    ).start()
    return server


def run_setup(path: Path = DEFAULT_CONFIG, home: Path = HOME, port: int = 8765) -> None:
    """Serve the wizard until it has written a valid config, then return.

    Blocking on purpose: main() calls this and then falls through into the
    ordinary startup path, so the config the loop runs is the one the
    ordinary loader read back off disk.
    """
    token = secrets.token_urlsafe(32)
    server = serve(path, home, port, token)
    url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    opening = (
        "Editing the ClaudeLoop configuration"
        if server.existing
        else "ClaudeLoop is not configured yet"
    )
    log.warning("%s. Open the setup wizard:\n\n    %s\n", opening, url)
    try:
        server.saved.wait()
    except KeyboardInterrupt:
        raise SystemExit("setup cancelled")
    finally:
        server.shutdown()
        server.server_close()
