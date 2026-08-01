# ClaudeLoop Session Environment (S1.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator control how sessions run independently of the repository they run against — what instructions they carry, what "done" means when the repository does not say, which plugins and MCP servers they load, and which credentials reach them.

**Architecture:** A new pure module `claudeloop/prompt.py` composes `--append-system-prompt` from three layers with a stated precedence. `config.py` gains five optional keys, a `[session_env]` table, and a refusal to load a world-readable secrets file. `session.py` calls the composer and passes the new flags. Nothing else in the project changes.

**Tech Stack:** Python 3.11+ standard library only.

## Global Constraints

- **Python 3.11 or newer.**
- **No third-party packages, ever.** `pip install` must never be required.
- **Every new configuration key is optional.** A config omitting all of them must produce exactly today's behaviour — existing configs stay valid and untouched.
- **`CLAUDELOOP_RESULT` is merged last into the child environment**, so a misconfigured `session_env` can never redirect the result file and break the loop's completion detection.
- **Precedence is stated in the prompt as text**, not left implicit: protocol outranks everything, operator layer outranks the repository, repository is the base.
- Tests run as `python -m unittest discover -s tests -t .` from the repository root. The suite stands at **165 tests** before this plan and should stand at **197** after it.
- Reference spec: `docs/superpowers/specs/2026-08-01-claudeloop-session-environment-design.md`.

## Deviations from the spec

1. **`compose(cfg)` takes only the config, not `compose(cfg, repo)`.** The repository path is already `cfg.repo`; passing it separately creates two sources of truth for the same value and invites them to disagree.
2. **The environment merge is extracted as `child_env(cfg, run_dir)`.** The spec describes it inline in `run()`, but `run()` spawns a subprocess and cannot be tested without one. A pure function makes the `CLAUDELOOP_RESULT`-wins rule directly assertable.

## File Structure

| File | Responsibility |
|---|---|
| `claudeloop/prompt.py` | `PROTOCOL`, the built-in definition of done, the precedence text, and `compose()`. Pure. |
| `claudeloop/config.py` | *Modify:* five optional keys, `[session_env]`, the permissions refusal, the `strict_mcp` guard. |
| `claudeloop/session.py` | *Modify:* `PROTOCOL` moves out; `build_command` composes and adds flags; `child_env` extracted. |
| `tests/test_config.py` | *Modify:* append a class for the new keys and the permissions rule. |
| `tests/test_prompt.py` | Every branch of `compose()`. |
| `tests/test_session.py` | *Modify:* two existing assertions change; append a class for the new flags and the env merge. |

---

### Task 1: Configuration keys and the secrets-file guard

**Files:**
- Modify: `claudeloop/config.py`
- Modify: `tests/test_config.py` (append a class; leave the existing ones alone)

**Interfaces:**
- Consumes: the existing `Config` and `load_config`.
- Produces: `Config` gains `instructions_file: Path | None = None`, `definition_of_done_file: Path | None = None`, `settings_file: Path | None = None`, `mcp_config: Path | None = None`, `strict_mcp: bool = False`, `session_env: dict[str, str]` (default empty).

**Design notes for the implementer:**

