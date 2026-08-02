# S5 — Setup wizard and config schema

**Date:** 2026-08-03
**Status:** designed, not built

## The problem

`config.toml` carries 23 keys today — 15 top-level and 8 under `[jira]`, plus
the free-form `[session_env]` table — with non-obvious
interactions between them: `tasks_file` is required under one `source` and
forbidden inside `repo`; `web_token` is required only when `web_host` is not
loopback; `strict_mcp` without `mcp_config` silently disables every MCP server
on the machine. All of it is hand-edited TOML, validated by hand-written checks
in `load_config` that raise on the first failure and are readable only as
Python.

Two audiences are badly served. An operator setting ClaudeLoop up for the
first time has to read the README end to end and get every key right before
anything runs. And the S4 Home Assistant addon operator — the one this whole
packaging slice exists for — has no terminal to hand-edit TOML in at all.

## Scope

In: the config schema, and the browser setup wizard it renders.

Out, deferred to its own slice: the curated **proposed plugin set** and the
fourth prompt layer its per-plugin usage files would add. That is a change to
the composed system prompt, and prompt strings are the product here — it
deserves its own spec, its own covering tests, and its own live smoke test
rather than riding along behind a config refactor. `ROADMAP.md`'s S5 entry
listed it; this spec removes it from S5 and it becomes a slice of its own.

## Design

### 1. The schema

One tuple of frozen `Field` records in `config.py`, the single source of truth
for both the loader and the wizard:

```python
@dataclass(frozen=True)
class Field:
    name: str
    type: str                  # "path" | "str" | "int" | "float" | "bool" | "choice"
    default: object = None
    section: str = ""          # "" for a top-level key, "jira" for [jira]
    step: str = "repository"   # which wizard screen this key appears on
    label: str = ""
    help: str = ""
    secret: bool = False
    choices: tuple[str, ...] = ()
    required: bool = False
    required_if: Callable[[dict], bool] | None = None
    check: Callable[[object, dict], str | None] = None
```

`required_if` and `check` both take the whole submitted data, so every
condition `load_config` enforces today becomes table data rather than an `if`
in a function:

| Rule | Where it lives |
|---|---|
| `repo` present, and is a git repository | `required=True`, `check` on `repo` |
| `tasks_file` required under `source = "file"` | `required_if` on `tasks_file` |
| `tasks_file` must not resolve inside `repo` | `check` on `tasks_file` |
| `source` is one of `file`, `jira` | `type="choice"`, `choices` |
| `jira.site`/`email`/`token` required under `source = "jira"` | `required_if` on each |
| `jira.site` starts `https://` | `check` on `site` |
| `jira.project` required when `source = "jira"` and no `jql` | `required_if` on `project` |
| `web_token` required when `web_host` is not loopback | `required_if` on `web_token` |
| `web_token` is ASCII | `check` on `web_token` |
| `settings_file` / `mcp_config` exist when named | `check` on each |
| `strict_mcp` requires `mcp_config` | `check` on `strict_mcp` |

Two rules stay outside the table, because neither is a property of a key:

- **The 0600 secrets guard.** It is a property of the file, checked before any
  key is read.
- **`[session_env]`.** Its names are free-form, so there is no field to attach
  to; `_session_env` keeps its own validation.

`validate(data) -> list[tuple[str, str]]` walks the table and returns **every**
error as `(key, message)` pairs. The wizard needs them all, keyed per field, to
mark up a form. `load_config` calls the same function and raises `ValueError`
on the first pair, so its behaviour from the command line is unchanged — the
existing messages are kept verbatim, since several of them explain a real
hazard at length and were written for a human.

`Config` stays a hand-written frozen dataclass. It is the typed surface every
other module reads, and generating it from the table would cost more than it
saves. The drift that shape can grow — a key in the table with no field, or the
reverse — is closed by one test asserting the two agree.

That test is not a bijection, and the exceptions are named in it rather than
left to be rediscovered. `jira.project` and `jira.status` are config keys with
no `Config` field: `_jql` composes them into `jira.jql` and they are not
carried further. `home` is the reverse — a `Config` field that is a
`load_config` parameter and never a config key. Everything else maps one to
one.

**Acknowledged cost.** Expressing four conditional-requirement rules as
callables in a table is more machinery than four `if` statements. It is bought
deliberately: it is what stops the wizard's notion of a valid config drifting
from the loader's, which would otherwise be two places to update for every key
added after this.

### 2. Setup mode

`python -m claudeloop` with no config file, or `python -m claudeloop --setup`
with one, calls `setup.run_setup()`. It blocks until a valid `config.toml` has
been written, then returns, and `main()` falls through into the existing
startup path unchanged: `load_config` → `worktree.probe` → `_serve_dashboard`
→ `main_loop`. The config the loop runs is the one the ordinary loader read
back off disk, never the wizard's own in-memory parse — so a file the wizard
could write but the loader would reject is impossible to run.

Today `main()` exits with `no config file at ... -- see README.md`. That
message goes; the wizard is the answer to it now.

`--setup` is parsed with `argparse` — one flag, and a usage error for a typo
comes free.

**Security.** Setup mode configures an agent that will run with bypassed
permissions against real credentials, so it gets two independent barriers, and
they are independent on purpose: with no config there is no `web_token` to
authenticate against, so the network barrier cannot be the only one.

- Binds `127.0.0.1` unconditionally, ignoring `web_host` in an existing config.
- A one-time `secrets.token_urlsafe(32)`, printed to the console at startup and
  required on every request, page load included.
