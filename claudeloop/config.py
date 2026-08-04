"""Load and validate the ClaudeLoop configuration file."""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


HOME = Path.home() / ".claudeloop"
DEFAULT_CONFIG = HOME / "config.toml"
SOURCES = ("file", "jira")
DEFAULT_ORDER = "ORDER BY created ASC"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
WILDCARD_HOSTS = ("0.0.0.0", "::")

INGRESS_ENV = "CLAUDELOOP_INGRESS"
INGRESS_HOST = "0.0.0.0"
INGRESS_PORT = 8765
"""Must match addon/config.yaml's ingress_port. Under ingress the bind is
ClaudeLoop's own decision rather than the operator's: web_host and web_port
describe a listener a browser reaches directly, and there is not one -- the
supervisor proxies from its own container, and nothing is published to the
host at all."""


def ingress() -> bool:
    """Whether this process is behind Home Assistant's ingress proxy.

    An environment variable rather than a config key, because the setup wizard
    has to be reachable on a box that has no config.toml yet -- there is
    nothing to read a key out of. Read on every call rather than captured at
    import, so a test can set it around a running server.
    """
    return os.environ.get(INGRESS_ENV) == "1"


def bind_address(host: str, port: int) -> tuple[str, int]:
    """Where a server actually listens.

    The operator's choice, or the ingress one when the supervisor is the only
    route in. Both servers go through this: the dashboard's loopback default
    and setup mode's unconditional loopback are equally unreachable from
    another container.
    """
    return (INGRESS_HOST, INGRESS_PORT) if ingress() else (host, port)


def _secrets_file_guard(path: Path) -> None:
    """Refuse to load a config readable beyond its owner.

    This file holds web_token, [session_env] credentials, and — once S3
    lands — a Jira API token.
    """
    if os.name != "posix":
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(
            f"{path}: this file holds secrets but its mode is {mode:03o},"
            f" readable beyond its owner. Run: chmod 600 {path}"
        )


from collections.abc import Callable

Condition = Callable[[dict], bool]
Validator = Callable[[object, dict], "str | None"]


@dataclass(frozen=True)
class Field:
    """One config.toml key: how to read it, and how to explain it.

    This table is the only description of the configuration there is.
    load_config walks it, and the setup wizard renders a form from the same
    records -- which is the whole point: a key added here reaches both, and
    cannot be validated in one place and forgotten in the other.
    """

    name: str
    type: str = "str"  # str | int | float | bool | path | choice
    default: object = None
    section: str = ""  # "" for a top-level key, "jira" for [jira]
    step: str = "advanced"  # which wizard screen this key appears on
    label: str = ""
    help: str = ""
    secret: bool = False
    choices: tuple[str, ...] = ()
    required: bool = False
    required_if: Condition | None = None
    required_error: str = ""
    check: Validator | None = None

    @property
    def key(self) -> str:
        return f"{self.section}.{self.name}" if self.section else self.name


def is_url(value: object) -> bool:
    """Whether `repo` names a remote to clone rather than a checkout on disk.

    Both forms git itself accepts: a scheme (`https://`, `ssh://`, `file://`)
    and the scp-like shorthand (`git@github.com:owner/repo.git`).
    """
    text = str(value)
    return "://" in text or re.match(r"^[^/\s]+@[^/\s:]+:", text) is not None


def repo_path(value: object, home: Path = HOME) -> Path:
    """Where the repository lives on this box: the path itself, or the
    directory a remote is cloned into.

    The clone directory is named after the URL and keyed by a hash of it.
    Basenames collide -- `github.com/us/api` and `github.com/them/api` are
    different projects -- and an unattended loop silently running tasks
    against the wrong checkout is the worst way to find that out.
    """
    if not is_url(value):
        return Path(str(value)).expanduser()
    url = str(value).rstrip("/")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.rsplit("/", 1)[-1].removesuffix(".git"))
    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    return home / "clones" / f"{name or 'repo'}-{digest}"


def _coerce(field: Field, value: object) -> object:
    """Raises ValueError with a message written for a human."""
    if field.type == "repo":
        # A URL is kept verbatim, not turned into its clone path: the wizard
        # writes coerced values back to config.toml, and a config that had
        # lost the URL could never re-clone.
        return str(value) if is_url(value) else Path(str(value)).expanduser()
    if field.type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    if field.type == "path":
        return Path(str(value)).expanduser()
    if field.type == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field.key} must be a whole number, not {value!r}")
    if field.type == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field.key} must be a number, not {value!r}")
    return str(value)


