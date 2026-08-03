# ClaudeLoop — S1.1 Session Environment — Design

Date: 2026-08-01
Status: approved, ready for implementation planning

## Goal

Give the operator control over how ClaudeLoop's sessions run, independently of
the repository they run against: what instructions they carry, what "done"
means when the repository does not say, which plugins and MCP servers they
have, and which credentials reach them.

## Why this exists

`session.PROTOCOL` today says "follow this repository's CLAUDE.md end to end"
and nothing more. Two gaps follow.

**A repository without a `CLAUDE.md` gives the session no definition of done.**
The sentinel-file contract still holds, so the loop terminates, but "done"
means whatever the model decided it meant. ClaudeLoop currently only works
properly against repositories that already document their own workflow.

**There is no way to impose anything across every task without editing the
target repository.** A repository's `CLAUDE.md` is shared with its
contributors; the policy governing an unattended agent on a particular machine
is not. They are different documents with different audiences.

This slice was pulled ahead of S3 because S3's session needs to be told it can
talk to Jira, and that instruction belongs in a layer that should exist first
rather than being invented ad hoc.

## Scope

Session environment only: the prompt the session carries, the plugins and MCP
servers it loads, and the environment variables it inherits. No change to the
loop, the state machine, the dashboard, or the task sources.

## Decisions

### Three instruction layers, with a stated precedence

`--append-system-prompt` is composed from three parts:

| Layer | Source | Present |
|---|---|---|
| **Protocol** | built into ClaudeLoop | always; never overridable |
| **Operator** | `instructions_file` | when the file exists |
| **Definition of done** | the repository's `CLAUDE.md`, or `definition_of_done_file`, or a built-in | always, one of the three |

The precedence is written into the composed prompt as text, not left implicit:
the protocol outranks everything, the operator layer outranks the repository,
and the repository is the base. An unattended session must not be left
reconciling a contradiction on its own, and the operator — who runs the
machine — needs to be able to impose a rule tighter than the repository's own
documentation.

Configuration is already one repository per instance (S1), so the operator
layer is per-repository without any additional scoping mechanism.

### What each layer is for

The definition of done answers *when am I finished* and is a property of the
work. The operator layer answers *how do I operate on this machine* and is a
property of the deployment. Concretely, the operator layer carries:

- **Environment facts the repository cannot know.** `assimo/CLAUDE.md` mandates
  a Chrome verification sweep; on the S4 Home Assistant box Claude-in-Chrome
  does not exist, because it is a browser extension. "You are running headless,
  use Playwright for the browser verification" is true of that deployment, not
  of the repository, and must not go into a file the repository's contributors
  share.
- **Policy tighter than the repository's.** `assimo/CLAUDE.md` pre-authorises
  `git push origin main` because CI deploys from it. An operator running
  unattended overnight may want pull requests instead. This is why the operator
  layer outranks the repository.
- **ClaudeLoop's own capabilities.** S3 will add "you can read and post Jira
  comments with `python -m claudeloop.jira`". That is true of every repository
  ClaudeLoop runs against and belongs nowhere near a repository's own docs.
- **Cost and escalation limits**, which apply regardless of what finished means.

### Definition of done: built-in, overridable

If the repository has `CLAUDE.md` or `.claude/CLAUDE.md`, the prompt points at
it, exactly as today. Otherwise ClaudeLoop injects the contents of
`definition_of_done_file`, and if that file does not exist, this built-in:

> Done means: the change is implemented; the repository's tests pass; the work
> is committed on a branch; and a pull request is open. If the repository has
> no remote configured, stop after committing and say so in your summary.

The remote caveat matters: "create a pull request" is unreachable in a
repository with no remote, and without the caveat a scratch repository would
fail at the last step having done all the work.

The file is named `definition-of-done.md` rather than anything involving the
word "fallback", because the file's name should say what it contains.

### Plugins and MCP: pass through, do not manage

Four configuration keys mapping directly onto flags the CLI already has. No
plugin installation, no trust management, no marketplace handling — the box's
own Claude configuration owns all of that.

> **Reversed in part, 2026-08-03.** S7 reverses the installation half of this
> decision: ClaudeLoop now installs and enables the plugins it proposes, at
> user scope, rather than assuming the box already carries them. `settings_file`
> passthrough, described below, is untouched. See
> `docs/superpowers/specs/2026-08-03-claudeloop-plugin-set-design.md`.

`settings_file` is the significant one: a settings JSON carries
`enabledPlugins`, `extraKnownMarketplaces`, hooks and permissions together, the
same shape as `assimo/.claude/settings.json`. One key therefore covers plugin
control without a configuration key per plugin.

### Credentials: an environment table

`session.run` already builds `env = os.environ | {"CLAUDELOOP_RESULT": ...}`.
A configured `[session_env]` table merges into it.

This is what makes the built-in definition of done reachable. Opening a pull
request needs push credentials and a forge CLI, each with its own token.
ClaudeLoop currently inherits whatever the box happens to have — on a
developer's machine an SSH agent and an authenticated `gh`, on the S4 box
nothing at all.

