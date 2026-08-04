# S10 — Repository-first prompt layering — TDD plan

Spec: [`../specs/2026-08-04-claudeloop-repo-first-prompt-design.md`](../specs/2026-08-04-claudeloop-repo-first-prompt-design.md)

Six steps. Each is red first, then green. Step 6 is the live smoke test, which
is where prompt changes are actually judged.

---

## Step 1 — The working-tree section exists and is always present

**Red** — `tests/test_prompt.py`, new `WorkingTreeSectionTest`:

```python
class WorkingTreeSectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.tree = self.tmp / "worktrees" / "abc123"
        self.tree.mkdir(parents=True)

    def cfg(self, **overrides):
        base = {"repo": self.repo, "tasks_file": self.tmp / "t.md",
                "home": self.tmp / "home"}
        return Config(**{**base, **overrides})

    def test_the_section_names_the_tree_and_the_default_branch(self):
        text = compose(self.cfg(), self.tree, default_branch="trunk")
        self.assertIn(str(self.tree), text)
        self.assertIn("trunk", text)

    def test_it_gives_both_publish_commands_naming_head(self):
        text = compose(self.cfg(), self.tree, default_branch="main")
        self.assertIn("git push origin HEAD:main", text)
        self.assertIn("git push -u origin HEAD", text)

    def test_it_warns_that_pushing_the_branch_name_ships_nothing(self):
        # The defect this whole slice exists for: `git push origin main` from
        # a worktree pushes main's own ref, reports "Everything up-to-date"
        # and exits 0, and a literal-minded session reads that as shipped.
        text = compose(self.cfg(), self.tree, default_branch="main")
        self.assertIn("git push origin main", text)
        self.assertIn("Everything up-to-date", text)
        self.assertIn("ships nothing", text)

    def test_it_leaves_the_choice_to_the_repository(self):
        text = compose(self.cfg(), self.tree, default_branch="main")
        self.assertIn("this repository's decision, not ClaudeLoop's", text)

    def test_it_is_present_whether_or_not_the_repo_documents_itself(self):
        (self.tree / "CLAUDE.md").write_text("# fully documented\n")
        with_md = compose(self.cfg(), self.tree, default_branch="main")
        without_md = compose(self.cfg(), self.tmp, default_branch="main")
        for text in (with_md, without_md):
            self.assertIn("## Your working tree", text)

    def test_absent_when_there_is_no_worktree_to_describe(self):
        self.assertNotIn("## Your working tree", compose(self.cfg()))
        self.assertNotIn("## Your working tree",
                         compose(self.cfg(), self.tree))
```

**Green** — `claudeloop/prompt.py`:

```python
WORKING_TREE = """## Your working tree

You are in a git worktree at {tree}, on a branch ClaudeLoop cut for this task
from {default}. Nothing else touches this tree while you have it, so its
branch, its commits and any uncommitted changes are yours alone.

{default} itself is checked out elsewhere, so two things that usually work do
not work here. `git checkout {default}` fails with "already used by worktree".
And `git push origin {default}` from this tree pushes that branch's own ref,
which does not carry your commits: it reports "Everything up-to-date", exits
0, and ships nothing. Name HEAD explicitly instead:

    git push origin HEAD:{default}   # to land your work on {default}
    git push -u origin HEAD          # to publish this branch, for a pull request

Which of the two is right is this repository's decision, not ClaudeLoop's. If
its own instructions say work lands on {default}, use the first. If they say
nothing, or ask for a pull request, use the second."""


def working_tree_section(tree: Path | None, default_branch: str | None) -> str:
    """Fact about the environment, not policy -- so it is composed for every
    task, whatever the repository documents about itself.

    Empty unless both facts are known: a section that guesses the default
    branch would hand a literal-minded session a push command aimed at a
    branch that may not exist.
    """
    if tree is None or not default_branch:
        return ""
    return WORKING_TREE.format(tree=tree, default=default_branch)
```

