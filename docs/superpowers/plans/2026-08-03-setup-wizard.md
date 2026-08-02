# Setup Wizard and Config Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `config.py`'s hand-written validation with one declarative
field table, and serve a browser wizard rendered from that same table so an
operator never has to hand-edit TOML.

**Architecture:** A tuple of frozen `Field` records in `config.py` carries every
key's type, default, help text and conditions. `validate(data)` walks it once
and returns coerced values plus every error; `load_config` raises on the first
error, the wizard renders them all. A new `setup.py` serves a loopback-only
server guarded by a one-time console token, subclassing `web.Handler` so the
request-smuggling fix is inherited. It blocks until it has written a valid
`config.toml`, then `main()` falls through into the ordinary startup path.

**Tech Stack:** Python 3.11 standard library only — `tomllib`, `http.server`,
`argparse`, `subprocess`, `urllib`. One new no-build HTML file.

**Spec:** `docs/superpowers/specs/2026-08-03-claudeloop-setup-wizard-design.md`

## Global Constraints

- **Python 3.11+, standard library only.** No third-party packages, in the
  orchestrator, the tests or the frontend. `pip install` and `npm install` must
  both remain unnecessary.
- **No build step.** The wizard is one HTML file with inline CSS and an inline
  module script, making no off-origin requests.
- **The web layer never touches the loop's objects.** Setup mode runs only when
  the loop does not, so there is no shared state at all — do not add any.
- **Any route that returns early without draining the request body must close
  its connection.** `do_POST` sets `self.close_connection = True` before
  anything else. This is inherited from `web.Handler`; do not re-implement
  `do_POST` from scratch in a way that loses it.
- **Nothing ClaudeLoop writes may land inside the target repository.**
- **Strictly serial**, one task at a time — unchanged by this slice.
- Tests use stdlib `unittest`, real files on disk, and fake external programs
  rather than mock libraries. A test that spawns `git` in a scratch repository
  must set `commit.gpgsign false` locally on it.
- Run the whole suite with `python -m unittest discover -s tests -t .`
  (~75s). One module: `python -m unittest tests.test_config -v`.
- Commit after every task. Do not push; this repository has no usable remote.

---

### Task 1: The field table and `validate()`

Adds the schema and the validation walk to `config.py`. Nothing consumes it yet
— `load_config` is left exactly as it is, so this task cannot break a running
install and the existing suite is untouched.

**Files:**
- Modify: `claudeloop/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Field` — frozen dataclass with `.key` property.
  - `SCHEMA: tuple[Field, ...]` — every `config.toml` key, in an order where
    each field's conditions see only fields declared before it.
  - `validate(data: dict) -> tuple[dict, list[tuple[str, str]]]` — coerced
    values keyed by `Field.key`, and `(key, message)` for every error found.
  - `_compose_jql(project: str, status: str) -> str`.
  - `_session_env(data: dict) -> dict[str, str]` — signature changes, losing its
    `path` argument.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
from claudeloop.config import SCHEMA, Field, validate


