"""Load and validate the ClaudeLoop configuration file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home() / ".claudeloop"
DEFAULT_CONFIG = HOME / "config.toml"
REQUIRED_KEYS = ("repo",)
SOURCES = ("file", "jira")
JIRA_KEYS = ("site", "email", "token")
DEFAULT_ORDER = "ORDER BY created ASC"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
WILDCARD_HOSTS = ("0.0.0.0", "::")


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


def _coerce(field: Field, value: object) -> object:
    """Raises ValueError with a message written for a human."""
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


def _raw(data: dict, field: Field) -> object | None:
    """The submitted value, or None when the key is absent.

    A blank string counts as absent. The wizard posts every field it renders,
    so an untouched optional key arrives as "" and must fall back to its
    default rather than becoming an empty path or an empty model name.
    """
    table = data.get(field.section) if field.section else data
    if not isinstance(table, dict):
        return None
    value = table.get(field.name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value.strip() if isinstance(value, str) else value


# --- the checks and conditions the table refers to ------------------------

def _is_git_repo(value, values) -> str | None:
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
    if Path(value).resolve().is_relative_to(Path(repo).resolve()):
        return (
            f"tasks_file {value} is inside repo {repo}. ClaudeLoop's task list"
            " must live outside the repository it works in."
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
    Field("repo", "path", step="repository", required=True,
          check=_is_git_repo, label="Repository",
          help="The git repository ClaudeLoop works in. Each task gets its own"
               " worktree cut from this repository's default branch; your own"
               " checkout is never moved."),
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
          check=_outside_repo, label="Tasks file",
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
        raw = _raw(data, field)
        if raw is None:
            if field.required or (field.required_if and field.required_if(values)):
                errors.append(
                    (field.key, field.required_error or f"{field.key} is required")
                )
            values[field.key] = field.default
            continue
        try:
            value = _coerce(field, raw)
        except ValueError as error:
            errors.append((field.key, str(error)))
            values[field.key] = field.default
            continue
        if field.choices and value not in field.choices:
            errors.append((
                field.key,
                f"{field.key} {value!r} is not one of {', '.join(field.choices)}",
            ))
            values[field.key] = field.default
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


def _optional_path(data: dict, key: str) -> Path | None:
    raw = data.get(key)
    return Path(str(raw)).expanduser() if raw else None


@dataclass(frozen=True)
class JiraConfig:
    site: str
    email: str
    token: str
    jql: str
    transition_start: str = ""
    transition_done: str = ""


def _jql(table: dict, path: Path) -> str:
    """The operator's query, or one composed from project and status.

    Writing JQL by hand to start is a barrier, and getting it subtly wrong
    yields a silently empty backlog with nothing saying why. An explicit jql
    still wins: the shorthand cannot express an assignee, a label filter or a
    priority ordering.
    """
    jql = str(table.get("jql", "")).strip()
    if jql:
        return jql
    project = str(table.get("project", "")).strip()
    if not project:
        raise ValueError(
            f"{path}: [jira] needs either jql, or project (with an optional"
            ' status) for ClaudeLoop to compose one, e.g. project = "OPS"'
        )
    status = str(table.get("status", "")).strip()
    where = f'project = "{project}"'
    if status:
        where += f' AND status = "{status}"'
    # Jira refuses an unbounded JQL outright, so `where` always carries a
    # restriction -- confirmed against a live instance.
    return f"{where} {DEFAULT_ORDER}"


def _jira(data: dict, path: Path) -> JiraConfig:
    """Validated at load, not at first poll: a missing token would otherwise
    surface as a 401 on every 30-second poll forever, with the dashboard
    showing an empty backlog and nothing saying why."""
    table = data.get("jira")
    if not isinstance(table, dict):
        raise ValueError(
            f'{path}: source = "jira" needs a [jira] table with '
            f"{', '.join(JIRA_KEYS)}, and either jql or project"
        )
    missing = [key for key in JIRA_KEYS if not str(table.get(key, "")).strip()]
    if missing:
        raise ValueError(
            f"{path}: [jira] is missing required key(s): {', '.join(missing)}"
        )
    site = str(table["site"])
    if not site.startswith("https://"):
        raise ValueError(
            f"{path}: [jira] site {site!r} must start with https:// -- urllib"
            " forwards the Authorization header across a redirect, so an"
            " http:// site puts the Basic-auth API token on the wire in"
            " cleartext the first time Jira redirects it."
        )
    return JiraConfig(
        site=site,
        email=str(table["email"]),
        token=str(table["token"]),
        jql=_jql(table, path),
        transition_start=str(table.get("transition_start", "")),
        transition_done=str(table.get("transition_done", "")),
    )


@dataclass(frozen=True)
class Config:
    repo: Path
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

    The config file is user input, so both the required keys and the repo path
    are validated here rather than failing much later inside a subprocess.
    """
    with open(path, "rb") as handle:
        data = tomllib.load(handle)

    _secrets_file_guard(path)

    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise ValueError(f"{path}: missing required key(s): {', '.join(missing)}")

    repo = Path(data["repo"]).expanduser()
    if not (repo / ".git").exists():
        raise ValueError(f"{path}: repo {repo} is not a git repository")

    source = str(data.get("source", "file"))
    if source not in SOURCES:
        raise ValueError(
            f"{path}: source {source!r} is not one of {', '.join(SOURCES)}"
        )
    if source == "file" and "tasks_file" not in data:
        raise ValueError(f'{path}: source = "file" requires tasks_file')

    tasks_file = Path(str(data["tasks_file"])).expanduser() if "tasks_file" in data else None
    # Resolved so `..` segments and symlinks can't sneak a tasks_file that
    # lands inside repo past this -- but the unresolved path is still what
    # gets stored on Config below, matching repo itself. No trace of
    # ClaudeLoop belongs in a repository it works in, the same reason
    # result.json/events.jsonl/state.db all live under ~/.claudeloop/
    # instead: a session doing ordinary branch hygiene (`git checkout .`,
    # `git stash`, `git checkout main`) can revert ClaudeLoop's own `- [x]`
    # mark, and the loop then re-runs work it already finished. This still
    # applies under source = "jira" whenever tasks_file happens to be set.
    if tasks_file is not None and tasks_file.resolve().is_relative_to(repo.resolve()):
        raise ValueError(
            f"{path}: tasks_file {tasks_file} is inside repo {repo}. "
            "ClaudeLoop's task list must live outside the repository it "
            "works in."
        )

    jira = _jira(data, path) if source == "jira" else None

    web_host = str(data.get("web_host", "127.0.0.1"))
    web_token = str(data.get("web_token", "")).strip()
    if not web_token.isascii():
        raise ValueError(
            f"{path}: web_token must be ASCII -- secrets.compare_digest, used to"
            " check it on every request, raises TypeError on anything else."
        )
    if web_host not in LOOPBACK_HOSTS and not web_token:
        raise ValueError(
            f"{path}: web_host {web_host!r} is not loopback, so web_token must be"
            " set to a non-empty value. The dashboard watches an agent holding"
            " real credentials; exposing it beyond this machine has to be a"
            " deliberate act."
        )

    # Checked here, same as repo above, so a typo'd path fails loudly at
    # startup: unchecked, it makes `claude` exit immediately on every task,
    # and main_loop deliberately does not source.mark on that kind of crash,
    # so the loop would retry every 30s forever with the dashboard stuck in
    # 'error'. Unlike instructions_file/definition_of_done_file, these two
    # are never optional once named -- a missing one is a config mistake,
    # not an absent optional layer.
    settings_file = _optional_path(data, "settings_file")
    if settings_file is not None and not settings_file.exists():
        raise ValueError(f"{path}: settings_file {settings_file} does not exist")

    mcp_config = _optional_path(data, "mcp_config")
    if mcp_config is not None and not mcp_config.exists():
        raise ValueError(f"{path}: mcp_config {mcp_config} does not exist")

    strict_mcp = bool(data.get("strict_mcp", False))
    if strict_mcp and mcp_config is None:
        raise ValueError(
            f"{path}: strict_mcp is set but mcp_config is not. On its own,"
            " --strict-mcp-config tells the CLI to use only the servers from"
            " --mcp-config — of which there would be none — silently disabling"
            " every MCP server this machine has configured."
        )

    return Config(
        repo=repo,
        tasks_file=tasks_file,
        model=str(data.get("model", "opus")),
        max_resumes=int(data.get("max_resumes", 20)),
        max_waits=int(data.get("max_waits", 200)),
        session_timeout_s=float(data.get("session_timeout_s", 4 * 3600)),
        web_host=web_host,
        web_port=int(data.get("web_port", 8765)),
        web_token=web_token,
        instructions_file=_optional_path(data, "instructions_file")
        or home / "instructions.md",
        definition_of_done_file=_optional_path(data, "definition_of_done_file")
        or home / "definition-of-done.md",
        settings_file=settings_file,
        mcp_config=mcp_config,
        strict_mcp=strict_mcp,
        session_env=_session_env(data),
        home=home,
        source=source,
        jira=jira,
    )