1. **The two instruction paths default to files under `home`**, resolved in `load_config`. The dataclass default stays `None` so that a `Config(...)` built directly in a test — which many existing tests do — means "no such file" rather than crashing on a path that does not exist.
2. **`strict_mcp` without `mcp_config` is refused.** `--strict-mcp-config` alone tells the CLI to use only the servers from `--mcp-config`, of which there would be none, silently disabling every MCP server the box has configured. That is never what anyone means.
3. **The permissions check is POSIX-only** and looks at group and other bits. `config.toml` already holds `web_token` and will hold `jira_token` and `[session_env]` credentials.
4. **`session_env` is a `dict` on a frozen dataclass**, which makes `Config` unhashable. Nothing in the project hashes it. Do not work around this by inventing a tuple-of-pairs representation — the awkwardness at every call site costs more than the property is worth.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`, before the `if __name__` block:

```python
class SessionEnvironmentConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.home = self.tmp / "home"

    def write(self, extra: str = "", mode: int = 0o600) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(
            f'repo = "{self.repo}"\n'
            f'tasks_file = "{self.tmp}/tasks.md"\n' + extra
        )
        path.chmod(mode)
        return path

    def test_instruction_paths_default_under_home(self):
        cfg = load_config(self.write(), home=self.home)
        self.assertEqual(cfg.instructions_file, self.home / "instructions.md")
        self.assertEqual(cfg.definition_of_done_file, self.home / "definition-of-done.md")

    def test_instruction_paths_can_be_overridden(self):
        cfg = load_config(
            self.write(
                f'instructions_file = "{self.tmp}/mine.md"\n'
                f'definition_of_done_file = "{self.tmp}/dod.md"\n'
            ),
            home=self.home,
        )
        self.assertEqual(cfg.instructions_file, self.tmp / "mine.md")
        self.assertEqual(cfg.definition_of_done_file, self.tmp / "dod.md")

    def test_plugin_and_mcp_keys_default_to_unset(self):
        cfg = load_config(self.write(), home=self.home)
        self.assertIsNone(cfg.settings_file)
        self.assertIsNone(cfg.mcp_config)
        self.assertFalse(cfg.strict_mcp)

    def test_plugin_and_mcp_keys_are_read(self):
        cfg = load_config(
            self.write(
                f'settings_file = "{self.tmp}/settings.json"\n'
                f'mcp_config = "{self.tmp}/mcp.json"\n'
                "strict_mcp = true\n"
            ),
            home=self.home,
        )
        self.assertEqual(cfg.settings_file, self.tmp / "settings.json")
        self.assertEqual(cfg.mcp_config, self.tmp / "mcp.json")
        self.assertTrue(cfg.strict_mcp)

    def test_strict_mcp_without_mcp_config_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write("strict_mcp = true\n"), home=self.home)
        self.assertIn("mcp_config", str(caught.exception))

    def test_session_env_defaults_empty(self):
        self.assertEqual(load_config(self.write(), home=self.home).session_env, {})

    def test_session_env_is_read_as_strings(self):
        cfg = load_config(
            self.write(
                "[session_env]\n"
                'GH_TOKEN = "ghp_abc"\n'
                'GIT_CONFIG_COUNT = 1\n'
            ),
            home=self.home,
        )
        self.assertEqual(cfg.session_env, {"GH_TOKEN": "ghp_abc", "GIT_CONFIG_COUNT": "1"})

    def test_session_env_rejects_a_nested_table(self):
        with self.assertRaises(ValueError) as caught:
            load_config(
                self.write("[session_env.nested]\nA = \"b\"\n"), home=self.home
            )
        self.assertIn("session_env", str(caught.exception))

    def test_a_group_readable_config_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            load_config(self.write(mode=0o640), home=self.home)
        message = str(caught.exception)
        self.assertIn("chmod 600", message)

    def test_a_world_readable_config_is_refused(self):
        with self.assertRaises(ValueError):
            load_config(self.write(mode=0o644), home=self.home)

    def test_an_owner_only_config_is_accepted(self):
        cfg = load_config(self.write(mode=0o600), home=self.home)
        self.assertEqual(cfg.repo, self.repo)
```

Note that `ConfigTest` and `WebConfigTest` already in this file write their
config files without setting a mode. `tempfile.mkdtemp()` creates a `0o700`
directory, and files created inside it inherit the process umask — commonly
`0o644`, which the new rule would reject and break those existing tests. Fix
that in Step 3 by having those two classes' `write` helpers `chmod(0o600)`
as well; do not weaken the rule to accommodate them.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_config -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'instructions_file'`

- [ ] **Step 3: Write the implementation**

In `claudeloop/config.py`, add to the imports:

```python
import os
from dataclasses import dataclass, field
```