class SchemaTest(unittest.TestCase):
    """The table is the single source of truth for both load_config and the
    setup wizard. These pin the walk itself; the ConfigTest cases above pin
    what load_config does with it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def minimal(self, **extra) -> dict:
        return {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md", **extra}

    def test_a_valid_config_produces_no_errors_and_coerced_values(self):
        values, errors = validate(self.minimal(max_resumes="7", strict_mcp=False))
        self.assertEqual(errors, [])
        self.assertEqual(values["repo"], self.repo)
        self.assertIsInstance(values["repo"], Path)
        self.assertEqual(values["max_resumes"], 7)
        self.assertEqual(values["model"], "opus")  # the default landed

    def test_every_error_is_collected_not_just_the_first(self):
        # The wizard marks up a whole form at once; raising on the first
        # would make it a one-error-per-round-trip guessing game.
        values, errors = validate({
            "repo": str(self.tmp / "nope"),
            "source": "jira",
            "web_host": "0.0.0.0",
        })
        keys = [key for key, _ in errors]
        self.assertIn("repo", keys)
        self.assertIn("web_token", keys)
        self.assertIn("jira.site", keys)
        self.assertGreaterEqual(len(errors), 3)

    def test_errors_are_keyed_by_field_key(self):
        _, errors = validate(self.minimal(source="jira"))
        keys = dict(errors)
        self.assertIn("jira.site", keys)
        self.assertIn("[jira]", keys["jira.site"])

    def test_a_non_numeric_number_is_an_error_not_a_crash(self):
        _, errors = validate(self.minimal(max_waits="soon"))
        self.assertEqual([key for key, _ in errors], ["max_waits"])

    def test_a_bad_choice_names_the_alternatives(self):
        _, errors = validate(self.minimal(source="carrier pigeon"))
        key, message = errors[0]
        self.assertEqual(key, "source")
        self.assertIn("file", message)
        self.assertIn("jira", message)

    def test_an_empty_string_counts_as_absent(self):
        # The wizard submits "" for every field the operator left alone.
        values, errors = validate(self.minimal(model="", settings_file=""))
        self.assertEqual(errors, [])
        self.assertEqual(values["model"], "opus")
        self.assertIsNone(values["settings_file"])

    def test_a_false_boolean_is_not_absent(self):
        values, errors = validate(self.minimal(strict_mcp=False))
        self.assertEqual(errors, [])
        self.assertIs(values["strict_mcp"], False)

    def test_jira_needs_project_or_jql_and_says_so(self):
        _, errors = validate({"repo": str(self.repo), "source": "jira",
                              "jira": {"site": "https://x.atlassian.net",
                                       "email": "a@b.c", "token": "t"}})
        message = dict(errors)["jira.project"]
        self.assertIn("jql", message)
        self.assertIn("project", message)

    def test_an_explicit_jql_removes_the_project_requirement(self):
        _, errors = validate({"repo": str(self.repo), "source": "jira",
                              "jira": {"site": "https://x.atlassian.net",
                                       "email": "a@b.c", "token": "t",
                                       "jql": "project = OPS"}})
        self.assertEqual(errors, [])

    def test_a_condition_only_sees_fields_declared_before_it(self):
        # SCHEMA order is load-bearing: web_token's required_if reads
        # web_host, tasks_file's check reads repo, strict_mcp's check reads
        # mcp_config. Anything that reorders the table must fail here.
        order = [field.key for field in SCHEMA]
        for earlier, later in (("repo", "tasks_file"), ("source", "tasks_file"),
                               ("web_host", "web_token"), ("jira.jql", "jira.project"),
                               ("mcp_config", "strict_mcp")):
            self.assertLess(order.index(earlier), order.index(later),
                            f"{earlier} must be declared before {later}")

    def test_every_field_carries_help_text_for_the_wizard(self):
        for field in SCHEMA:
            self.assertTrue(field.help.strip(), f"{field.key} has no help text")
            self.assertTrue(field.label.strip(), f"{field.key} has no label")

    def test_the_table_and_the_Config_dataclass_agree(self):
        # Not a bijection, and the exceptions are named rather than left to
        # be rediscovered: jira.project and jira.status are composed away
        # into jira.jql by _compose_jql and never reach Config, and `home`
        # is a load_config parameter that is never a config key.
        composed_away = {"jira.project", "jira.status"}
        not_a_config_key = {"home", "jira"}
        top_level = {f.name for f in SCHEMA if not f.section}
        jira_keys = {f.name for f in SCHEMA if f.section == "jira"}
        self.assertEqual(
            top_level | {"session_env"},
            set(Config.__dataclass_fields__) - not_a_config_key,
        )
        self.assertEqual(
            {f"jira.{name}" for name in jira_keys} - composed_away,
            {f"jira.{name}" for name in JiraConfig.__dataclass_fields__},
        )

    def test_secret_fields_are_marked(self):
        secret = {field.key for field in SCHEMA if field.secret}
        self.assertEqual(secret, {"web_token", "jira.token"})
```

Add `JiraConfig` to the module's imports at the top of the file:

```python
from claudeloop.config import SCHEMA, Config, Field, JiraConfig, load_config, validate
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_config.SchemaTest -v`
Expected: FAIL with `ImportError: cannot import name 'SCHEMA'`.

- [ ] **Step 3: Implement the table and the walk**

In `claudeloop/config.py`, add after the existing constants and before
`JiraConfig`:

```python
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
```

Change `_session_env` to drop its `path` argument, since `validate` has no path
to report and `load_config` adds the prefix itself:

```python
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
```

Update its one existing caller in `load_config` to `_session_env(data)` so the
module still imports; `load_config` is otherwise untouched in this task, and
its message loses the `{path}: ` prefix only for that one case — Task 2 puts it
back for every message at once.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_config -v`
Expected: PASS, both `SchemaTest` and the existing `ConfigTest`.

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/config.py tests/test_config.py
git commit -m "feat: a declarative field table and a validate() walk over it"
```

---

### Task 2: `load_config` walks the table

Deletes the hand-written validation and rebuilds `Config` from `validate()`'s
coerced values. The existing `ConfigTest` cases are the regression gate: they
were written against the old code and must pass unchanged.

**Files:**
- Modify: `claudeloop/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `SCHEMA`, `validate`, `_compose_jql` from Task 1.
- Produces: `load_config` unchanged in signature and in every message it
  raises. `_jql` and `_jira` are deleted.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`, inside `ConfigTest`:

```python
    def test_load_config_reports_the_first_error_with_the_file_path(self):
        # Every message a human sees names the file it came from -- the
        # common install failure is a config.toml at the default umask, and
        # that operator must get the message, not a traceback.
        path = self.write(
            f'repo = "{self.tmp}/nope"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
        )
        with self.assertRaises(ValueError) as caught:
            load_config(path, home=self.tmp / "home")
        self.assertIn(str(path), str(caught.exception))

    def test_the_jira_shorthand_still_composes_a_query(self):
        path = self.write(
            f'repo = "{self.repo}"\n'
            'source = "jira"\n'
            "[jira]\n"
            'site = "https://x.atlassian.net"\n'
            'email = "a@b.c"\n'
            'token = "t"\n'
            'project = "OPS"\n'
            'status = "To Do"\n'
        )
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.jira.jql, 'project = "OPS" AND status = "To Do" ORDER BY created ASC')
```

- [ ] **Step 2: Run to verify the first one fails**

Run: `python -m unittest tests.test_config.ConfigTest -v`
Expected: FAIL on `test_load_config_reports_the_first_error_with_the_file_path`
— today's message for a bad repo already carries the path, so if it passes
immediately, confirm by checking the message text and move on. The second test
should pass on the old code too; it is there to pin the behaviour across the
rewrite.

- [ ] **Step 3: Rewrite `load_config`**

Replace the body of `load_config` (keeping its signature and docstring) with:

```python
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
        raise ValueError(f"{path}: {errors[0][1]}")

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
        repo=values["repo"],
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
```

Delete `_jql`, `_jira`, `_optional_path`, `REQUIRED_KEYS` and `JIRA_KEYS` — every
one of them is now table data. Keep `SOURCES`, `DEFAULT_ORDER`,
`LOOPBACK_HOSTS`, `WILDCARD_HOSTS` and `_secrets_file_guard`.

- [ ] **Step 4: Run the config tests**

Run: `python -m unittest tests.test_config -v`
Expected: PASS. Every existing `ConfigTest` case must pass without being
edited — if one needs editing, the rewrite changed behaviour and that is a bug
in this task, not in the test.

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS. `test_jira.py` and `test_loop.py` both build configs.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/config.py tests/test_config.py
git commit -m "refactor: load_config validates through the schema table"
```

---

### Task 3: Writing TOML

The stdlib reads TOML and does not write it, and no package may be added — so
the emitter is ours. Schema-driven, so key order and the `#` comments come from
the same table the wizard renders.

**Files:**
- Create: `claudeloop/setup.py`
- Test: `tests/test_setup.py` (create)

**Interfaces:**
- Consumes: `SCHEMA`, `validate` from Task 1.
- Produces: `setup.dump_toml(data: dict) -> str`, taking the `config.toml`
  shape — top-level keys, plus `"jira"` and `"session_env"` tables — and
  returning the file's text.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_setup.py`:

```python
import tempfile
import tomllib
import unittest
from pathlib import Path

from claudeloop import setup
from claudeloop.config import load_config


class DumpTomlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def roundtrip(self, data: dict) -> dict:
        text = setup.dump_toml(data)
        return tomllib.loads(text)

    def test_a_minimal_config_round_trips(self):
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md"}
        self.assertEqual(self.roundtrip(data), data)

    def test_types_survive_the_trip(self):
        data = {
            "repo": str(self.repo),
            "tasks_file": f"{self.tmp}/tasks.md",
            "web_port": 8765,
            "session_timeout_s": 14400.0,
            "strict_mcp": False,
        }
        back = self.roundtrip(data)
        self.assertEqual(back["web_port"], 8765)
        self.assertIsInstance(back["web_port"], int)
        self.assertIs(back["strict_mcp"], False)
        self.assertEqual(back["session_timeout_s"], 14400.0)

    def test_tables_are_emitted(self):
        data = {
            "repo": str(self.repo),
            "source": "jira",
            "jira": {"site": "https://x.atlassian.net", "email": "a@b.c",
                     "token": "t", "project": "OPS"},
            "session_env": {"GH_TOKEN": "ghp_x"},
        }
        back = self.roundtrip(data)
        self.assertEqual(back["jira"]["project"], "OPS")
        self.assertEqual(back["session_env"]["GH_TOKEN"], "ghp_x")

    def test_a_value_with_quotes_and_backslashes_survives(self):
        # A Windows-shaped path or a JQL with a quoted status would break a
        # naive f'"{value}"'.
        nasty = 'a "quoted" \\ value\twith a tab'
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/t.md",
                "session_env": {"WEIRD": nasty}}
        self.assertEqual(self.roundtrip(data)["session_env"]["WEIRD"], nasty)

    def test_empty_values_are_omitted_not_emitted_blank(self):
        # An emitted `settings_file = ""` would be read back as a path that
        # does not exist, and load_config would then refuse the file the
        # wizard just wrote.
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/t.md",
                "settings_file": "", "web_token": ""}
        text = setup.dump_toml(data)
        self.assertNotIn("settings_file", text)
        self.assertNotIn("web_token", text)

    def test_help_text_is_emitted_as_comments(self):
        text = setup.dump_toml({"repo": str(self.repo),
                                "tasks_file": f"{self.tmp}/t.md"})
        self.assertIn("# ", text)
        self.assertIn("worktree", text)  # repo's help text

    def test_what_the_wizard_writes_is_what_load_config_reads(self):
        # The whole claim of this slice in one assertion.
        data = {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md",
                "model": "haiku", "web_port": 9000}
        path = self.tmp / "config.toml"
        path.write_text(setup.dump_toml(data))
        path.chmod(0o600)
        cfg = load_config(path, home=self.tmp / "home")
        self.assertEqual(cfg.repo, self.repo)
        self.assertEqual(cfg.model, "haiku")
        self.assertEqual(cfg.web_port, 9000)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_setup -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claudeloop.setup'`.

- [ ] **Step 3: Write the emitter**

Create `claudeloop/setup.py`:

```python
"""Setup mode: a loopback-only server that writes config.toml, and the TOML
emitter behind it.

