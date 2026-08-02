# S6 — A git worktree per task

**Status:** design agreed, not built.
**Date:** 2026-08-02.

Every task so far has run in the same working tree, and `reset_to_default_branch`
exists to undo what the previous task left there. It compensates for shared
mutable state by mutating it once more on the way in. S2b's live smoke test
showed what that costs: a task that parked before creating a branch resumed onto
the **next** task's branch, on both task sources — and separately, task 2
committed straight onto the default branch that task 1 later cut its branch
from, carrying an unrelated commit along.

Giving each task its own `git worktree` removes the shared state rather than
patching it. Nothing to inherit, nothing to reset.

## What is being built

1. Each task runs in `~/.claudeloop/worktrees/<task-id>`, created by ClaudeLoop
   with `git worktree add -b claudeloop/<task-id> <path> <default-branch>`.
2. The session therefore starts **already on a fresh branch cut from the
   default branch**, made for it. Branch creation stops being something a
   session has to remember to do.
3. A parked task's worktree persists untouched — including uncommitted changes —
   and its resumed session finds it exactly as it left it.
4. A terminal task's worktree is removed; its branch and commits stay in the
   repository.
5. `reset_to_default_branch` is deleted, along with every use of the
   `DEFAULT_BRANCH_CANDIDATES` guessing except as the start point for `add`.

## Decisions

### ClaudeLoop creates the branch, not the session

`git worktree add -b claudeloop/<task-id> <path> <default>`.

The built-in definition of done has told sessions to branch before their first
commit since S1, and S1's live smoke test measured about 50% compliance. That
number is why `reset_to_default_branch` was written at all. Creating the branch
structurally makes the instruction unnecessary rather than better-worded — the
session cannot fail to comply with something it is not asked to do.

The name is ClaudeLoop's and therefore not descriptive. A session may rename it
(`git branch -m`) and the prompt says so; nothing in the orchestrator reads the
branch name back, so a rename costs nothing.

The alternative considered was `git worktree add --detach`, leaving the session
to branch itself as today. It keeps nicer names and keeps the non-compliance.
The failure mode is milder under worktrees than it is now — a non-compliant
session commits to a detached HEAD or to a private checkout rather than to the
operator's real default branch — but it is still a failure mode, and it is the
one this slice exists to remove.

### The worktree is reused across a park, and only a terminal status releases it

`ensure(repo, root, task_id)` returns the same path every time for a given
task. If that path is already a registered worktree it is reused as-is: this is
the resume path, and reusing it is what makes a parked session's tree — branch,
commits, and uncommitted changes — survive intact until its answer arrives.

If the path is absent but the branch `claudeloop/<task-id>` already exists (the
worktree was removed, or the home directory was wiped), `add` is retried
without `-b` against that branch, so an answered task lands back on its own
work rather than on a fresh branch beside it.

On a terminal result — `done` or `failed` — `git worktree remove <path>` runs,
**without `--force`**. Git refuses to remove a tree with uncommitted changes;
that refusal is the feature, and it is logged and left alone. The same
reasoning `reset_to_default_branch` was written under still holds: destroying a
working tree in an unattended loop is worse than leaving a directory behind.
Branches and commits survive removal regardless — they live in the repository,
not in the worktree.

There is no age policy and no config key. Disk grows with the number of parked
tasks and dirty failed ones, which is bounded in practice by how many questions
a human leaves unanswered. If that turns out to be wrong, pruning is a later
slice with real numbers behind it.

### One code path: no fallback to the repository itself

At startup, `probe(repo)` runs `git worktree prune`, then resolves the default
branch. Either failing exits the loop with that message. A successful prune
already proves both that `repo` is a git repository and that this git has the
`worktree` subcommand, so it doubles as the capability check; it also clears
registrations left behind by a home directory wiped while worktrees existed. A box whose git cannot do worktrees, or a repository with no
resolvable default branch, is a configuration error, and an unattended loop
should say so before it starts rather than on each task in turn.

A per-task `git worktree add` failure is an **environment fault, not a
verdict**: it propagates out of `run_task` and `main_loop`'s existing crash
handler records it as `error` without marking the task in its source.
(Corrected during implementation — the design first said `failed` and marked.
`failed` is terminal, so a transient fault such as a held `index.lock` or a
full disk would have failed and permanently marked every remaining task in
seconds, which is the exact reasoning the crash handler already carries.)

Falling back to running in `cfg.repo` was rejected. It would keep
`reset_to_default_branch`, the default-branch guessing, and the branch
inheritance bug alive as a second path — one that runs rarely, is therefore
poorly tested, and reintroduces exactly the defect this slice removes.

### `cfg.repo` becomes the repository to branch *from*