`CLAUDELOOP_RESULT` is merged **last**, so a misconfigured `session_env` cannot
redirect the result file and break the loop's completion detection.

Worked example, including the trio that forces commit signing off for the
session's git alone, without touching the repository's configuration or the
box's:

```toml
[session_env]
GH_TOKEN           = "ghp_..."
GIT_CONFIG_COUNT   = "1"
GIT_CONFIG_KEY_0   = "commit.gpgsign"
GIT_CONFIG_VALUE_0 = "false"
```

That last case is not hypothetical: a locked 1Password SSH agent blocked
commits twice during this project's own development, and it is a permanent
condition on a headless box rather than a transient one.

### No forge-specific knowledge

ClaudeLoop passes credentials and says nothing about which CLI to use. The
repository's `CLAUDE.md` or the operator layer names `gh`, `glab`, or anything
else. GitHub, GitLab, Gitea and Bitbucket all work on day one, and ClaudeLoop
holds no opinion it would have to keep current.

### Configuration is a secrets file

`config.toml` already holds `web_token` and will hold `jira_token` and now
`[session_env]`. Loading refuses to start when the file is readable by group or
others, with a message naming `chmod 600`. Checked on POSIX only.

## Architecture

### `claudeloop/prompt.py`

```python
def compose(cfg: Config, repo: Path) -> str
```

Pure: takes the configuration and the repository path, reads at most two files,
returns the composed system prompt. Tested directly against every combination,
the same way `loop.decide()` and `render.render_event()` are.

Module constants hold the protocol text and the built-in definition of done, so
both are inspectable and assertable from tests.

### `claudeloop/config.py`

Five new optional keys plus the environment table:

```toml
instructions_file       = "~/.claudeloop/instructions.md"
definition_of_done_file = "~/.claudeloop/definition-of-done.md"
settings_file           = "~/.claudeloop/settings.json"
mcp_config              = "~/.claudeloop/mcp.json"
strict_mcp              = false

[session_env]
GH_TOKEN = "ghp_..."
```

The two instruction paths default to `instructions.md` and
`definition-of-done.md` under `cfg.home`, so tests and alternate homes work
without special-casing — the `~/.claudeloop/` paths shown above are what those
defaults resolve to for the default `home`, not literals.

`strict_mcp` without `mcp_config` is a trap worth guarding: `--strict-mcp-config`
alone tells the CLI to use only the servers from `--mcp-config`, of which there
would be none, silently disabling every MCP server the box has configured.
Loading refuses that combination rather than shipping it. Every key is optional: a configuration omitting all of
them produces exactly today's behaviour, so existing configurations are
untouched.

`session_env` is a `dict[str, str]` on a frozen dataclass. That makes `Config`
unhashable, which nothing in the project relies on.

### `claudeloop/session.py`

`PROTOCOL` moves to `prompt.py`. `build_command` calls `compose()` for
`--append-system-prompt` and appends `--settings`, `--mcp-config` and
`--strict-mcp-config` when their keys are set. `run` merges `session_env` into
the child environment ahead of `CLAUDELOOP_RESULT`.

Nothing else in the project changes.

## Verification

`tests/test_prompt.py` — `compose()` across the matrix: repository with
`CLAUDE.md`, with `.claude/CLAUDE.md`, with neither; operator layer present and
absent; custom definition of done present and absent. The precedence statement
appears in all of them, and the built-in definition of done appears only when
both the repository and the file are silent.

`tests/test_config.py` — the new keys and their defaults, `session_env` parsing,
and the permissions refusal naming `chmod 600`.

`tests/test_session.py` — each flag appears only when its key is set;
`session_env` reaches the child environment; `CLAUDELOOP_RESULT` wins over a
`session_env` entry of the same name.

Then one live run against a scratch repository with **no** `CLAUDE.md` and no
remote — the case that does not work today. The session should implement, run
the tests, commit, and stop before the pull request, saying so in its summary.

## Out of scope

Per-task instruction overrides. Plugin installation and trust management. Git
identity or credential-helper configuration beyond passing environment
variables. Anything Jira — S3 slots its instructions into the operator layer
this builds.

## Acceptance criteria

- A repository with no `CLAUDE.md` runs to a sensible completion under the
  built-in definition of done, committing and explaining why it stopped short
  of a pull request when there is no remote.
- A repository with a `CLAUDE.md` behaves exactly as before.
- An operator instruction file reaches the session, and the composed prompt
  states that it outranks the repository.
- `settings_file`, `mcp_config` and `strict_mcp` produce their flags, and
  produce nothing when unset.
- A `[session_env]` entry is visible to the session; one named
  `CLAUDELOOP_RESULT` does not override the loop's own value.
- A `config.toml` readable by group or others is refused at startup with a
  message naming `chmod 600`.
- The full suite passes with no configuration key set, proving existing
  configurations are unaffected.
