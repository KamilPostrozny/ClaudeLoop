# S14 — The branch tasks are cut from is configurable: TDD plan

Spec: `docs/superpowers/specs/2026-08-05-base-branch-design.md`

## Step 1 — `default_branch` takes an override

**Test** (`tests/test_worktree.py`, extending the existing default-branch
tests): a scratch repository whose branches are `main` and `side`.

```python
def test_an_override_names_the_branch_tasks_are_cut_from(self):
    repo = self.repo_with_branches("main", "side")
    self.assertEqual(worktree.default_branch(repo, "side"), "side")

def test_no_override_still_resolves_the_repositorys_own_default(self):
    repo = self.repo_with_branches("main", "side")
    self.assertEqual(worktree.default_branch(repo), "main")

def test_an_override_that_does_not_exist_is_none_not_a_guess(self):
    repo = self.repo_with_branches("main", "side")
    self.assertIsNone(worktree.default_branch(repo, "typo"))
```

The third is the one that carries the design: an override must not fall
through to `main`, because a loop cutting from `main` while its config says
`xtool` is the silent failure this slice exists to remove.

**Code**: `default_branch(repo: Path, override: str | None = None)`. When
`override` is set, `git rev-parse -q --verify refs/heads/<override>` decides:
the name back, or `None`. `origin/HEAD` is not consulted. When it is unset,
today's body, unchanged.

## Step 2 — `probe` refuses to start on a base branch that is not there

**Test** (`tests/test_worktree.py`, `ProbeTest`):

```python
def test_probe_rejects_a_base_branch_the_repository_does_not_have(self):
    problem = worktree.probe(self.repo_with_branches("main"), "xtool")
    self.assertIn("base_branch", problem)
    self.assertIn("xtool", problem)

def test_probe_accepts_a_base_branch_the_repository_does_have(self):
    self.assertIsNone(worktree.probe(self.repo_with_branches("main", "xtool"), "xtool"))
```

The first asserts the key's name reaches the message. An operator who typed
`xtoool` gets told which key to fix, not that their repository has no default
branch — which it does.

**Code**: `probe(repo: Path, base: str | None = None)`, passing `base` to
`default_branch`, and branching the existing error message on whether `base`
was given.

## Step 3 — `create` cuts from the override, including when it is checked out

**Test** (`tests/test_worktree.py`, `CreateTest`): a scratch repository with
`main` and `side`, a file committed on `side` only, and `side` left as the
checked-out branch — which is Port22's actual shape and the case S6's live run
showed was worth pinning.

```python
def test_a_worktree_is_cut_from_the_override_branch(self):
    repo = self.repo_with_branches("main", "side")
    (repo / "only-on-side").write_text("x")
    self.commit(repo, "side", "only-on-side")
    tree = worktree.create(repo, self.root, "t1", base="side")
    self.assertTrue((tree / "only-on-side").exists())

def test_the_override_may_be_the_branch_that_is_checked_out(self):
    # git refuses a second checkout of one branch, but cutting a new branch
    # from it is fine -- and the operator's own tree sits on it.
```

**Code**: `create(repo, root, task_id, base: str | None = None)`, threading
`base` into its `default_branch` call. The `RuntimeError` it already raises
when there is no branch to cut from covers a bad override too, since Step 1
returns `None` for one.

## Step 4 — the config key

**Test** (`tests/test_config.py`):

```python
def test_base_branch_is_read(self):
    cfg = self.load('repo = "{repo}"\nbase_branch = "xtool"\n')
    self.assertEqual(cfg.base_branch, "xtool")

def test_base_branch_defaults_to_empty(self):
    self.assertEqual(self.load('repo = "{repo}"\n').base_branch, "")
```

**Code**: a `Field("base_branch", step="repository", default="", ...)`
declared immediately after `repo` in `SCHEMA`, a `base_branch: str = ""` on
`Config`, and the pass-through in `load_config`. No `check`: the branch cannot
be verified at load time for a URL `repo`, and `probe` is where it is verified
for both kinds. The help text says so, since the wizard renders it.

## Step 5 — the loop passes it everywhere it asks

**Test** (`tests/test_loop.py`): a config with `base_branch` set, asserting
`worktree.probe` and `worktree.default_branch` are called with it, and that
the composed prompt names it.

```python
def test_the_configured_base_branch_reaches_the_prompt(self):
    # WORKING_TREE names the branch the tree was cut from; with base_branch
    # set that must be the configured one, not origin/HEAD's answer.
```

**Code**: three call sites in `loop.py` — `probe` at startup, the
`default_branch` in `run_task`, and the one in the startup prompt audit — plus
`create`'s in whichever of the two modules calls it, each passing
`cfg.base_branch or None`.

## Step 6 — README

The config table gains `base_branch`, and the Branches and worktrees section
gains a paragraph: what it is for, that unset means the repository's own
default branch, and that the branch must exist locally.

## Step 7 — the live smoke test

Not optional, and this slice's version has a shape of its own: a scratch
repository with two branches, `base_branch` pointing at the non-default one,
**two tasks**, `model = "haiku"`.

What only a live run can show:

- that a session cut from the configured branch is *told* it was — the
  `WORKING_TREE` text naming `xtool` rather than `main` is composed for a real
  session here, not asserted as a substring;
- that the second task also cuts from the configured branch rather than from
  the first task's branch, which is the exact defect S1's live run found in
  the original branch handling;
- that a base branch checked out in the operator's own tree does not trip
  `already used by worktree` against a real `git worktree add`.