_BLANK = object()
"""Sentinel for _raw: a key present but whitespace-only, distinct from a key
truly absent. An empty string "" is what the wizard posts for every field the
operator left alone, so it must still mean absent -- but "   " is a typo, not
a blank submission, and silently vanishing it undercuts _must_exist's whole
point (see validate())."""


def _raw(data: dict, field: Field) -> object:
    """The submitted value, `None` when the key is absent, or `_BLANK` when
    it is present but whitespace-only.

    A genuinely empty string counts as absent. The wizard posts every field
    it renders, so an untouched optional key arrives as "" and must fall
    back to its default rather than becoming an empty path or an empty model
    name.
    """
    table = data.get(field.section) if field.section else data
    if not isinstance(table, dict):
        return None
    value = table.get(field.name)
    if value is None:
        return None
    if isinstance(value, str):
        if value == "":
            return None
        if not value.strip():
            return _BLANK
        return value.strip()
    return value


# --- the checks and conditions the table refers to ------------------------

def _is_git_repo(value, values) -> str | None:
    if is_url(value):
        # Nothing to check without the network, and this runs in the wizard
        # too. `worktree.clone` at startup reports a bad URL, once, before
        # anything is listening.
        return None
    if not (Path(value) / ".git").exists():
        return f"repo {value} is not a git repository"
    return None


def _outside_repo(value, values) -> str | None:
    """No trace of ClaudeLoop belongs in a repository it works in: a session
    doing ordinary branch hygiene (`git add -A`, `git checkout -- .`, `git
    stash`) would otherwise revert ClaudeLoop's own `- [x]` mark, and the loop
    would re-run work it had already finished, unattended and unbounded.

    Resolved so `..` segments and symlinks cannot sneak a path past this,
    while the unresolved path is what gets stored, matching repo itself.
    """
    repo = values.get("repo")
    if repo is None:
        return None  # repo already has its own error; do not pile a second on
    if Path(value).resolve().is_relative_to(repo_path(repo).resolve()):
        return (
            f"tasks_file {value} is inside repo {repo}. ClaudeLoop's task list"
            " must live outside the repository it works in."
        )
    return None


def _tasks_file(value, values) -> str | None:
    """Both of tasks_file's rules, in the order a bad path should hear them."""
    return _outside_repo(value, values) or _writable_tasks_file(value, values)


def _writable_tasks_file(value, values) -> str | None:
    """A tasks file this process cannot write is an unbounded, paid loop.

    `FileSource._rewrite` suppresses the OSError deliberately -- the file is
    the operator's and may vanish mid-run -- so a failed mark is silent, and
    the task stays `- [ ]`, and it is picked up again on the next poll, and
    paid for again, forever. Measured in S4's live smoke test: 37 runs of one
    task in fifteen minutes, $1.10, before it was killed by hand.

    Reachable the moment the loop and the file have different owners, which is
    ordinary in the add-on: the loop runs unprivileged and /share is root's.
    """
    if Path(value).exists() and not os.access(value, os.W_OK):
        # A file that is not there yet is left alone: pending() reads a
        # missing checklist as an empty backlog, so there is nothing to mark
        # and nothing to loop on.
        return (
            f"tasks_file {value} is not writable. ClaudeLoop marks each task"
            " in this file as it finishes; a task it cannot mark is offered"
            " again on the next poll and paid for again."
        )
    return None


def _ascii_token(value, values) -> str | None:
    if not str(value).isascii():
        return (
            "web_token must be ASCII -- secrets.compare_digest, used to check"
            " it on every request, raises TypeError on anything else."
        )
    return None


def _https_site(value, values) -> str | None:
    if values.get("source") != "jira":
        # An unused [jira] table -- source = "file", or a value left over
        # from switching away from source = "jira" -- must not block a
        # config that never reads it.
        return None
    if not str(value).startswith("https://"):
        return (
            f"[jira] site {str(value)!r} must start with https:// -- urllib"
            " forwards the Authorization header across a redirect, so an"
            " http:// site puts the Basic-auth API token on the wire in"
            " cleartext the first time Jira redirects it."
        )
    return None