add these fields to `Config`, after `web_token` and before `home`:

```python
    instructions_file: Path | None = None
    definition_of_done_file: Path | None = None
    settings_file: Path | None = None
    mcp_config: Path | None = None
    strict_mcp: bool = False
    session_env: dict[str, str] = field(default_factory=dict)
```

add this helper above `load_config`:

```python
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


def _session_env(data: dict, path: Path) -> dict[str, str]:
    table = data.get("session_env", {})
    if not isinstance(table, dict):
        raise ValueError(f"{path}: session_env must be a table of name = \"value\"")
    result = {}
    for name, value in table.items():
        if isinstance(value, (dict, list)):
            raise ValueError(
                f"{path}: session_env.{name} must be a single value, not a"
                " table or array — environment variables are strings"
            )
        result[str(name)] = str(value)
    return result


def _optional_path(data: dict, key: str) -> Path | None:
    raw = data.get(key)
    return Path(str(raw)).expanduser() if raw else None
```

then in `load_config`, immediately after the `with open(path, "rb") as handle:`
block that loads the TOML — the guard reads the file's mode, so it can run
either side of the parse, but running it after means a syntactically broken
config reports that first:

```python
    _secrets_file_guard(path)
```

and before the `return`, after the existing `web_*` handling:

```python
    mcp_config = _optional_path(data, "mcp_config")
    strict_mcp = bool(data.get("strict_mcp", False))
    if strict_mcp and mcp_config is None:
        raise ValueError(
            f"{path}: strict_mcp is set but mcp_config is not. On its own,"
            " --strict-mcp-config tells the CLI to use only the servers from"
            " --mcp-config — of which there would be none — silently disabling"
            " every MCP server this machine has configured."
        )
```

and add to the `Config(...)` call, after `web_token=web_token`:

```python
        instructions_file=_optional_path(data, "instructions_file")
        or home / "instructions.md",
        definition_of_done_file=_optional_path(data, "definition_of_done_file")
        or home / "definition-of-done.md",
        settings_file=_optional_path(data, "settings_file"),
        mcp_config=mcp_config,
        strict_mcp=strict_mcp,
        session_env=_session_env(data, path),
```

Finally, in the existing `ConfigTest.write` and `WebConfigTest.write` helpers in
`tests/test_config.py`, add `path.chmod(0o600)` before the `return path`, so
those pre-existing tests satisfy the new rule.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_config -v`
Expected: PASS, 21 tests.

- [ ] **Step 5: Run the whole suite**

Run: `python -m unittest discover -s tests -t .`
Expected: PASS, 176 tests.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/config.py tests/test_config.py
git commit -m "feat: session environment config keys and a secrets-file guard"
```

---

### Task 2: The prompt composer

**Files:**
- Create: `claudeloop/prompt.py`
- Create: `tests/test_prompt.py`

**Interfaces:**
- Consumes: `Config` (Task 1), specifically `repo`, `instructions_file`, `definition_of_done_file`.
- Produces: `compose(cfg: Config) -> str`, `repo_claude_md(repo: Path) -> Path | None`, and the constants `PROTOCOL`, `PRECEDENCE`, `BUILTIN_DEFINITION_OF_DONE`, `CLAUDE_MD_NAMES`.

**Design notes for the implementer:**

1. **`PROTOCOL` moves here from `session.py`, and loses its `CLAUDE.md` sentence.** Today it reads "Follow this repository's CLAUDE.md end to end — it defines what 'done' means here". That responsibility now belongs to the definition-of-done layer, which points at the repository's file only when one exists. Leaving the sentence in the protocol would tell a session with no `CLAUDE.md` to follow a file that isn't there.
2. **A missing or unreadable file is "absent", not an error.** `OSError` is swallowed. The operator layer is optional by design, and a session must not fail to start because a file was renamed.
3. **The repository's `CLAUDE.md` outranks `definition_of_done_file`.** The configured file is the fallback for repositories that do not document themselves, not an override of ones that do.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_prompt.py`:

```python
import tempfile
import unittest
from pathlib import Path

from claudeloop.config import Config
from claudeloop.prompt import (
    BUILTIN_DEFINITION_OF_DONE,
    PRECEDENCE,
    PROTOCOL,
    compose,
    repo_claude_md,
)


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)

    def cfg(self, **overrides) -> Config:
        base = {
            "repo": self.repo,
            "tasks_file": self.tmp / "tasks.md",
            "home": self.tmp / "home",
        }
        return Config(**{**base, **overrides})

    def test_protocol_and_precedence_are_always_present(self):
        text = compose(self.cfg())
        self.assertIn(PROTOCOL, text)
        self.assertIn(PRECEDENCE, text)

    def test_protocol_still_names_the_result_contract(self):
        for token in ("CLAUDELOOP_RESULT", "done", "failed", "blocked"):
            self.assertIn(token, PROTOCOL)

    def test_protocol_no_longer_names_claude_md(self):
        # That sentence moved to the definition-of-done layer, which only
        # points at the repository's file when the repository has one.
        self.assertNotIn("CLAUDE.md", PROTOCOL)

    def test_a_repo_claude_md_is_pointed_at(self):
        (self.repo / "CLAUDE.md").write_text("# rules")
        text = compose(self.cfg())
        self.assertIn(str(self.repo / "CLAUDE.md"), text)
        self.assertNotIn(BUILTIN_DEFINITION_OF_DONE, text)

    def test_a_dot_claude_claude_md_is_found(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "CLAUDE.md").write_text("# rules")
        self.assertEqual(
            repo_claude_md(self.repo), self.repo / ".claude" / "CLAUDE.md"
        )

    def test_no_claude_md_anywhere_returns_none(self):
        self.assertIsNone(repo_claude_md(self.repo))

    def test_the_builtin_is_used_when_repo_and_file_are_both_silent(self):
        self.assertIn(BUILTIN_DEFINITION_OF_DONE, compose(self.cfg()))

    def test_the_builtin_covers_the_no_remote_case(self):
        self.assertIn("no remote", BUILTIN_DEFINITION_OF_DONE)

    def test_a_definition_of_done_file_wins_over_the_builtin(self):
        dod = self.tmp / "dod.md"
        dod.write_text("Done means the customer said so.")
        text = compose(self.cfg(definition_of_done_file=dod))
        self.assertIn("Done means the customer said so.", text)
        self.assertNotIn(BUILTIN_DEFINITION_OF_DONE, text)

    def test_a_repo_claude_md_wins_over_the_definition_of_done_file(self):
        (self.repo / "CLAUDE.md").write_text("# rules")
        dod = self.tmp / "dod.md"
        dod.write_text("Done means the customer said so.")
        text = compose(self.cfg(definition_of_done_file=dod))
        self.assertIn(str(self.repo / "CLAUDE.md"), text)
        self.assertNotIn("Done means the customer said so.", text)

    def test_operator_instructions_are_included(self):
        instructions = self.tmp / "mine.md"
        instructions.write_text("Never push to main.")
        self.assertIn("Never push to main.", compose(self.cfg(instructions_file=instructions)))

    def test_there_is_no_operator_layer_when_the_file_is_absent(self):
        text = compose(self.cfg(instructions_file=self.tmp / "nope.md"))
        self.assertNotIn("Operator instructions", text)

    def test_an_empty_operator_file_produces_no_layer(self):
        instructions = self.tmp / "mine.md"
        instructions.write_text("   \n\n")
        self.assertNotIn("Operator instructions", compose(self.cfg(instructions_file=instructions)))

    def test_none_paths_are_treated_as_absent(self):
        text = compose(self.cfg(instructions_file=None, definition_of_done_file=None))
        self.assertIn(BUILTIN_DEFINITION_OF_DONE, text)
        self.assertNotIn("Operator instructions", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_prompt -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claudeloop.prompt'`

- [ ] **Step 3: Write the implementation**