It runs only when the loop does not. There is no shared state with the loop
at all, which is why this can write a file the dashboard's own rules would
forbid.
"""

from __future__ import annotations

import json

from .config import SCHEMA


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
    return json.dumps(str(value))


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
    table is documented in every file written afterwards for free.
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
            out.append(f"{name} = {_scalar(value)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_setup -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/setup.py tests/test_setup.py
git commit -m "feat: a schema-driven TOML emitter for the setup wizard"
```

---

### Task 4: The setup server and its guards

The server itself: loopback-only, one-time token, and the schema route the
wizard renders from. No write route yet.

**Files:**
- Modify: `claudeloop/setup.py`
- Create: `claudeloop/static/setup.html` (a placeholder page in this task, the
  real wizard in Task 7)
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `web.Handler`, `SCHEMA`, `Config`.
- Produces:
  - `setup.Handler(web.Handler)`.
  - `setup.serve(path: Path, home: Path, port: int, token: str) -> _SetupServer`
    — starts the server on a daemon thread and returns it. Exposed for tests;
    `run_setup` is what production calls.
  - `setup.run_setup(path: Path, home: Path, port: int = 8765) -> None` —
    prints the URL, blocks until saved.
  - `setup.schema_payload(existing: dict) -> dict` — what `GET
    /api/setup/schema` answers with.
  - `_SetupServer` attributes the handler reads: `.path`, `.home`, `.existing`
    (the parsed old TOML, `{}` on first run), `.saved` (a `threading.Event`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_setup.py`:

```python
import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request


class SetupServerBase(unittest.TestCase):
    """Fixture only -- no tests of its own.

    Deliberately not a test case other classes subclass: unittest would run
    every inherited test again in each subclass, and the first-run assertions
    below are false by construction in the editing subclass.
    """

    def existing_config(self) -> str:
        """config.toml as it stands before the wizard opens. "" is a first
        run. A method, not a class attribute, because the interesting cases
        interpolate paths setUp has only just created."""
        return ""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        # A bare `.git` is all validate() looks for. The live repo check in
        # CheckRouteTest makes a real repository of its own.
        (self.repo / ".git").mkdir(parents=True)
        self.path = self.tmp / "config.toml"
        body = self.existing_config()
        if body:
            self.path.write_text(body)
            self.path.chmod(0o600)
        self.token = "one-time-token"
        self.server = setup.serve(self.path, self.tmp / "home", 0, self.token)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def get(self, route, token="one-time-token"):
        url = self.base + route
        if token is not None:
            url += ("&" if "?" in route else "?") + "token=" + urllib.parse.quote(token)
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()

    def post(self, route, payload, content_type="application/json",
             token="one-time-token", raw=None):
        url = self.base + route
        if token is not None:
            url += "?token=" + urllib.parse.quote(token)
        body = raw if raw is not None else json.dumps(payload).encode()
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read() or b"{}")
            error.close()
            return error.code, payload


class FirstRunTest(SetupServerBase):
    def test_the_page_is_served(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        page = body.decode()
        self.assertIn("<!doctype html", page.lower())
        # No build step and no CDN: everything the page needs is in the file.
        self.assertNotIn("<script src=", page)
        self.assertNotIn("cdn.", page)

    def test_the_one_time_token_is_required(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/", token=None)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_a_wrong_token_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/", token="guess")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_a_foreign_host_header_is_refused(self):
        # DNS rebinding: a page in a browser on this machine can point an
        # attacker-controlled hostname at 127.0.0.1 and still reach here.
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", f"/?token={self.token}",
                           headers={"Host": "evil.example:80"})
        self.assertEqual(connection.getresponse().status, 403)

    def test_the_schema_route_describes_every_field(self):
        _, body = self.get("/api/setup/schema")
        payload = json.loads(body)
        keys = [field["key"] for field in payload["fields"]]
        self.assertIn("repo", keys)
        self.assertIn("jira.site", keys)
        self.assertIn("strict_mcp", keys)
        for field in payload["fields"]:
            self.assertTrue(field["label"])
            self.assertTrue(field["help"])
            self.assertIn(field["step"], [step["id"] for step in payload["steps"]])

    def test_the_schema_route_says_this_is_a_first_run(self):
        payload = json.loads(self.get("/api/setup/schema")[1])
        self.assertFalse(payload["editing"])
        self.assertEqual(payload["values"], {})

    def test_an_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()


class EditingTest(SetupServerBase):
    def existing_config(self) -> str:
        return (
            f'repo = "{self.repo}"\n'
            'model = "haiku"\n'
            'web_token = "hunter2"\n'
            "[jira]\n"
            'token = "jira-secret"\n'
            "[session_env]\n"
            'GH_TOKEN = "ghp_secret"\n'
        )

    def test_existing_values_are_prefilled(self):
        payload = json.loads(self.get("/api/setup/schema")[1])
        self.assertTrue(payload["editing"])
        self.assertEqual(payload["values"]["model"], "haiku")

    def test_no_secret_ever_reaches_the_browser(self):
        # The wizard is exactly the screen an operator screenshots when
        # asking for help, and under S4 it is reached through Home Assistant
        # ingress, which logs.
        _, body = self.get("/api/setup/schema")
        self.assertNotIn(b"hunter2", body)
        self.assertNotIn(b"jira-secret", body)
        self.assertNotIn(b"ghp_secret", body)
        payload = json.loads(body)
        self.assertIn("web_token", payload["secrets_set"])
        self.assertIn("jira.token", payload["secrets_set"])
        self.assertEqual(payload["session_env"], {"GH_TOKEN": ""})
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_setup.FirstRunTest -v`
Expected: FAIL with `AttributeError: module 'claudeloop.setup' has no attribute
'serve'`.

- [ ] **Step 3: Implement the server**

Add to `claudeloop/setup.py`:

```python
import logging
import secrets
import threading
import tomllib
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import web
from .config import DEFAULT_CONFIG, HOME, SCHEMA, Config

log = logging.getLogger("claudeloop.setup")

STATIC = Path(__file__).parent / "static"

STEPS = (
    {"id": "repository", "title": "Repository"},
    {"id": "source", "title": "Task source"},
    {"id": "dashboard", "title": "Dashboard"},
    {"id": "instructions", "title": "Instructions"},
    {"id": "advanced", "title": "Advanced"},
    {"id": "review", "title": "Review and save"},
)

MAX_BODY_BYTES = 256 * 1024
"""A whole config is a few kilobytes. This bounds what an unauthenticated
peer can make the process allocate before the token check has even run."""


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
        "required": field.required or field.required_if is not None,
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
        self._json(404, {"error": "not found"})

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
        return payload if isinstance(payload, dict) else None


class _SetupServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, path: Path, home: Path, token: str):
        # Setup mode binds loopback unconditionally, whatever an existing
        # config says: with no config there is no web_token to authenticate
        # against, so the network barrier cannot be the only one.
        self.cfg = Config(repo=Path("."), web_host="127.0.0.1", web_token=token)
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
    log.warning("ClaudeLoop is not configured yet. Open the setup wizard:\n\n    %s\n", url)
    try:
        server.saved.wait()
    except KeyboardInterrupt:
        raise SystemExit("setup cancelled")
    finally:
        server.shutdown()
        server.server_close()
```

Add `json` to the imports if Task 3 did not already (it did, for `_scalar`).

Create `claudeloop/static/setup.html` as a placeholder Task 7 replaces:

```html
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>ClaudeLoop setup</title></head>
<body><p>The setup wizard lands in Task 7.</p></body>
</html>
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_setup -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/setup.py claudeloop/static/setup.html tests/test_setup.py
git commit -m "feat: a loopback-only setup server behind a one-time token"
```

---

### Task 5: Validating and saving

The two write-side routes. This is where the secret-merge rule and the 0600
write live.

**Files:**
- Modify: `claudeloop/setup.py`
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `Handler._read_json`, `_SetupServer.existing/.path/.saved` from
  Task 4; `validate`, `dump_toml`.
- Produces:
  - `POST /api/setup/validate` — `{"values": {...}}` in, `{"errors": {key:
    message}}` out, always 200.
  - `POST /api/setup/save` — the same body in; 200 `{"ok": true}` on success,
    400 `{"errors": {...}}` otherwise.
  - `setup.merge_secrets(submitted: dict, existing: dict) -> dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_setup.py`:

```python
class ValidateRouteTest(SetupServerBase):
    def values(self, **extra) -> dict:
        return {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md", **extra}

    def test_a_good_config_validates_clean(self):
        code, payload = self.post("/api/setup/validate", {"values": self.values()})
        self.assertEqual(code, 200)
        self.assertEqual(payload["errors"], {})

    def test_errors_come_back_keyed_by_field(self):
        code, payload = self.post("/api/setup/validate", {"values": {
            "repo": str(self.tmp / "nope"), "web_host": "0.0.0.0"}})
        self.assertEqual(code, 200)
        self.assertIn("repo", payload["errors"])
        self.assertIn("web_token", payload["errors"])
        self.assertIn("tasks_file", payload["errors"])

    def test_validate_writes_nothing(self):
        self.post("/api/setup/validate", {"values": self.values()})
        self.assertFalse(self.path.exists())

    def test_a_post_without_the_json_content_type_is_refused(self):
        code, _ = self.post("/api/setup/validate", {"values": self.values()},
                            content_type="text/plain")
        self.assertEqual(code, 415)

    def test_a_rejected_post_cannot_smuggle_a_second_request(self):
        # Inherited from web.Handler's do_POST, and pinned here because this
        # server writes config.toml: a cross-origin page could otherwise send
        # one CORS-safelisted text/plain POST whose body is a well-formed
        # application/json POST, and the second pass would clear every guard.
        inner_body = json.dumps({"values": self.values()})
        smuggled = (
            f"POST /api/setup/save?token={self.token} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.server_port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(inner_body)}\r\n\r\n{inner_body}"
        )
        outer = (
            f"POST /api/setup/validate?token={self.token} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.server.server_port}\r\n"
            "Content-Type: text/plain;charset=UTF-8\r\n"
            f"Content-Length: {len(smuggled)}\r\n\r\n{smuggled}"
        )
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(outer.encode())
        sock.settimeout(2)
        received = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
        except (TimeoutError, OSError):
            pass
        self.assertNotIn(b"200 OK", received, received)
        self.assertFalse(self.path.exists(), "the smuggled POST wrote a config")


class SaveRouteTest(SetupServerBase):
    def values(self, **extra) -> dict:
        return {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md", **extra}

    def test_saving_writes_a_config_load_config_accepts(self):
        code, payload = self.post("/api/setup/save", {"values": self.values(model="haiku")})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        cfg = load_config(self.path, home=self.tmp / "home")
        self.assertEqual(cfg.model, "haiku")

    def test_the_file_is_written_0600(self):
        # It holds web_token, the Jira API token and every [session_env]
        # credential, and load_config refuses to read it at any other mode.
        self.post("/api/setup/save", {"values": self.values()})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_saving_releases_the_waiter(self):
        self.assertFalse(self.server.saved.is_set())
        self.post("/api/setup/save", {"values": self.values()})
        self.assertTrue(self.server.saved.wait(timeout=5))

    def test_an_invalid_config_is_not_written(self):
        code, payload = self.post("/api/setup/save",
                                  {"values": {"repo": str(self.tmp / "nope")}})
        self.assertEqual(code, 400)
        self.assertIn("repo", payload["errors"])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.server.saved.is_set())


class SaveSecretsTest(SetupServerBase):
    def existing_config(self) -> str:
        return (
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n'
            'web_host = "0.0.0.0"\n'
            'web_token = "hunter2"\n'
            "[session_env]\n"
            'GH_TOKEN = "ghp_secret"\n'
        )

    def values(self, **extra) -> dict:
        return {"repo": str(self.repo), "tasks_file": f"{self.tmp}/tasks.md",
                "web_host": "0.0.0.0", **extra}

    def test_a_blank_secret_keeps_the_stored_value(self):
        # The browser was never told the token, so blank means "unchanged",
        # not "clear it" -- and web_host is non-loopback here, so clearing it
        # would fail validation outright.
        code, _ = self.post("/api/setup/save",
                            {"values": self.values(web_token="",
                                                   session_env={"GH_TOKEN": ""})})
        self.assertEqual(code, 200)
        cfg = load_config(self.path, home=self.tmp / "home")
        self.assertEqual(cfg.web_token, "hunter2")
        self.assertEqual(cfg.session_env["GH_TOKEN"], "ghp_secret")

    def test_a_new_secret_replaces_the_stored_one(self):
        code, _ = self.post("/api/setup/save",
                            {"values": self.values(web_token="rotated")})
        self.assertEqual(code, 200)
        cfg = load_config(self.path, home=self.tmp / "home")
        self.assertEqual(cfg.web_token, "rotated")

    def test_a_removed_session_env_name_is_dropped(self):
        # Blank keeps a value; omitting the name entirely removes the entry.
        code, _ = self.post("/api/setup/save",
                            {"values": self.values(web_token="", session_env={})})
        self.assertEqual(code, 200)
        cfg = load_config(self.path, home=self.tmp / "home")
        self.assertEqual(cfg.session_env, {})
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_setup.SaveRouteTest -v`
Expected: FAIL — every save returns 404 today.

- [ ] **Step 3: Implement the routes**

In `claudeloop/setup.py`, add `os` to the imports, add `validate` to the
`.config` import, and replace `Handler.do_POST`'s final `self._json(404, ...)`
with a router:

```python
        route = parsed.path
        if route == "/api/setup/validate":
            self._validate()
        elif route == "/api/setup/save":
            self._save()
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
        values = self._submitted()
        if values is None:
            return
        _, errors = validate(values)
        if errors:
            self._json(400, {"errors": dict(errors)})
            return
        try:
            write_config(self.server.path, values)
        except OSError as error:
            self._json(500, {"error": f"could not write the config: {error}"})
            return
        self._json(200, {"ok": True})
        # Released after the response is written, so the operator's browser
        # gets its answer before run_setup returns and the server shuts down.
        self.server.saved.set()
```

And, at module level:

```python
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
        table = merged.setdefault(field.section, {}) if field.section else merged
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


def write_config(path: Path, values: dict) -> None:
    """Write config.toml at 0600, whatever the umask is.

    load_config refuses a config readable beyond its owner, so a file written
    at the default 0644 would be one the loop starting seconds later cannot
    read. os.open with the mode, not a chmod afterwards: the window between
    creating a world-readable file holding an API token and narrowing it is
    small, and does not need to exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dump_toml(values)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    # O_CREAT's mode applies only to a file this call created; an existing
    # one keeps whatever mode it had, which may be the 0644 the operator is
    # here to escape.
    os.chmod(path, 0o600)
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_setup -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/setup.py tests/test_setup.py
git commit -m "feat: validate and save routes, with secrets that never leave the process"
```

---

### Task 6: The three live checks

Each one turns a failure that is otherwise silent at runtime into a message
while the operator is still looking at the form.

**Files:**
- Modify: `claudeloop/setup.py`
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `Handler._submitted` from Task 5; `worktree.probe`;
  `jira.JiraClient`, `jira.compose_jql`, `jira.JiraError`; `config._compose_jql`.
- Produces: `POST /api/setup/test` — `{"what": "repo"|"jira"|"claude",
  "values": {...}}` in, `{"ok": bool, "message": str}` out, always 200.
  `setup.check_repo/check_jira/check_claude(values) -> tuple[bool, str]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_setup.py`, adding `import os` and these two imports —
both fixtures already exist and neither should be re-written:

```python
from .gitrepo import make_repo      # a real repo, one commit on main, gpgsign off
from .jira_fake import FakeJira     # routes {"POST /search/jql": (status, body)}