def _must_exist(label: str) -> Validator:
    """Checked at load, not at first use: unchecked, a typo'd path makes
    `claude` exit immediately on every task, and main_loop deliberately does
    not source.mark on that kind of crash -- so the loop would retry every
    30s forever with the dashboard stuck in 'error'."""

    def check(value, values) -> str | None:
        return None if Path(value).exists() else f"{label} {value} does not exist"

    return check


def _strict_mcp_needs_config(value, values) -> str | None:
    if value and not values.get("mcp_config"):
        return (
            "strict_mcp is set but mcp_config is not. On its own,"
            " --strict-mcp-config tells the CLI to use only the servers from"
            " --mcp-config — of which there would be none — silently disabling"
            " every MCP server this machine has configured."
        )
    return None


def _file_source(values) -> bool:
    return values.get("source") == "file"


def _jira_source(values) -> bool:
    return values.get("source") == "jira"


def _jira_without_jql(values) -> bool:
    return _jira_source(values) and not values.get("jira.jql")


def _exposed(values) -> bool:
    return values.get("web_host") not in LOOPBACK_HOSTS


SCHEMA: tuple[Field, ...] = (
    Field("repo", "repo", step="repository", required=True,
          check=_is_git_repo, label="Repository",
          help="The git repository ClaudeLoop works in: a local path, or a URL"
               " (https://, ssh://, git@host:owner/repo.git) which is cloned"
               " once into ~/.claudeloop/clones/ and worked in from there."
               " Each task gets its own worktree cut from the default branch;"
               " your own checkout is never moved."),
    Field("model", step="repository", default="opus",
          label="Model",
          help="Which Claude model each session runs on, e.g. opus, sonnet,"
               " haiku."),
    Field("source", "choice", step="source", default="file",
          choices=SOURCES, label="Task source",
          help="Where the backlog comes from: a markdown checklist (file) or a"
               " Jira Cloud project (jira)."),
    Field("tasks_file", "path", step="source", required_if=_file_source,
          required_error='source = "file" requires tasks_file',
          check=_tasks_file, label="Tasks file",
          help="A markdown checklist, one task per `- [ ]` line. It must live"
               " outside the repository, or a session's branch hygiene can"
               " revert ClaudeLoop's own completion marks."),
    Field("site", step="source", section="jira", required_if=_jira_source,
          required_error='[jira] is missing required key: site',
          check=_https_site, label="Jira site",
          help="Your Jira Cloud URL, e.g. https://yourcompany.atlassian.net —"
               " no /jira suffix."),
    Field("email", step="source", section="jira", required_if=_jira_source,
          required_error="[jira] is missing required key: email",
          label="Jira account email",
          help="The account the API token belongs to."),
    Field("token", step="source", section="jira", secret=True,
          required_if=_jira_source,
          required_error="[jira] is missing required key: token",
          label="Jira API token",
          help="id.atlassian.com → Security → API tokens."),
    Field("jql", step="source", section="jira", label="JQL (advanced)",
          help="A query of your own, which wins over project and status. Use"
               " it for anything the two cannot express — an assignee, a label"
               " filter, a priority ordering."),
    Field("project", step="source", section="jira",
          required_if=_jira_without_jql,
          required_error='[jira] needs either jql, or project (with an optional'
                         ' status) for ClaudeLoop to compose one, e.g.'
                         ' project = "OPS"',
          label="Project key",
          help="Which project to take work from, e.g. OPS."),
    Field("status", step="source", section="jira", label="Status",
          help="Optional. The exact status name on your board, e.g. To Do."),
    Field("transition_start", step="source", section="jira",
          label="Transition on start",
          help="Optional. Moved here when a task starts, if the workflow offers"
               " that transition from where the issue sits."),
    Field("transition_done", step="source", section="jira",
          label="Transition on finish",
          help="Optional. Moved here when a task ends, the same way."),
    Field("web_host", step="dashboard", default="127.0.0.1",
          label="Dashboard host",
          help="127.0.0.1 keeps the dashboard on this machine. Anything else"
               " exposes an agent holding real credentials, and requires a"
               " token."),
    Field("web_port", "int", step="dashboard", default=8765,
          label="Dashboard port", help="Default 8765."),
    Field("web_token", step="dashboard", secret=True, default="",
          required_if=_exposed, check=_ascii_token,
          required_error="web_host is not loopback, so web_token must be set to"
                         " a non-empty value. The dashboard watches an agent"
                         " holding real credentials; exposing it beyond this"
                         " machine has to be a deliberate act.",
          label="Dashboard token",
          help="Required unless the dashboard is on loopback. Every request"
               " must then carry ?token=…"),
    Field("instructions_file", "path", step="instructions",
          label="Operator instructions",
          help="Your own instructions, added to every session's prompt above"
               " the definition of done. Defaults to"
               " ~/.claudeloop/instructions.md; absent when the file is."),
    Field("definition_of_done_file", "path", step="instructions",
          label="Definition of done",
          help="Overrides the built-in definition of done. The target"
               " repository's own CLAUDE.md still wins over both. Defaults to"
               " ~/.claudeloop/definition-of-done.md."),
    Field("max_resumes", "int", default=20, label="Max resumes",
          help="How many plain nudges one task may take before it is given"
               " up on."),
    Field("max_waits", "int", default=200, label="Max quota waits",
          help="How many rate-limit sleeps one task may take. Counted"
               " separately from resumes, because a quota wait is not the"
               " session's fault."),
    Field("session_timeout_s", "float", default=4 * 3600,
          label="Session timeout (seconds)",
          help="Kills a wedged session. Default 14400, four hours."),
    Field("settings_file", "path", check=_must_exist("settings_file"),
          label="Settings file", help="Passed to the CLI as --settings."),
    Field("mcp_config", "path", check=_must_exist("mcp_config"),
          label="MCP config", help="Passed to the CLI as --mcp-config."),
    Field("strict_mcp", "bool", default=False, check=_strict_mcp_needs_config,
          label="Strict MCP",
          help="Adds --strict-mcp-config, so only the servers in the MCP config"
               " are used. Requires an MCP config."),
)
"""Order is load-bearing: a field's required_if and check see only the values
of fields declared before it. tasks_file's check reads repo, web_token's
condition reads web_host, jira.project's reads jira.jql, strict_mcp's check
reads mcp_config."""