Create `claudeloop/prompt.py`:

```python
"""Compose the system prompt a session carries.

Three layers with a stated precedence: ClaudeLoop's own protocol, which is
invariant; the operator's instructions, which outrank the repository because
the operator runs the machine; and the definition of done, which is the
repository's own CLAUDE.md when it has one. Pure, so every combination is
testable without spawning anything.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config

PROTOCOL = (
    "You are running unattended under ClaudeLoop. Nobody is watching, so "
    "decide open questions yourself rather than waiting. When the task is "
    "fully complete, or provably cannot be completed, write a JSON object to "
    "the path in the CLAUDELOOP_RESULT environment variable with keys "
    "\"status\" (one of \"done\", \"failed\", \"blocked\"), \"summary\" (one "
    "paragraph on what you did), and, when blocked, \"question\" (the one "
    "thing a human must answer). Writing that file is what ends the task; do "
    "not stop without it."
)

PRECEDENCE = (
    "These instructions are layered. The ClaudeLoop protocol above is "
    "invariant and overrides everything below it. The operator instructions "
    "outrank the repository's own documentation. The definition of done is "
    "the base. Where two layers conflict, follow the higher one and say so in "
    "your summary."
)

BUILTIN_DEFINITION_OF_DONE = (
    "Done means: the change is implemented; the repository's tests pass; the "
    "work is committed on a branch; and a pull request is open. If the "
    "repository has no remote configured, stop after committing and say so in "
    "your summary."
)

CLAUDE_MD_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md")


def repo_claude_md(repo: Path) -> Path | None:
    """The repository's own instructions file, if it has one."""
    for name in CLAUDE_MD_NAMES:
        candidate = repo / name
        if candidate.exists():
            return candidate
    return None


def _read(path: Path | None) -> str:
    """A missing or unreadable file is 'absent', not an error: these layers
    are optional, and a session must not fail to start over a rename."""
    if path is None:
        return ""
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def compose(cfg: Config) -> str:
    parts = [PROTOCOL, PRECEDENCE]

    operator = _read(cfg.instructions_file)
    if operator:
        parts.append(f"## Operator instructions\n\n{operator}")

    claude_md = repo_claude_md(cfg.repo)
    if claude_md is not None:
        # The repository documents itself; point at it rather than imposing
        # a definition of done over the top of one it already has.
        parts.append(
            "## Definition of done\n\nThis repository has its own instructions "
            f"at {claude_md}. Follow that file end to end — it defines what "
            "\"done\" means here, including its testing and verification "
            "requirements."
        )
    else:
        parts.append(
            "## Definition of done\n\n"
            + (_read(cfg.definition_of_done_file) or BUILTIN_DEFINITION_OF_DONE)
        )

    return "\n\n".join(parts)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m unittest tests.test_prompt -v`
Expected: PASS, 14 tests.

- [ ] **Step 5: Commit**

```bash
git add claudeloop/prompt.py tests/test_prompt.py
git commit -m "feat: layered system prompt with a definition of done"
```

---

### Task 3: Wire the session

**Files:**
- Modify: `claudeloop/session.py`
- Modify: `tests/test_session.py` (two existing assertions change; append a class)

**Interfaces:**
- Consumes: `compose(cfg)` and `PROTOCOL` from `claudeloop.prompt` (Task 2); the new `Config` fields (Task 1).
- Produces: `child_env(cfg: Config, run_dir: Path) -> dict[str, str]` in `session.py`. `session.PROTOCOL` ceases to exist — it lives in `prompt.py` now.

**Design notes for the implementer:**