class CheckRouteTest(SetupServerBase):
    def test_the_repo_check_passes_on_a_real_repository(self):
        # A real repository, not a bare .git directory: worktree.probe shells
        # out to `git worktree prune` and then resolves the default branch.
        repo = make_repo(self.tmp / "real")
        code, payload = self.post("/api/setup/test",
                                  {"what": "repo", "values": {"repo": str(repo)}})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"], payload["message"])

    def test_the_repo_check_explains_a_directory_that_is_not_a_repository(self):
        (self.tmp / "plain").mkdir()
        _, payload = self.post("/api/setup/test",
                               {"what": "repo", "values": {"repo": str(self.tmp / "plain")}})
        self.assertFalse(payload["ok"])
        self.assertIn("worktree", payload["message"])

    def test_an_unknown_check_is_refused(self):
        code, payload = self.post("/api/setup/test",
                                  {"what": "astrology", "values": {}})
        self.assertEqual(code, 400)

    def test_the_claude_check_reports_what_the_cli_says(self):
        # A fake `claude` on PATH, the same technique the session tests use.
        fake = self.tmp / "bin"
        fake.mkdir()
        script = fake / "claude"
        script.write_text(
            "#!/bin/sh\n"
            'echo \'{"loggedIn": true, "authMethod": "claude.ai",'
            ' "subscriptionType": "pro"}\'\n'
        )
        script.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{fake}:{old}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        _, payload = self.post("/api/setup/test", {"what": "claude", "values": {}})
        self.assertTrue(payload["ok"], payload["message"])
        self.assertIn("claude.ai", payload["message"])

    def test_the_claude_check_says_so_when_the_cli_is_missing(self):
        old = os.environ["PATH"]
        os.environ["PATH"] = str(self.tmp / "empty")
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        _, payload = self.post("/api/setup/test", {"what": "claude", "values": {}})
        self.assertFalse(payload["ok"])
        self.assertIn("claude", payload["message"])

    def test_the_claude_check_applies_session_env(self):
        # A stray ANTHROPIC_API_KEY in [session_env] moves the session off
        # subscription billing, so the rate_limit_events the whole recovery
        # path is built on stop arriving. The check must see what a session
        # would see, not what this process happens to have.
        fake = self.tmp / "bin2"
        fake.mkdir()
        script = fake / "claude"
        script.write_text(
            "#!/bin/sh\n"
            'echo "{\\"loggedIn\\": true, \\"authMethod\\": \\"$ANTHROPIC_API_KEY\\"}"\n'
        )
        script.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{fake}:{old}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        _, payload = self.post("/api/setup/test", {
            "what": "claude",
            "values": {"session_env": {"ANTHROPIC_API_KEY": "leaked"}},
        })
        self.assertIn("leaked", payload["message"])

    def test_the_jira_check_reports_the_matching_issue_count(self):
        jira = FakeJira({"POST /search/jql": (
            200, {"issues": [{"key": "OPS-1"}, {"key": "OPS-2"}]})})
        self.addCleanup(jira.close)
        _, payload = self.post("/api/setup/test", {"what": "jira", "values": {
            "source": "jira",
            "jira": {"site": jira.url, "email": "a@b.c", "token": "t",
                     "project": "OPS"},
        }})
        self.assertTrue(payload["ok"], payload["message"])
        self.assertIn("2", payload["message"])

    def test_the_jira_check_sends_the_composed_query(self):
        # The label guard is spliced on by compose_jql. A check that reported
        # on a different query than the loop will actually poll with would be
        # worse than no check.
        jira = FakeJira({"POST /search/jql": (200, {"issues": []})})
        self.addCleanup(jira.close)
        self.post("/api/setup/test", {"what": "jira", "values": {
            "source": "jira",
            "jira": {"site": jira.url, "email": "a@b.c", "token": "t",
                     "project": "OPS", "status": "To Do"},
        }})
        _, _, body = jira.requests[-1]
        self.assertIn('project = "OPS"', body["jql"])
        self.assertIn("claudeloop-done", body["jql"])

    def test_the_jira_check_reports_a_rejected_token(self):
        jira = FakeJira({"POST /search/jql": (401, {"errorMessages": ["nope"]})})
        self.addCleanup(jira.close)
        _, payload = self.post("/api/setup/test", {"what": "jira", "values": {
            "source": "jira",
            "jira": {"site": jira.url, "email": "a@b.c", "token": "wrong",
                     "project": "OPS"},
        }})
        self.assertFalse(payload["ok"])
        self.assertIn("401", payload["message"])