def validate(data: dict) -> tuple[dict, list[tuple[str, str]]]:
    """Walk SCHEMA over `data`. Coerced values, and every error found.

    Every error, not the first: the wizard marks up a whole form in one
    round trip. load_config raises on errors[0] and so behaves as it always
    did from the command line.
    """
    values: dict = {}
    errors: list[tuple[str, str]] = []
    for field in SCHEMA:
        # Coerced once so an absent/invalid field lands on the same type its
        # Config field declares -- session_timeout_s: float defaults to
        # 4 * 3600, a bare int, without this.
        fallback = _coerce(field, field.default) if field.default is not None else None
        raw = _raw(data, field)
        if raw is _BLANK:
            if field.required or (field.required_if and field.required_if(values)):
                # A blank required field is not fixed by removing the key --
                # that only turns this into the "missing" error below on the
                # next validate(), which is why the message has to be the
                # same one absence gets, not "remove the key".
                errors.append(
                    (field.key, field.required_error or f"{field.key} is required")
                )
            else:
                errors.append(
                    (field.key, f"{field.key} is blank -- remove the key or give it a value")
                )
            values[field.key] = fallback
            continue
        if raw is None:
            if field.required or (field.required_if and field.required_if(values)):
                errors.append(
                    (field.key, field.required_error or f"{field.key} is required")
                )
            values[field.key] = fallback
            continue
        try:
            value = _coerce(field, raw)
        except ValueError as error:
            errors.append((field.key, str(error)))
            values[field.key] = fallback
            continue
        if field.choices and value not in field.choices:
            errors.append((
                field.key,
                f"{field.key} {value!r} is not one of {', '.join(field.choices)}",
            ))
            values[field.key] = fallback
            continue
        values[field.key] = value
        if field.check:
            message = field.check(value, values)
            if message:
                errors.append((field.key, message))
    try:
        values["session_env"] = _session_env(data)
    except ValueError as error:
        errors.append(("session_env", str(error)))
        values["session_env"] = {}
    return values, errors