1. **Two existing tests in `tests/test_session.py` assert against `session.PROTOCOL` and must be updated, not deleted.** `test_carries_the_flags_the_loop_depends_on` asserts the `--append-system-prompt` value equals `session.PROTOCOL`; it becomes `compose(cfg)`. `test_protocol_names_the_result_variable_and_every_status` asserts `"CLAUDE.md"` appears in `session.PROTOCOL`; that assertion moved to `tests/test_prompt.py` in Task 2, so delete this test here rather than weakening it.
2. **`child_env` exists so the merge order is testable.** `run()` spawns a subprocess and cannot assert on the environment it built; a pure function can.
3. **Flags appear only when their key is set.** An unconfigured ClaudeLoop must produce a command line byte-identical to today's apart from the composed prompt.

- [ ] **Step 1: Write the failing tests**

In `tests/test_session.py`, replace the body of `test_carries_the_flags_the_loop_depends_on`'s final assertion — the line reading
`self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], session.PROTOCOL)` — with:

```python
        self.assertEqual(
            cmd[cmd.index("--append-system-prompt") + 1], compose(self.cfg)
        )
```

delete `test_protocol_names_the_result_variable_and_every_status` entirely (Task 2
covers it in `tests/test_prompt.py`), add to the imports at the top of the file:

```python
from claudeloop.prompt import compose
```

and append this class before the `if __name__` block:

```python
class SessionEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repo" / ".git").mkdir(parents=True)
        self.run_dir = self.tmp / "run"

    def cfg(self, **overrides) -> Config:
        base = {
            "repo": self.tmp / "repo",
            "tasks_file": self.tmp / "tasks.md",
            "home": self.tmp / "home",
        }
        return Config(**{**base, **overrides})

    def test_no_new_flags_when_nothing_is_configured(self):
        cmd = session.build_command(self.cfg(), "uuid-1", "do it", resume=False)
        self.assertNotIn("--settings", cmd)
        self.assertNotIn("--mcp-config", cmd)
        self.assertNotIn("--strict-mcp-config", cmd)

    def test_settings_flag_only_when_set(self):
        cfg = self.cfg(settings_file=self.tmp / "settings.json")
        cmd = session.build_command(cfg, "uuid-1", "do it", resume=False)
        self.assertEqual(cmd[cmd.index("--settings") + 1], str(self.tmp / "settings.json"))

    def test_mcp_config_flag_only_when_set(self):
        cfg = self.cfg(mcp_config=self.tmp / "mcp.json")
        cmd = session.build_command(cfg, "uuid-1", "do it", resume=False)
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], str(self.tmp / "mcp.json"))

    def test_strict_mcp_flag_only_when_set(self):
        cfg = self.cfg(mcp_config=self.tmp / "mcp.json", strict_mcp=True)
        cmd = session.build_command(cfg, "uuid-1", "do it", resume=False)
        self.assertIn("--strict-mcp-config", cmd)

    def test_the_composed_prompt_is_what_is_sent(self):
        (self.tmp / "repo" / "CLAUDE.md").write_text("# rules")
        cfg = self.cfg()
        cmd = session.build_command(cfg, "uuid-1", "do it", resume=False)
        sent = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertEqual(sent, compose(cfg))
        self.assertIn("CLAUDE.md", sent)

    def test_session_env_reaches_the_child(self):
        cfg = self.cfg(session_env={"GH_TOKEN": "ghp_abc"})
        self.assertEqual(session.child_env(cfg, self.run_dir)["GH_TOKEN"], "ghp_abc")

    def test_the_ambient_environment_is_preserved(self):
        env = session.child_env(self.cfg(), self.run_dir)
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_claudeloop_result_wins_over_session_env(self):
        cfg = self.cfg(session_env={"CLAUDELOOP_RESULT": "/tmp/hijacked.json"})
        env = session.child_env(cfg, self.run_dir)
        self.assertEqual(env["CLAUDELOOP_RESULT"], str(self.run_dir / "result.json"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m unittest tests.test_session -v`
Expected: FAIL with `AttributeError: module 'claudeloop.session' has no attribute 'child_env'`

- [ ] **Step 3: Write the implementation**

In `claudeloop/session.py`, delete the entire `PROTOCOL = (...)` assignment and
add to the imports:

```python
from .prompt import compose
```

replace `build_command` with:

