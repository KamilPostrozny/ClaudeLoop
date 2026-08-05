# S14 — The branch tasks are cut from is configurable

**Date:** 2026-08-05
**State at time of writing:** S13 merged; no slice scheduled.

## The problem

`worktree.create` cuts every task's branch from `worktree.default_branch(repo)`,
which resolves `git symbolic-ref refs/remotes/origin/HEAD` and falls back to a
local `main` or `master`. There is no way to say "cut from something else".

That is wrong for any repository whose live work is not on the default branch.
The case that forced this: Port22 keeps a long-lived `xtool` branch that is
never pushed — a local-only fast-track loop for iterating on the iOS app
against a real phone, carrying six overlay files that must never reach `main`.
A task run against that repository is cut from `main`, so it is written
against the wrong `App/`. The first one landed a call to `state.dpadExpanded`,
a member `main` has and `xtool` does not, and the operator had to port it by
hand before it would compile.

The workaround available today is to lie to git — point
`refs/remotes/origin/HEAD` at a remote-tracking ref that does not exist:

    git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/xtool

It works, because `base_ref`'s fetch of a branch the remote does not have
fails and degrades to the local ref. It is still a lie written into the
operator's own repository, where every later reader of `origin/HEAD` inherits
it, and it is invisible from ClaudeLoop's configuration — the one file that is
supposed to say how this instance behaves.

## The decision

One optional config key, `base_branch`. Unset, everything behaves exactly as
it does today. Set, it names the branch every task's worktree is cut from, and
`origin/HEAD` is not consulted at all.

```toml
repo        = "/home/you/Projects/yourrepo"
base_branch = "xtool"   # optional; default is the repository's own default branch
```

### Why the name is `base_branch` and not `default_branch`

`default_branch` is git's word for what `origin/HEAD` points at, and this key
is precisely the thing that overrides that. Naming the override after the
thing it overrides would make `default_branch = "xtool"` read as a claim about
the repository rather than an instruction to ClaudeLoop. The function keeps
its name because it still answers the same question — *which branch do we cut
from* — it just takes an answer now.

### Where the override is applied

In `worktree.default_branch(repo, override=None)`, not at its call sites.
Four places ask that question today — `probe`, `create`, `run_task`'s prompt
composition, and the startup prompt audit — and an override threaded through
each of them separately is four chances to miss one. The audit is exactly the
place a missed one would be silent: it would print a prompt naming `main`
while every task was cut from `xtool`.

### An override that does not exist is a startup failure, not a task failure

`default_branch` returns the override only when `refs/heads/<override>`
resolves, and `None` otherwise. That makes `probe`'s existing check — the one
that already refuses to start a loop whose default branch cannot be
determined — the check for this too, with no new call site and no new failure
path. `probe` runs after the clone, so it works for a URL `repo` as well,
which a `check` in `config.SCHEMA` could not: at load time that repository has
not been cloned yet and has no local refs to verify against.

The message it fails with names the key, because "cannot determine the default
branch" is a lie when the operator named one explicitly and typed it wrong.

### `base_ref` is untouched

`create` cuts from `base_ref(repo, base)`, which fetches `origin/<base>` and
degrades to the local branch when there is no such remote branch. Under a
local-only base branch that fetch fails on every task. That is one failed
`git fetch` per task, bounded by `FETCH_TIMEOUT_S` and already the behaviour
for any repository whose origin is unreachable, and the alternative — skipping
the fetch when an override is set — would silently stale the base for an
operator who set `base_branch = "develop"` against a real remote branch, which
is the other half of this key's audience. Left alone deliberately.

### The base branch may be the one that is checked out

Port22's `xtool` is checked out in the operator's own working tree, so this is
the normal case rather than an edge one. `git worktree add -b <new> <path>
<base>` creates a *new* branch from `<base>`; only checking `<base>` itself out
a second time is what fails with `already used by worktree`. It is pinned by a
test all the same, because S6's live smoke test is where that distinction was
learned and a regression here would break every task rather than one.

### What the session is told

Nothing new. `prompt.working_tree_section` already fills `WORKING_TREE` from
whatever `default_branch` returned, so a session cut from `xtool` is told it is
on a branch cut from `xtool`, that `git checkout xtool` will fail, and that
`git push origin HEAD:xtool` is how work lands there. Those sentences become
true for the configured branch for free — which is the argument for overriding
inside the function rather than at three of its four call sites.

## Known hole: the wizard's repository check does not know about it

`setup.run_setup` validates `repo` by calling `worktree.probe(repo)` with no
base, because `base_branch` is submitted on the same screen and there is no
ordering in which one field's check can see a value the operator has not typed
yet. So a repository that has *only* a non-standard branch — no `main`, no
`master`, no `origin/HEAD` — is rejected by the wizard even though
`base_branch` would name the branch it has.

Left standing. It needs a repository with no conventional branch at all, which
is rare, and the way out is hand-editing `config.toml`, where the loop's own
`probe` accepts it. Closing it means either a second probe keyed on a field
declared later, or teaching the repository check to skip the default-branch
half — both larger than the case deserves.

## What this does not do

- **No per-task base branch.** The key is per instance, like every other. A
  repository that needs two would run two instances, which is already the
  answer for two repositories.
- **No `git checkout` of the base branch, ever.** S6's constraint stands: the
  operator's own working tree is never moved.
- **No validation that the base branch is sensible** — that it is not an
  ancestor of something, not stale, not a tag. It has to exist; the rest is
  the operator's business.