and in `compose`, after the precedence paragraph:

```python
def compose(cfg: Config, tree: Path | None = None,
            default_branch: str | None = None) -> str:
    ...
    facts = working_tree_section(tree, default_branch)
    if facts:
        parts.append(facts)
```

---

## Step 2 — The guards move into PROTOCOL, the builtin becomes only a
definition of done

**Red** — in `PromptTest`:

```python
    def test_the_protocol_carries_the_task_file_guard(self):
        # It used to live in BUILTIN_DEFINITION_OF_DONE, which compose()
        # drops whenever the repository's own CLAUDE.md defines done -- so
        # the better a repository documented itself, the fewer of
        # ClaudeLoop's own guards survived. This is not a definition of done;
        # it is ClaudeLoop's bookkeeping, and it holds unconditionally.
        self.assertIn("task-tracking file", PROTOCOL)
        self.assertIn("git add -A", PROTOCOL)

    def test_the_guards_survive_a_repo_that_fully_defines_done(self):
        (self.repo / "CLAUDE.md").write_text(
            "# rules\n\nDone means: committed and pushed to main.\n"
        )
        text = compose(self.cfg())
        self.assertIn("task-tracking file", text)

    def test_the_builtin_no_longer_carries_the_guards(self):
        self.assertNotIn("task-tracking file", BUILTIN_DEFINITION_OF_DONE)
        self.assertNotIn("Never check out the default branch",
                         BUILTIN_DEFINITION_OF_DONE)

    def test_the_builtin_defers_to_the_repository_on_where_work_lands(self):
        self.assertIn("as this repository's instructions direct",
                      BUILTIN_DEFINITION_OF_DONE)
```

Existing tests updated in the same step, since they pin the old placement:
`test_the_definition_of_done_still_forbids_the_default_branch` is deleted (the
working-tree section states it as fact, and Step 1 covers it);
`test_the_builtin_forbids_touching_the_task_list` becomes the PROTOCOL test
above; `test_the_definition_of_done_does_not_ask_the_session_to_branch` keeps
its assertion against the new wording ("branch you are already on").

**Green** — append to `PROTOCOL`:

```python
    " One thing is ClaudeLoop's own bookkeeping rather than part of the work, "
    "and holds whatever this repository's instructions say: never git add, "
    "stage, commit, stash or revert ClaudeLoop's own task-tracking file if one "
    "lives in this repository. ClaudeLoop rewrites it itself once you finish, "
    "and a broad `git add -A`, or a branch cleanup like `git checkout -- .` or "
    "`git stash`, can silently make already-finished work look pending again. "
    "Prefer staging files by name."
```

and rewrite `BUILTIN_DEFINITION_OF_DONE` to drop the branch mechanics and the
guards, keeping every substring the escape-hatch tests pin:

```python
BUILTIN_DEFINITION_OF_DONE = (
    "Done means: the change is implemented; the repository's own tests and "
    "checks, if it has any, pass; the work is committed on the branch you are "
    "already on; and the work is published -- pushed as this repository's "
    "instructions direct, or, if they do not say, pushed as this branch with a "
    "pull request open. You do not need to create a branch, and you may rename "
    "the one you are on with `git branch -m` if you like. If the repository "
    "has no remote configured, or a remote is configured but push credentials "
    "or a forge CLI (gh, glab, or similar) to open a pull request with are not "
    "available, that is not blocked: supplying a remote or push credentials is "
    "not a decision anyone can hand you mid-run, so there is nothing to wait "
    "on, and the work itself is finished. Commit, then write status \"done\" "
    "(not \"blocked\") and name in your summary exactly what was missing. "
    "Write that result file and stop there; do not instead end your turn by "
    "asking a human what to do next -- nobody reads your last message, and the "
    "result file is what ends the task."
)
```

---

## Step 3 — `precedence()` says the repository comes first

**Red**:

```python
    def test_precedence_puts_the_repository_above_claudeloops_fallback(self):
        for text in (precedence(has_operator=True), precedence(has_operator=False)):
            self.assertIn("this repository's own instructions come first", text)
            self.assertIn("only a fallback", text)

    def test_precedence_no_longer_calls_the_builtin_the_base(self):
        # S1's framing, reversed by this slice: ClaudeLoop's definition of
        # done was the base and the repository's file was pointed at from
        # inside it.
        self.assertNotIn("definition of done is the base",
                         precedence(has_operator=False))

    def test_precedence_states_the_facts_layer_cannot_be_overridden(self):
        self.assertIn("fact about this machine", precedence(has_operator=False))

    def test_the_repo_pointer_says_the_repository_comes_first(self):
        (self.repo / "CLAUDE.md").write_text("# rules")
        self.assertIn("They come first", compose(self.cfg()))
```

`test_protocol_and_base_precedence_are_always_present` is updated to the new
wording in the same step.

**Green**:

```python
def precedence(has_operator: bool) -> str:
    parts = [
        "These instructions are layered. The ClaudeLoop protocol above is a "
        "small set of invariants that hold because ClaudeLoop itself breaks "
        "without them, and it overrides everything below it. The working tree "
        "section is fact about this machine rather than policy -- nothing "
        "below can make it untrue."
    ]
    if has_operator:
        parts.append(
            "The operator instructions outrank this repository's own "
            "instructions."
        )
    parts.append(
        "Below those, this repository's own instructions come first: they "
        "decide how work is done here, including when it is finished and "
        "where it lands. ClaudeLoop's definition of done is only a fallback "
        "for what they do not say. Where layers conflict, follow the higher "
        "one and say so in your summary."
    )
    return " ".join(parts)
```

and the repo pointer in `compose`:

```python
            "## Definition of done\n\nThis repository has its own instructions "
            f"at {claude_md}. They come first: follow that file end to end -- "
            "it defines what \"done\" means here, including its testing, "
            "verification and publishing requirements. Use what follows only "
            "for what that file does not say:\n\n"
```

---

## Step 4 — the default branch reaches the prompt

**Red** — `tests/test_session.py`:

```python
    def test_the_default_branch_reaches_the_appended_prompt(self):
        cmd = session.build_command(
            self.cfg, "uuid-1", "do it", resume=False,
            tree=self.tmp / "wt", default_branch="trunk",
        )
        sent = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("git push origin HEAD:trunk", sent)
```

and `tests/test_loop.py`, against the existing fake-CLI harness, asserting the
recorded argv for a run carries the section (the fake writes its arguments to
a file — the existing `run_task` tests already read it):

```python
    def test_run_task_tells_the_session_which_branch_is_the_default(self):
        ...
        self.assertIn("git push origin HEAD:main", appended_system_prompt)
```

**Green** — `session.build_command` and `session.run` grow a
`default_branch: str | None = None` parameter, forwarded to `compose`; the
existing `tree`/`cwd` argument is already threaded. In `loop.run_task`, next to
the `worktree.ensure` call:

```python
    # Cheap local git call, on the same thread hop as ensure(): the prompt
    # states the default branch as fact, and a session that has to guess it
    # guesses wrong -- that is the defect this slice fixes.
    default = await asyncio.to_thread(worktree.default_branch, cfg.repo)
```

passed into `session.run(..., default_branch=default)`.

---

## Step 5 — a new worktree is cut from the remote's default branch

**Red** — `tests/gitrepo.py` grows a helper:

```python
def make_repo_with_remote(path: Path, remote: Path) -> Path:
    """A repository whose `origin` is a real bare repository on disk, so a
    fetch is exercised without a network."""
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    repo = make_repo(path)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo
```

`tests/test_worktree.py`:

```python
    def test_a_new_branch_is_cut_from_the_remote_default_branch(self):
        # The case this exists for: a previous task pushed straight to the
        # remote's main, so the local ref is behind and a task cut from it
        # would silently lose that work.
        remote = self.tmp / "remote.git"
        repo = make_repo_with_remote(self.tmp / "withremote", remote)
        _push_a_commit_to_remote_main(remote)   # helper in the test module

        path = worktree.ensure(repo, self.root, "abc123")

        self.assertTrue((path / "from-elsewhere.txt").exists())

    def test_a_repository_with_no_remote_still_cuts_from_the_local_branch(self):
        path = worktree.ensure(self.repo, self.root, "abc123")
        self.assertEqual(branch_of(path), "claudeloop/abc123")
        self.assertTrue((path / "README.md").exists())

    def test_an_unreachable_remote_falls_back_to_the_local_branch(self):
        repo = make_repo(self.tmp / "broken")
        subprocess.run(["git", "remote", "add", "origin",
                        str(self.tmp / "no-such-repo.git")],
                       cwd=repo, check=True, capture_output=True,
                       stdin=subprocess.DEVNULL)

        path = worktree.ensure(repo, self.root, "abc123")  # must not raise

        self.assertTrue((path / "README.md").exists())

    def test_a_reused_tree_is_never_refetched_or_rebased(self):
        path = worktree.ensure(self.repo, self.root, "abc123")
        (path / "half-done.txt").write_text("work in progress\n")
        again = worktree.ensure(self.repo, self.root, "abc123")
        self.assertEqual((again / "half-done.txt").read_text(), "work in progress\n")
```

**Green** — `claudeloop/worktree.py`:

```python
FETCH_TIMEOUT_S = 60
"""A fetch crosses the network, so GIT_TIMEOUT_S -- written for local commands
that finish in milliseconds -- would abort a healthy one. Still bounded: an
unattended loop must not hang on a remote that accepts and then stalls."""


def base_ref(repo: Path, branch: str) -> str:
    """The ref a new task branch is cut from: `origin/<branch>` when the
    repository has a remote this fetch can reach, `<branch>` otherwise.

    Sessions may push their work straight to the remote's default branch --
    which is the repository's own decision, and what its instructions ask for
    in at least one live case -- and that never moves the local ref. Without
    this, every task after the first cuts from the same stale point and loses
    the work in between.

    Every failure degrades to the local branch rather than failing the task:
    no remote, no network, a locked credential agent. A stale base is worse
    than no progress only if you can tell the difference, and the prompt says
    which branch the tree was cut from either way.
    """
    result = _try_git(repo, "fetch", "--quiet", "origin", branch,
                      timeout=FETCH_TIMEOUT_S)
    if result is None or result.returncode != 0:
        return branch
    check = _try_git(repo, "rev-parse", "-q", "--verify",
                     f"refs/remotes/origin/{branch}")
    if check is None or check.returncode != 0:
        return branch
    return f"origin/{branch}"
```

`_git`/`_try_git` grow a `timeout: int = GIT_TIMEOUT_S` keyword, and `ensure`
uses the new ref on the creation path only:

```python
    base = default_branch(repo)
    if base is None:
        raise RuntimeError(f"no default branch to cut {branch} from in {repo}")
    ref = base_ref(repo, base)
```

---

## Step 6 — Live smoke test

Not optional, and this slice is entirely prompt text plus one git behaviour,
which is exactly the category a fixture suite cannot judge.

Scratch repository, `model = "haiku"`, **two tasks**, and — new for this slice
— a **local bare remote** as `origin`, plus a `CLAUDE.md` that states a
close-out of "commit and `git push origin main`", the shape assimo has.

What the run has to show:

1. Task one's commits reach the *remote's* `main`, not just a local branch.
2. Task two's worktree contains task one's work, proving the fetch.
3. Neither session tries `git checkout main`, and neither reports success off
   the back of an "Everything up-to-date" no-op.
4. Both write a result file with status `done`.

Then: full suite, a review over the whole branch, `ROADMAP.md` updated with
S10 and the staleness item struck from the open issues, and merge.