```python
def build_command(cfg: Config, session_id: str, prompt: str, resume: bool) -> list[str]:
    command = ["claude", "-p", prompt]
    # --resume and --session-id are alternative ways to name the session;
    # passing both is a conflict.
    command += ["--resume", session_id] if resume else ["--session-id", session_id]
    command += [
        "--append-system-prompt", compose(cfg),
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "bypassPermissions",
        "--model", cfg.model,
    ]
    # Each of these appears only when configured, so an unconfigured
    # ClaudeLoop produces the same command line it always did.
    if cfg.settings_file:
        command += ["--settings", str(cfg.settings_file)]
    if cfg.mcp_config:
        command += ["--mcp-config", str(cfg.mcp_config)]
    if cfg.strict_mcp:
        command += ["--strict-mcp-config"]
    return command


def child_env(cfg: Config, run_dir: Path) -> dict[str, str]:
    """The environment the session runs in.

    CLAUDELOOP_RESULT is merged last on purpose: a misconfigured session_env
    must not be able to redirect the result file, which is the only thing the
    loop uses to decide a task is finished.
    """
    return (
        os.environ
        | dict(cfg.session_env)
        | {"CLAUDELOOP_RESULT": str(run_dir / "result.json")}
    )
```

and in `run()`, replace the line

```python
    env = os.environ | {"CLAUDELOOP_RESULT": str(run_dir / "result.json")}
```

with

```python
    env = child_env(cfg, run_dir)
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m unittest discover -s tests -t . -v`
Expected: PASS, 197 tests.

- [ ] **Step 5: Update the README**

`README.md` documents the config block but knows nothing about these keys. Add
them to that block and a short section explaining the three instruction layers,
their precedence, that a repository without a `CLAUDE.md` gets a built-in
definition of done, and that `config.toml` must be `chmod 600` because it holds
secrets.

- [ ] **Step 6: Commit**

```bash
git add claudeloop/session.py tests/test_session.py README.md
git commit -m "feat: compose the session prompt and pass plugin, mcp and env config"
```

---

## Manual smoke test

After Task 3. This is the case that does not work today.

- [ ] Create a scratch git repository with **no** `CLAUDE.md` and **no** remote.
- [ ] Point a `chmod 600` config at it with `model = "haiku"` and one trivial
      task, e.g. `- [ ] Add a LICENSE file (MIT, 2026 Kamil Postrozny)`.
- [ ] Run `python -m claudeloop`. The session should implement the change, run
      whatever tests exist, commit on a branch, and stop before opening a pull
      request — saying in its summary that there is no remote.
- [ ] Add `~/.claudeloop/instructions.md` containing a rule that visibly changes
      behaviour, e.g. "Every commit message must start with `[cl]`." Run a second
      task and confirm the rule was followed.
- [ ] `chmod 644` the config and confirm ClaudeLoop refuses to start, naming
      `chmod 600`.

## Spec coverage

| Spec requirement | Task |
|---|---|
| Three layers with a stated precedence | 2 |
| Protocol invariant, operator outranks repo, repo is base | 2 |
| Repo `CLAUDE.md` or `.claude/CLAUDE.md` detected | 2 |
| Built-in definition of done, with the no-remote caveat | 2 |
| `definition_of_done_file` overrides the built-in, repo overrides both | 2 |
| Operator layer optional, absent when the file is | 2 |
| `instructions_file` / `definition_of_done_file` default under `home` | 1 |
| `settings_file`, `mcp_config`, `strict_mcp` | 1, 3 |
| `strict_mcp` without `mcp_config` refused | 1 |
| `[session_env]` table, values as strings | 1 |
| `CLAUDELOOP_RESULT` merged last | 3 |
| Config refused when readable beyond its owner | 1 |
| Every key optional; existing configs unaffected | Global constraints; 1, 3 |
| No forge-specific knowledge | Nothing implements it — deliberately |
| Acceptance criteria 1–7 | Task 3 tests + the manual smoke test |
