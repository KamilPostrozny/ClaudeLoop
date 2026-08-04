# S10 — Repository-first prompt layering

Design capture, 2026-08-04. Records what was decided at the time.

## The problem

A live Jira task against `~/Projects/assimo` reported `done` with its work
committed, and the work never reached `main`. The repository's own `CLAUDE.md`
says, under "Close-out is not a separate approval gate":

> Once the change is implemented and verified, complete the ADR + wiki refresh
> + index + log, then `git commit` **and `git push origin main`**, then report.

The session committed. It did not ship. Three separate defects in the prompt
layering produced that, and a fourth is latent behind the fix.

### 1. `git push origin main` from a worktree is a silent no-op

Reproduced in a scratch repository — a bare remote, a clone, and a worktree on
a second branch, exactly S6's shape:

```
$ git checkout main
fatal: 'main' is already used by worktree at '.../work'
$ git push origin main
Everything up-to-date
$ echo $?
0
```

`git push origin main` pushes the *ref named `main`*, which in a worktree
checkout of `claudeloop/<task-id>` is the repository's own untouched default
branch — not `HEAD`. Git reports success. A literal-minded session, which is
what `prompt.py` says these strings are written for, reads "Everything
up-to-date" as "already shipped" and writes `done`.

Nothing in any prompt layer tells the session it is in a worktree, what branch
it is on, that the default branch cannot be checked out here, or how to
publish from where it actually stands. All of that is left to be inferred from
`git status`.

### 2. ClaudeLoop's own guards are inside a conditional branch

`compose()` emits, when the repository has a `CLAUDE.md`:

> This repository has its own instructions at `<path>`. Follow that file end to
> end — it defines what "done" means here… **If it does not say when the work
> is finished, use this instead:** `<BUILTIN_DEFINITION_OF_DONE>`

assimo's `CLAUDE.md` does say when work is finished. So the builtin is
correctly dropped — and it takes these with it:

- "Never check out the default branch and commit onto it."
- "Never git add, stage, commit, stash, or revert ClaudeLoop's own
  task-tracking file if one lives in this repository."
- "Prefer staging files by name over `git add -A`."

None of those is a definition of done. They are ClaudeLoop invariants that
exist because ClaudeLoop's own bookkeeping breaks without them, and they were
packaged in the layer most likely to be skipped. The better a repository
documents itself, the fewer of ClaudeLoop's guards survive — exactly backwards.

### 3. `precedence()` does not rank the conflict that actually occurs

It ranks protocol > operator instructions > definition of done. The real
conflict was *inside* the definition-of-done layer — the repository's file
against the builtin fallback — and between the repository's file and
ClaudeLoop's unstated mechanics. The session had no stated rule for "the
repository says push to `main`, and something has put me on a branch", so it
picked one, and picked the reading that silently does nothing.

### 4. Latent: every task cuts from a stale base

`worktree.ensure` bases each new branch on the *local* default branch. Today
that is only stale if the operator forgets to pull. Once sessions push their
own work to the remote default branch — which is what fixing (1) authorises —
the local ref never moves at all, and every subsequent task branches off the
point the first one started from, missing all the work in between.

## The decision

**The repository's own instructions come first.** ClaudeLoop's layers are
demoted to three specific jobs, in this order:

1. **Invariants** — the handful of rules that exist because ClaudeLoop itself
   breaks without them. The result file ends the task; do not transition the
   Jira issue; do not stage or revert ClaudeLoop's task-tracking file.
2. **Facts** — what is mechanically true about the environment the session was
   placed in, which it could not know and must not guess.
3. **Fallbacks** — a definition of done, used only where the repository is
   silent.

Anything that is not one of those three is not ClaudeLoop's to say.

### A new always-present "Your working tree" section

Composed between the protocol and the task source, present for every task
regardless of what the repository documents, because it is fact rather than
policy:

```
## Your working tree

You are in a git worktree at <tree>, on branch <branch>, cut from
<base> in <repo>. Nothing else runs in this tree while you have it.

The default branch (<default>) is checked out elsewhere, so `git
checkout <default>` fails here with "already used by worktree" — and
`git push origin <default>` from this tree pushes that branch's own
ref, which does not have your commits on it. It reports "Everything
up-to-date" and ships nothing. Publish by naming HEAD explicitly:

    git push origin HEAD:<default>   # work lands on the default branch
    git push -u origin HEAD          # work lands on this branch, for a PR

Which of the two applies is this repository's decision, not
ClaudeLoop's: if its instructions say work lands on the default branch,
use the first. Otherwise use the second and open a pull request.
```

The literal command lines are deliberate. "Push HEAD rather than the branch
name" is the kind of instruction a session satisfies by guessing, and the
guess it already made was wrong.

`git push origin HEAD:<default>` is authorised because the repository
authorises it. For assimo that means an unattended session shipping to
production, which is that repository's stated ship flow ("direct-to-`main`
matches the project's ship flow; CI deploys from `main`, so the push is what
ships the fix"). A repository that does not say so gets the branch-and-PR path,
which is what the builtin already described.

### The guards move up, out of the fallback

"Never stage, commit, stash or revert ClaudeLoop's task-tracking file" and
"prefer staging files by name over `git add -A`" move into `PROTOCOL`, which is
always present and stated to be invariant. "Never check out the default branch"
is replaced by the working-tree section, which says why it cannot be done at
all rather than forbidding it.

What is left in `BUILTIN_DEFINITION_OF_DONE` is only a definition of done:
implemented, tests pass, committed, published. The publishing mechanics are now
stated once, in the facts section, instead of twice with different wording.

### `precedence()` is rewritten to say the repository wins

The new text states the actual ranking: the protocol is a small set of
mechanical invariants that stand above everything because ClaudeLoop breaks
otherwise; the working-tree section is fact and cannot be overridden by
anything; the operator layer, when present, outranks the repository; and the
repository's own instructions outrank ClaudeLoop's definition of done, which is
a fallback for what the repository does not say.

That is a real reversal of S1's framing, in which ClaudeLoop's definition of
done was the base and the repository's file was pointed at from inside it.
Recorded here rather than rewritten into the S1 spec.

### `worktree.ensure` fetches and cuts from the remote

When the repository has an `origin` and the fetch succeeds, a new branch is cut
from `origin/<default>`; otherwise from the local `<default>`, exactly as
today. A fetch that fails — no network, a locked credential agent, no remote at
all — degrades to the local branch rather than failing the task: an unattended
loop that cannot reach the network should still make progress on a stale base,
and it says which base it used in the working-tree section either way.

The fetch happens only on the creation path. A reused worktree — a parked task
coming back with its answer, an interrupted task resuming — is never rebased
and never touched.

The fetch gets its own timeout, separate from `GIT_TIMEOUT_S`, which is
documented as bounding local commands that finish in milliseconds.

## Out of scope

- **assimo's live-verification gate.** Its `CLAUDE.md` requires hitting
  deployed endpoints and a Chrome sweep after every coding change, and reasons
  that "the push is what deploys". A session that pushes to the default branch
  can now satisfy the first half; the Chrome half needs an MCP server this
  configuration does not have. That is assimo's own decision to make in its
  own `CLAUDE.md`, not something ClaudeLoop should paper over in a prompt.
- **Config-driven ship flow.** A `publish = "branch" | "default-branch"` key
  was considered and rejected: it puts the decision on the operator when the
  repository is the thing that knows, and the facts section is needed either
  way.
- **Refreshing the base of a *reused* worktree.** A long-parked task still
  resumes on the base it was cut from. Rebasing a tree with uncommitted work in
  it, unattended, is a worse failure than a stale base.

## Acceptance

- The composed prompt names the worktree, the branch, the base it was cut
  from, and both publish commands, for every source and whether or not the
  repository has a `CLAUDE.md`.
- A repository whose instructions ask for a push to the default branch gets a
  prompt that makes that possible, and does not get a ClaudeLoop instruction
  contradicting it.
- ClaudeLoop's task-file and staging guards survive a repository that fully
  defines its own done.
- A second task cut after a first has pushed to the remote default branch
  contains the first task's work.
- The live smoke test runs two tasks against a scratch repository with a local
  bare remote and a `CLAUDE.md` that demands a push to `main`, and the work
  from both lands on the remote's `main`.