- `setup.Handler` subclasses `web.Handler`, inheriting `_host_allowed`,
  `_authorized`, `_json`/`_body`, and — the load-bearing one —
  `do_POST`'s `self.close_connection = True`. That line is the request-smuggling
  fix documented in `CLAUDE.md`; a hand-rolled second handler is exactly how a
  project loses it. The handler carries a synthetic
  `Config(web_host="127.0.0.1", web_token=<one-time token>)` so both inherited
  guards work with no modification.
- Every POST requires `Content-Type: application/json`, the same CSRF barrier
  the answer route uses.
- The file is written with `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)`, not
  at the umask, and an existing file is `chmod`ed to 600 — otherwise the loader
  would refuse to read back what the wizard just wrote.

**Routes.**

| Route | Purpose |
|---|---|
| `GET /` | `static/setup.html` |
| `GET /api/setup/schema` | the field table as JSON, plus current values with secrets masked |
| `POST /api/setup/validate` | a draft in, `{key: message}` out |
| `POST /api/setup/test` | one live check: `repo`, `jira` or `claude` |
| `POST /api/setup/save` | validate, write, shut down, return |

**Writing TOML.** The stdlib reads TOML and does not write it, and no
third-party package may be added, so `setup.py` emits it: top-level keys in
schema order, then `[jira]`, then `[session_env]`. Strings go through
`json.dumps` — JSON's escape set is a subset of what a TOML basic string
accepts — and bools and numbers are emitted bare. Each key's `help` is emitted
above it as a `#` comment, so an operator who does open the file by hand gets a
documented one at no extra cost. A test round-trips every config the wizard can
produce back through `tomllib` and `load_config`.

### 3. The wizard

`static/setup.html`, a second no-build file alongside `index.html`, which is
untouched.

Six screens: **Repository** → **Task source** → **Dashboard** →
**Instructions** → **Advanced** → **Review and save**. Each field's `step`
decides where it lands. `Next` POSTs the whole accumulated draft to
`/api/setup/validate`; only errors on the current screen's fields block
advancing, so a later screen's unfilled requirement never traps the operator on
an earlier one. `Save` re-validates everything. On the `--setup` path the step
list is also directly clickable — changing one key should not be a five-click
walk.

The Task source screen shows the `[jira]` block only when `source = "jira"`,
and within it leads with the `project`/`status` shorthand, keeping `jql` behind
a disclosure. That ordering matches `_jql`'s own reasoning: writing JQL by hand
is a barrier, getting it subtly wrong yields a silently empty backlog, and an
explicit `jql` still wins outright when it is given.

**Secrets.** `secret` fields (`web_token`, `jira.token`, every `[session_env]`
value) are never sent to the browser. They come back blank, marked *set — leave
blank to keep*, and the server merges the stored value back in on save when the
field is submitted empty. The wizard is exactly the screen an operator
screenshots when asking for help, and under S4 it is reached through Home
Assistant ingress, which logs. The dashboard token already leaks through the
query string — a known open issue — and this slice does not add a second leak.

**Live checks**, run from `/api/setup/test` on an explicit button rather than on
every `Next`, since two of them are slow:

- **`worktree.probe(repo)`** — already exists and already runs at startup.
  Calling it here turns *git is too old* or *no default branch* into a message
  while the operator is still looking at the form.
- **Jira** — one authenticated `GET` against the configured site with the
  composed JQL, reporting the matching issue count or the exact error. S3's live
  smoke test established that a bad token, an unreachable site and a JQL Jira
  rejects all present identically as an empty backlog, forever, with nothing
  saying why. This is the only place that failure is ever visible before it
  costs a night.
- **`claude auth status --json`** — a real non-interactive probe, returning
  `{"loggedIn": ..., "authMethod": ..., "apiProvider": ..., "subscriptionType":
  ...}`. Run with `[session_env]` applied, so it reports what a session would
  actually get: a stray `ANTHROPIC_API_KEY` in that table shows up here as a
  changed `authMethod`, which is the README's quietest foot-gun — it moves the
  session off subscription billing, and the `rate_limit_event`s the entire
  recovery path is built on simply stop arriving.

**After save.** A panel with the dashboard's URL, not an automatic redirect.
The setup server is shutting down at the moment the dashboard binds, so a
redirect races it; and a redirect carrying a freshly-set `web_token` in its
query string would write that token into browser history on the spot.

## Testing

- `test_config` — the schema walk, every condition in the table above,
  multi-error collection, and the `Config`/table parity test.
- `test_setup` (new) — the TOML emitter round-tripping through `tomllib` and
  `load_config`; the one-time token and `Host` guards; the `Content-Type`
  guard; 0600 on both a fresh and a pre-existing file; blank-secret-keeps-the-
  stored-value; per-field error keying; and a same-socket request-smuggling
  regression, matching the one `test_web` already carries for the answer route.

## The live smoke test

Not optional, and this slice's is unusually load-bearing: the wizard's entire
claim is that the config it writes starts a loop.

Drive the real wizard in a browser from nothing — no `config.toml` on disk —
against a scratch repository with `model = "haiku"`, run all three live checks,
save, and let the loop it hands off to run **two** tasks. Then re-enter with
`--setup`, change one key, confirm the secrets survive being left blank, and
confirm the loop starts again on the edited config.

## Open questions

None. Everything above was settled in brainstorming.

## What this does not change

- The dashboard stays read-only apart from the answer route. Setup mode is a
  separate server that only runs while the loop does not.
- Nothing here writes into the target repository.
- No third-party package, no build step.