```

`FakeJira` serves plain HTTP, so `_https_site` would reject its URL through
`validate`. That is why `_test` does not call `validate` first, and it is not
an oversight: a live check reports what the network says, validation reports
what the schema says, and conflating them would make the check unable to run
until the form was already perfect. `JiraClient` is built with `retries=1` so a
401 answers immediately rather than sleeping through `BACKOFF_S` while an
operator watches a spinner.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_setup.CheckRouteTest -v`
Expected: FAIL — the route 404s today.

- [ ] **Step 3: Implement the checks**

Add to `claudeloop/setup.py`:

```python
import subprocess

from . import worktree
from .config import _compose_jql
from .jira import JiraClient, JiraError, compose_jql

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


def check_jira(values: dict) -> tuple[bool, str]:
    """One authenticated search with the composed query.

    A bad token, an unreachable site and a JQL Jira rejects are otherwise
    indistinguishable from an empty backlog, forever, with nothing saying why
    -- the loop is built to idle rather than fail on all three. This is the
    only place that difference is ever visible.
    """
    table = values.get("jira") or {}
    site = str(table.get("site", "")).strip()
    if not site:
        return False, "no Jira site set"
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
    if not payload.get("loggedIn"):
        return False, "claude is installed but not signed in. Run: claude setup-token"
    method = payload.get("authMethod", "unknown")
    plan = payload.get("subscriptionType", "")
    return True, f"signed in via {method}{f' ({plan})' if plan else ''}"


CHECKS = {"repo": check_repo, "jira": check_jira, "claude": check_claude}
```