def _compose_jql(project: str, status: str) -> str:
    """The query composed from the project/status shorthand.

    Writing JQL by hand to start is a barrier, and getting it subtly wrong
    yields a silently empty backlog with nothing saying why. An explicit jql
    still wins: the shorthand cannot express an assignee, a label filter or a
    priority ordering.
    """
    where = f'project = "{project}"'
    if status:
        where += f' AND status = "{status}"'
    # Jira refuses an unbounded JQL outright, so `where` always carries a
    # restriction -- confirmed against a live instance.
    return f"{where} {DEFAULT_ORDER}"


def _session_env(data: dict) -> dict[str, str]:
    table = data.get("session_env", {})
    if not isinstance(table, dict):
        raise ValueError("session_env must be a table of name = \"value\"")
    result = {}
    for name, value in table.items():
        if isinstance(value, (dict, list)):
            raise ValueError(
                f"session_env.{name} must be a single value, not a table or"
                " array — environment variables are strings"
            )
        result[str(name)] = str(value)
    return result


@dataclass(frozen=True)
class JiraConfig:
    site: str
    email: str
    token: str
    jql: str
    transition_start: str = ""
    transition_done: str = ""


@dataclass(frozen=True)
class Config:
    repo: Path
    """Always local: the clone directory when `repo_url` is set."""
    repo_url: str = ""
    """The remote to clone, when config.toml gave a URL rather than a path."""
    tasks_file: Path | None = None
    model: str = "opus"
    max_resumes: int = 20
    max_waits: int = 200
    session_timeout_s: float = 4 * 3600
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_token: str = ""
    instructions_file: Path | None = None
    definition_of_done_file: Path | None = None
    settings_file: Path | None = None
    mcp_config: Path | None = None
    strict_mcp: bool = False
    session_env: dict[str, str] = field(default_factory=dict)
    home: Path = HOME
    source: str = "file"
    jira: JiraConfig | None = None


def load_config(path: Path = DEFAULT_CONFIG, home: Path = HOME) -> Config:
    """Read `path` into a Config.

    The config file is user input, so every key is validated here rather than
    failing much later inside a subprocess. The rules themselves live in
    SCHEMA, which the setup wizard renders the same form from.
    """
    with open(path, "rb") as handle:
        data = tomllib.load(handle)

    _secrets_file_guard(path)

    values, errors = validate(data)
    if errors:
        # The first, not all of them: this is a command-line caller, and one
        # actionable sentence beats a wall. The wizard shows the whole list.
        # But a bare errors[0] hid the rest entirely -- an operator fixing a
        # [jira] table missing three keys would fix one, re-run, and only
        # then discover the next. Naming the remaining keys costs one clause
        # and saves that many re-runs.
        message = f"{path}: {errors[0][1]}"
        if len(errors) > 1:
            rest = len(errors) - 1
            others = ", ".join(key for key, _ in errors[1:])
            message += f" (and {rest} more problem{'s' if rest != 1 else ''}: {others})"
        raise ValueError(message)

    jira = None
    if values["source"] == "jira":
        jira = JiraConfig(
            site=values["jira.site"],
            email=values["jira.email"],
            token=values["jira.token"],
            jql=values["jira.jql"]
            or _compose_jql(values["jira.project"], values["jira.status"] or ""),
            transition_start=values["jira.transition_start"] or "",
            transition_done=values["jira.transition_done"] or "",
        )

    return Config(
        repo=repo_path(values["repo"], home),
        repo_url=values["repo"] if is_url(values["repo"]) else "",
        tasks_file=values["tasks_file"],
        model=values["model"],
        max_resumes=values["max_resumes"],
        max_waits=values["max_waits"],
        session_timeout_s=values["session_timeout_s"],
        web_host=values["web_host"],
        web_port=values["web_port"],
        web_token=values["web_token"],
        # Not a Field default: these two fall back to a path under `home`,
        # which is a parameter of this function and not a config key.
        instructions_file=values["instructions_file"] or home / "instructions.md",
        definition_of_done_file=values["definition_of_done_file"]
        or home / "definition-of-done.md",
        settings_file=values["settings_file"],
        mcp_config=values["mcp_config"],
        strict_mcp=values["strict_mcp"],
        session_env=values["session_env"],
        home=home,
        source=values["source"],
        jira=jira,
    )