`session.run` gains a `cwd` argument; the loop passes the worktree path.
`cfg.repo` keeps its config validation unchanged and is used for the git
plumbing only.

`prompt.compose` also gains the worktree path, so the definition-of-done layer
names the copy of `CLAUDE.md` the session can actually edit. The file is
identical in both places; pointing a literal-minded agent at a path outside its
own working directory is the kind of ambiguity this project treats as a defect.

### Three prompt strings change

- `BUILTIN_DEFINITION_OF_DONE`: "the work is committed on a new branch created
  from the repository's default branch. Create that branch before your first
  commit" becomes a statement of fact — the session is already on a fresh
  branch cut from the default branch and made for this task, it should commit
  there, it may rename the branch, and it must never check the default branch
  out and commit onto it.
- `ANSWER_PROMPT`: the branch-checkout clause is deleted outright. It exists
  only because the tree moved while the task was parked, which no longer
  happens. Replaced with the opposite assurance: the working tree is exactly as
  it was left, still on its branch, uncommitted changes included.
- `FRESH_ANSWER_PROMPT`: "an earlier attempt may have left a branch in this
  repository; look before you redo work" becomes "you are on the branch that
  attempt used, and its commits are there" — under worktree reuse that is no
  longer a maybe.

`PROTOCOL` is unchanged.

### The hard constraint is rewritten, and the exception is named

`CLAUDE.md` says no trace of ClaudeLoop lives in a repository it works in.
`git worktree add` writes `.git/worktrees/<task-id>/` into the target
repository and a `.git` file into the worktree, so this slice cannot ship under
that sentence as written.

The constraint's actual purpose, stated in the same paragraph, is that a
session doing ordinary branch hygiene must not be able to revert ClaudeLoop's
own mark and make finished work look pending. The rewrite says that instead:
**nothing ClaudeLoop writes may be committable** — reachable from a session's
staging area, or revertible by `git checkout -- .`, `git stash`, or a broad
`git add -A`. The result file, the event log and the database stay under
`~/.claudeloop` exactly as before, and `load_config` still refuses a
`tasks_file` inside `repo`.

`.git/worktrees/` is named as the one exception: it is outside every working
tree, invisible to `git add`, and cleaned by `git worktree prune`, which
`probe` runs at startup.

## What this closes

Three entries move out of the roadmap's open issues:

- Parked tasks widening the window for default-branch contamination. Every task
  branches at its own start, from the default branch, in its own tree.
- A parked task holding a branch that the next task's checkout moves away from,
  and `ANSWER_PROMPT` having to talk the resumed session back onto it.
- A parked task's uncommitted work being lost when the next task runs.

## Architecture

A new module, `claudeloop/worktree.py`. `loop.py` is already 735 lines, and the
git plumbing it holds (`_git`, `_try_git`, `default_branch`) moves there whole
rather than being copied — this is a move plus three functions, not a new layer.

| Function | Responsibility |
|---|---|
| `probe(repo) -> str \| None` | Startup check: prune, list, resolve default branch. An error string, or `None` when the box can do this |
| `ensure(repo, root, task_id) -> Path` | The task's worktree, created or reused. Raises on failure |
| `release(repo, path) -> None` | `git worktree remove`, never forced. Logs and returns when git refuses. Run from the repository, never from the tree being removed |

`run_task` calls `ensure` where `reset_to_default_branch` is called today, and
`release` after a terminal result. `main()` calls `probe` before the polling
loop starts.

## Testing

Against real scratch repositories in the existing style — no mocks, and
`commit.gpgsign false` set locally on each, for the reason in the roadmap's
working notes.

- `ensure` creates a worktree on the branch it says, cut from the default
  branch and not from whatever `HEAD` happens to be.
- `ensure` returns the same path with its contents intact when the worktree
  already exists — the park-and-resume case, asserted on an uncommitted file.
- `ensure` reattaches to an existing `claudeloop/<id>` branch when the worktree
  is gone, and the branch's commits are present.
- `release` removes a clean tree, and leaves a dirty one in place without
  raising.
- `probe` returns a message for a directory that is not a git repository, and
  `None` for one that is.
- `run_task` passes the worktree as the session's `cwd`, and marks a task
  `failed` when `ensure` raises.
- The three prompt strings are pinned by tests that fail on the old wording.

Then **the live smoke test**: a scratch repository, `model = "haiku"`, two
tasks, one of which must park on a question and be answered — resuming into a
kept worktree is this slice's central claim, and no fixture can test that a
real session finds its own work where it left it.

## Not in this slice

- Pruning worktrees by age or count.
- A config key for the worktree root.
- Migrating a task parked before this slice: it resumes into a fresh worktree,
  so its session's remembered paths are stale. One-time, and only for an
  install that is mid-park at upgrade.