And the route, in `do_POST`'s router:

```python
        elif route == "/api/setup/test":
            self._test()
```

```python
    def _test(self) -> None:
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
```

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_setup -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/setup.py tests/test_setup.py tests/jira_fake.py
git commit -m "feat: live repo, Jira and claude-auth checks in the wizard"
```

---

### Task 7: The wizard page

One no-build HTML file, rendered entirely from `/api/setup/schema`.

**Files:**
- Modify: `claudeloop/static/setup.html` (replacing the Task 4 placeholder)
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `/api/setup/schema`, `/api/setup/validate`, `/api/setup/test`,
  `/api/setup/save`.
- Produces: nothing other modules read.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_setup.py`:

```python
class WizardPageTest(SetupServerBase):
    def test_the_page_is_self_contained(self):
        page = self.get("/")[1].decode()
        self.assertNotIn("<script src=", page)
        self.assertNotIn("<link rel=\"stylesheet\"", page)
        self.assertNotIn("cdn.", page)
        self.assertNotIn("http://fonts", page)
        self.assertIn("#fd7c33", page.lower())  # the brand accent is used

    def test_the_page_names_every_step_and_route(self):
        page = self.get("/")[1].decode()
        for step in ("Repository", "Task source", "Dashboard", "Instructions",
                     "Advanced", "Review and save"):
            self.assertIn(step, page)
        for route in ("/api/setup/schema", "/api/setup/validate",
                      "/api/setup/test", "/api/setup/save"):
            self.assertIn(route, page)

    def test_the_page_carries_the_token_on_its_own_requests(self):
        # Every request needs the one-time token, and the page only ever has
        # it from its own URL.
        page = self.get("/")[1].decode()
        self.assertIn("location.search", page)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m unittest tests.test_setup.WizardPageTest -v`
Expected: FAIL — the placeholder page has none of it.

- [ ] **Step 3: Write the page**

Read `claudeloop/static/index.html` first and reuse its `:root` token block
verbatim — the same `--bg`, `--surface`, `--line`, `--text`, `--muted`,
`--accent`, `--accent-ink`, `--ok`, `--bad`, `--shadow`, and both dark-mode
blocks. The wizard should look like the dashboard, not like a different
product. Do not add a theme toggle; the `prefers-color-scheme` block is enough.

Structure:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>ClaudeLoop setup</title>
<link rel="icon" href="/logo.png">
<style>
/* the :root token block copied from index.html, then: */
body { background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, sans-serif;
       margin: 0; padding: 2rem 1rem; }
main { max-width: 46rem; margin: 0 auto; }
nav { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1.5rem; }
nav button { background: none; border: 1px solid var(--line); border-radius: 999px;
             padding: .3rem .8rem; color: var(--muted); cursor: pointer; }
nav button[aria-current="step"] { background: var(--accent); border-color: var(--accent);
                                  color: #fff; }
.field { margin-bottom: 1.25rem; }
.field label { display: block; font-weight: 600; }
.field .help { color: var(--muted); font-size: .875rem; margin: .15rem 0 .4rem; }
.field input, .field select { width: 100%; padding: .5rem; border-radius: 6px;
                              border: 1px solid var(--line); background: var(--surface);
                              color: var(--text); font: inherit; }
.field .error { color: var(--bad); font-size: .875rem; margin-top: .25rem; }
.field.bad input { border-color: var(--bad); }
.result.ok { color: var(--ok); }
.result.bad { color: var(--bad); }
</style>
</head>
<body>
<main>
  <h1>ClaudeLoop setup</h1>
  <nav id="steps"></nav>
  <form id="form"></form>
  <p id="message"></p>
  <button id="back">Back</button>
  <button id="next">Next</button>
</main>
<script type="module">
const token = new URLSearchParams(location.search).get("token") || "";
const url = (route) => route + "?token=" + encodeURIComponent(token);

const api = async (route, body) => {
  const response = await fetch(url(route), {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  return response.json();
};

const schema = await (await fetch(url("/api/setup/schema"))).json();
const draft = structuredClone(schema.values);
const env = structuredClone(schema.session_env);
let step = 0;

const put = (key, value) => {
  const field = schema.fields.find((f) => f.key === key);
  const table = field.section ? (draft[field.section] ||= {}) : draft;
  table[field.name] = value;
};

const get = (key) => {
  const field = schema.fields.find((f) => f.key === key);
  const table = field.section ? draft[field.section] || {} : draft;
  return table[field.name] ?? "";
};

const payload = () => ({...draft, session_env: env});

function render(errors = {}) {
  /* nav */
  document.getElementById("steps").replaceChildren(...schema.steps.map((s, i) => {
    const button = document.createElement("button");
    button.textContent = s.title;
    if (i === step) button.setAttribute("aria-current", "step");
    /* Editing an existing config: jumping straight to the one key you came
       to change should not be a five-click walk. */
    button.disabled = !schema.editing && i > step;
    button.onclick = () => { step = i; render(); };
    return button;
  }));

  const current = schema.steps[step].id;
  const form = document.getElementById("form");
  form.replaceChildren();
  if (current === "review") { renderReview(form); return; }

  for (const field of schema.fields) {
    if (field.step !== current) continue;
    /* The [jira] block is only meaningful under source = "jira". */
    if (field.section === "jira" && get("source") !== "jira") continue;
    if (field.name === "tasks_file" && get("source") !== "file") continue;
    form.append(renderField(field, errors[field.key]));
  }
}

function renderField(field, error) {
  const wrap = document.createElement("div");
  wrap.className = "field" + (error ? " bad" : "");
  const label = document.createElement("label");
  label.textContent = field.label + (field.required ? " *" : "");
  label.htmlFor = field.key;
  const help = document.createElement("p");
  help.className = "help";
  help.textContent = field.help;
  let input;
  if (field.type === "choice") {
    input = document.createElement("select");
    input.append(...field.choices.map((choice) => new Option(choice, choice)));
    input.value = get(field.key) || field.default || "";
  } else if (field.type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(get(field.key));
  } else {
    input = document.createElement("input");
    input.type = field.secret ? "password" : "text";
    input.value = get(field.key);
    if (field.secret && schema.secrets_set.includes(field.key)) {
      input.placeholder = "set — leave blank to keep";
    } else if (field.default !== null && field.default !== undefined) {
      input.placeholder = String(field.default);
    }
  }
  input.id = field.key;
  input.oninput = () => put(field.key,
    field.type === "bool" ? input.checked : input.value);
  input.onchange = input.oninput;
  /* source drives which fields exist at all, so redraw on its change. */
  if (field.key === "source") input.onchange = () => { put(field.key, input.value); render(); };
  wrap.append(label, help, input);
  if (error) {
    const message = document.createElement("p");
    message.className = "error";
    message.textContent = error;
    wrap.append(message);
  }
  if (field.key === "repo") wrap.append(testButton("repo", "Check this repository"));
  if (field.key === "token" && field.section === "jira") {
    wrap.append(testButton("jira", "Test the Jira connection"));
  }
  return wrap;
}

function testButton(what, label) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  const result = document.createElement("p");
  result.className = "result";
  button.onclick = async () => {
    result.textContent = "checking…";
    result.className = "result";
    const answer = await api("/api/setup/test", {what, values: payload()});
    result.textContent = answer.message;
    result.className = "result " + (answer.ok ? "ok" : "bad");
  };
  const wrap = document.createElement("div");
  wrap.append(button, result);
  return wrap;
}

function renderReview(form) {
  const list = document.createElement("dl");
  for (const field of schema.fields) {
    const value = get(field.key);
    if (value === "" || value === null || value === undefined) continue;
    const term = document.createElement("dt");
    term.textContent = field.key;
    const definition = document.createElement("dd");
    definition.textContent = field.secret ? "••••••" : String(value);
    list.append(term, definition);
  }
  form.append(list, testButton("claude", "Check the Claude CLI"));
}

document.getElementById("back").onclick = () => {
  if (step > 0) { step -= 1; render(); }
};

document.getElementById("next").onclick = async () => {
  const message = document.getElementById("message");
  const last = step === schema.steps.length - 1;
  if (last) {
    const answer = await api("/api/setup/save", {values: payload()});
    if (answer.ok) {
      /* No automatic redirect: the setup server shuts down as the dashboard
         binds, and a redirect carrying a freshly-set web_token would write
         that token into browser history. */
      document.body.replaceChildren();
      const done = document.createElement("main");
      done.innerHTML =
        "<h1>Saved</h1><p>ClaudeLoop is starting. The dashboard is at " +
        "<code>http://" + (get("web_host") || "127.0.0.1") + ":" +
        (get("web_port") || "8765") + "/</code>.</p>";
      document.body.append(done);
      return;
    }
    render(answer.errors || {});
    message.textContent = "Fix the errors above before saving.";
    return;
  }
  const answer = await api("/api/setup/validate", {values: payload()});
  const errors = answer.errors || {};
  const mine = Object.fromEntries(Object.entries(errors).filter(([key]) => {
    const field = schema.fields.find((f) => f.key === key);
    return field && field.step === schema.steps[step].id;
  }));
  /* Only this screen's errors block advancing: a later screen's unfilled
     requirement must not trap the operator on an earlier one. */
  if (Object.keys(mine).length) { render(mine); message.textContent = ""; return; }
  step += 1;
  message.textContent = "";
  render();
};

render();
</script>
</body>
</html>
```

`[session_env]` rows: add a small add/remove list on the Advanced step, writing
into the `env` object above. Keep it to a name input, a value input, and a
remove button per row, plus one "Add variable" button — it is a plain
`Object` and needs no more than that.

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_setup -v`
Expected: PASS.

- [ ] **Step 5: Look at it**

Run: `python - <<'EOF'` starting `setup.serve` on a fixed port against a scratch
path, then open the printed URL. Walk all six steps, confirm the `[jira]` block
appears only under `source = "jira"`, and that a bad repo path shows its error
under the right field.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/static/setup.html tests/test_setup.py
git commit -m "feat: the setup wizard page, rendered from the schema"
```

---

### Task 8: Wiring setup into `main()`

**Files:**
- Modify: `claudeloop/loop.py:631-655`
- Test: `tests/test_loop.py`

**Interfaces:**
- Consumes: `setup.run_setup`.
- Produces: `main()` accepting `--setup`; the `no config file at ...` SystemExit
  is gone.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_loop.py`:

```python
class MainSetupTest(unittest.TestCase):
    """main() enters setup mode when there is no config, and on --setup.

    Stubbed rather than mocked: run_setup is replaced with a function that
    records the call and raises, which is enough to pin the ordering without
    standing up a server or running the loop.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.calls = []
        self.real_run_setup = loop.setup.run_setup
        self.real_default = loop.DEFAULT_CONFIG
        self.addCleanup(lambda: setattr(loop.setup, "run_setup", self.real_run_setup))
        self.addCleanup(lambda: setattr(loop, "DEFAULT_CONFIG", self.real_default))

        def stub(path, home, port=8765):
            self.calls.append(path)
            raise SystemExit("stub ran")

        loop.setup.run_setup = stub
        loop.DEFAULT_CONFIG = self.tmp / "config.toml"

    def test_no_config_file_enters_setup(self):
        with self.assertRaises(SystemExit) as caught:
            loop.main([])
        self.assertEqual(str(caught.exception), "stub ran")
        self.assertEqual(self.calls, [self.tmp / "config.toml"])

    def test_setup_flag_enters_setup_even_with_a_config(self):
        loop.DEFAULT_CONFIG.write_text("repo = \"/nope\"\n")
        loop.DEFAULT_CONFIG.chmod(0o600)
        with self.assertRaises(SystemExit):
            loop.main(["--setup"])
        self.assertEqual(len(self.calls), 1)

    def test_an_existing_config_does_not_enter_setup(self):
        loop.DEFAULT_CONFIG.write_text("nonsense = [\n")
        loop.DEFAULT_CONFIG.chmod(0o600)
        with self.assertRaises(Exception):
            loop.main([])
        self.assertEqual(self.calls, [])
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest tests.test_loop.MainSetupTest -v`
Expected: FAIL — `main()` takes no argument today.

- [ ] **Step 3: Rewire `main()`**

In `claudeloop/loop.py`, add `import argparse`, `from . import setup`, and
`from .config import DEFAULT_CONFIG, HOME, Config, load_config`:

```python
def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m claudeloop")
    parser.add_argument(
        "--setup", action="store_true",
        help="open the setup wizard against the existing config",
    )
    args = parser.parse_args(argv)
    # No config is not an error any more -- it is a first run. The wizard
    # blocks until it has written one, then this falls through into the
    # ordinary startup path, so the config the loop runs is the one the
    # ordinary loader reads back off disk rather than the wizard's own parse.
    if args.setup or not DEFAULT_CONFIG.exists():
        setup.run_setup(DEFAULT_CONFIG, HOME)
    try:
        cfg = load_config()
    except FileNotFoundError:
        raise SystemExit(f"no config file at {DEFAULT_CONFIG}")
    except ValueError as error:
        raise SystemExit(str(error))
    problem = worktree.probe(cfg.repo)
    if problem:
        raise SystemExit(problem)
    _serve_dashboard(cfg)
    asyncio.run(main_loop(cfg))
```

Note the test replaces `loop.DEFAULT_CONFIG`, so `main` must read the
module-level name rather than importing it into a local.

- [ ] **Step 4: Run the tests**

Run: `python -m unittest tests.test_loop -v`
Expected: PASS.

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/loop.py tests/test_loop.py
git commit -m "feat: main() opens the setup wizard when there is no config"
```

---

### Task 9: Documentation

**Files:**
- Modify: `README.md`, `ROADMAP.md`, `CLAUDE.md`

- [ ] **Step 1: README**

Add a **Setup** section immediately before **Configure**:

- `python -m claudeloop` with no config prints a loopback URL carrying a
  one-time token; open it and the wizard writes `~/.claudeloop/config.toml` for
  you, at 0600.
- `python -m claudeloop --setup` reopens it against an existing config. Secret
  fields come back blank and marked *set* — leave one blank to keep it, type in
  it to replace it.
- The wizard binds `127.0.0.1` whatever `web_host` says, and the token is
  required on every request.
- It can check three things live: that `repo` is a repository git can make
  worktrees in, that Jira answers with the composed query, and that the
  `claude` CLI is installed and signed in.
- Hand-editing TOML still works and is still documented below; the wizard
  writes the same file, with the help text as comments.

State in **Configure** that the file the wizard writes is annotated from the
same table that validates it.

- [ ] **Step 2: ROADMAP**

- Move **S5** from Next to Built, with a paragraph covering: the schema table,
  the fact that the wizard and the loader now validate through one function,
  setup mode's two barriers, and whatever the live smoke test finds.
- Record the scope change: the curated plugin set and its fourth prompt layer
  were removed from S5 and are now a slice of their own. Add that slice to the
  table as **S7 — Proposed plugin set**, `not started`, carrying the decided
  points from S5's old entry verbatim (the per-plugin usage file, the layer
  slotting below the operator layer and above the definition of done, and the
  `superpowers` question-discipline rule).
- Add any new open issue the work surfaces to **Open issues carried across
  slices**.

- [ ] **Step 3: CLAUDE.md**

- Add `setup.py` to the module table: *"Setup mode: the schema-rendered wizard
  and the TOML writer. Runs only when the loop does not."*
- Amend the read-only constraint. It currently names S2b as the one deliberate
  exception and S5 as the next one; S5 has now landed, so it must say what
  setup mode actually does: it is a **separate server**, running only while the
  loop is not, binding loopback unconditionally behind a one-time token, and
  writing exactly one file.
- Note that `config.py`'s `SCHEMA` order is load-bearing.

- [ ] **Step 4: Run the whole suite one last time**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add README.md ROADMAP.md CLAUDE.md
git commit -m "docs: the setup wizard, and S5's narrowed scope"
```

---

## After the tasks: the live smoke test

**Not optional, and unusually load-bearing here** — the wizard's entire claim is
that the config it writes starts a loop. Four of six previous slices' smoke
tests found defects a passing suite could not.

1. Move any real `~/.claudeloop/config.toml` aside.
2. Make a scratch repository with a couple of commits on `main` and
   `git config --local commit.gpgsign false`.
3. Write a tasks file **outside** it with two distinct trivial tasks.
4. Run `python -m claudeloop`, open the printed URL, and walk all six screens
   with `model = "haiku"`. Run all three live checks and note what each says.
5. Save, and confirm the loop starts in the same process and runs **both**
   tasks — two, not one: several past defects only appeared on the second,
   where state left by the first one matters.
6. Stop it, run `python -m claudeloop --setup`, change one non-secret key,
   leave every secret blank, save, and confirm the secrets survived and the
   loop starts again.
7. Record findings in `ROADMAP.md` and fix them before merging.

Cost: about ten cents.

## Then

`superpowers:requesting-code-review` over the whole branch, then
`superpowers:finishing-a-development-branch` to merge.
