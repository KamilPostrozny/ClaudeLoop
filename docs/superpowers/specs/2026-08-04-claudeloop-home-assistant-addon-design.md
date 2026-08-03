# S4 — Home Assistant OS addon

**Status:** built and merged.
**Date:** 2026-08-04.

The two sections marked *found by running the image* were written after the
fact: the design had neither, and the live run is what produced them.

ClaudeLoop is built to run for days with nobody watching, and so far the thing
running it has been a laptop — which sleeps, reboots into an update, and leaves
with its owner. A Home Assistant OS box does none of those. This slice packages
the orchestrator as an addon so the machine that already runs unattended for
years is the machine that runs the loop.

The packaging itself is small: the hard constraint that ClaudeLoop is Python
3.11+ and standard library only is what makes the image a base image plus a
copy. The work is in the five landmines the roadmap has been carrying, and in
one thing the roadmap did not have: **ingress**, which is how a Home Assistant
addon exposes a web UI, and which neither of ClaudeLoop's two servers can
currently be reached through.

## What is being built

1. An addon directory — `addon/config.yaml`, `addon/Dockerfile`,
   `addon/run.sh`, `addon/DOCS.md` — plus a `repository.yaml` at the repository
   root, so the repository can be added to Home Assistant as an addon
   repository as it stands, and `.github/workflows/addon.yml`, which builds the
   image the addon installs.
2. `/data`, the addon's persistent volume, is the container's `HOME`. Both
   `~/.claudeloop` and Claude Code's own `~/.claude` land in it, so config,
   `state.db`, worktrees, clones and the CLI's credentials all survive a
   restart and an addon update.
3. `CLAUDELOOP_INGRESS=1`: one environment variable, set by `run.sh` and read
   in one place, which makes the dashboard and the setup wizard reachable
   through Home Assistant's ingress proxy.
4. The dashboard's frontend derives its own base path, so every request it
   makes resolves under the ingress prefix.
5. The addon's own options carry only what must exist *before* ClaudeLoop can
   be configured: the Claude credential and the git identity. Everything else
   is configured in S5's wizard, through ingress, on first run.

## Decisions

### Configuration stays in ClaudeLoop's wizard, not in addon options

Home Assistant addons conventionally declare their configuration in
`config.yaml`'s `options`/`schema`, rendered by the supervisor. Doing that here
would mean maintaining a second description of all 23 config keys next to
`SCHEMA` — the one thing S5 exists to prevent, and a description with no access
to `SCHEMA`'s `required_if`, `check` or live tests. It would also make the
wizard dead code on the platform that needs it most: the S4 operator is exactly
the operator with no terminal.

So the addon declares four options, and every one of them is something the
wizard structurally cannot carry:

| Option | Why it cannot wait for the wizard |
|---|---|
| `claude_code_oauth_token` | Without it every session fails to authenticate. There is no terminal in which to run `claude setup-token`, so it is pasted here and exported as `CLAUDE_CODE_OAUTH_TOKEN`. |
| `git_user_name`, `git_user_email` | `git commit` refuses to run without an identity, and a session discovering that has already been paid for. |
| `setup` | How an operator reopens the wizard. It is the `--setup` flag, and an addon has no command line to pass one on. |

`run_setup` already blocks `main()` until a valid `config.toml` exists, so a
freshly installed addon starts, serves the wizard on its ingress URL, and waits.
No first-run README step, no shell.

`setup` is the one that is not strictly first-run: without it, changing a
single config key on a Home Assistant OS box means installing a second addon
(SSH, Samba, File editor) to hand-edit `/data/.claudeloop/config.toml`. It
stays on until the operator turns it off, which means a restart with it still
set reopens the wizard and waits there rather than working — documented in
`DOCS.md`, and the honest cost of a boolean over a button the supervisor has no
way to offer.

### Ingress, and what it replaces

Home Assistant reaches an addon's web UI through the supervisor, which proxies
`/api/hassio_ingress/<session>/…` to the addon's `ingress_port`, stripping that
prefix. Only an authenticated Home Assistant user can reach that path, and the
`<session>` segment is unguessable.

Three things in ClaudeLoop's web layer assume a browser talking to it directly,
and all three fail through that proxy:

- It binds `web_host` (`127.0.0.1` by default). The supervisor connects from
  another container, so loopback is unreachable.
- `_host_allowed` compares the `Host` header against the configured host and
  port. Through ingress, `Host` names Home Assistant (`homeassistant.local:8123`),
  not the addon.
- `_authorized` wants `?token=` on a URL the operator never types — they click
  a sidebar entry.

`CLAUDELOOP_INGRESS=1` answers all three: bind `0.0.0.0`, skip the `Host`
check, skip the token check. Each of those three guards is documented in the
code as the barrier standing between an arbitrary web page and this server *at
the loopback default*, so removing them needs the replacement to be at least as
strong. It is:

- **The `Host` check defends against DNS rebinding**, which needs the target
  reachable from the victim's browser. An ingress addon publishes **no port**:
  `config.yaml` declares `ingress_port` and no `ports:`, so nothing is mapped
  onto the host and the listener exists only on the supervisor's internal
  docker network. There is no address for a rebound name to resolve to.
- **The token defends an unauthenticated network path.** Through ingress there
  isn't one — the supervisor authenticates a Home Assistant user before the
  request is proxied, which is a real login rather than a shared secret in a
  query string. It also removes the open issue that the token travels in the
  query string and would have reached the ingress access log.

What this does concede: any container on the supervisor's docker network can
reach the port unauthenticated, and any *authenticated Home Assistant user* can
use the dashboard and the wizard. Both are what every ingress addon concedes,
and the second is what an addon is for.

`CLAUDELOOP_INGRESS` is an environment variable rather than a config key
because the setup wizard has to work under ingress on a box that has no
`config.toml` yet — there is nothing to read a key out of. It is read through
`config.ingress()` at request time, not captured at import, so a test can set it
around a server.

**This is a deliberate, recorded reversal of half of S5's second barrier.**
Setup mode's rule was that it binds loopback unconditionally *and* requires a
one-time console token, because with no config there is no `web_token` and one
barrier alone could not be the network. Under ingress the network barrier is
replaced by supervisor authentication, so the console token — which the
operator could not supply anyway, having no way to add a query string to a
sidebar link — is dropped with it. Outside ingress, both barriers are exactly
as S5 left them.

### The frontend derives its base path

`index.html` builds every URL through one `url()` helper, and every path it
passes is absolute (`/api/state`). Under ingress the page is served from
`/api/hassio_ingress/<session>/`, where an absolute path escapes the prefix and
hits Home Assistant itself.

`url()` gains a `BASE` derived from `location.pathname` — everything up to and
including the last `/`, minus the trailing slash. That is `""` for the page at
`/` and `/api/hassio_ingress/<session>` under ingress, so one line covers both
and there is nothing to configure. It relies on the ingress URL ending in `/`,
which is how the supervisor serves it.

The server needs no path handling at all: the supervisor strips the prefix
before proxying, so routes arrive as `/api/state` either way.

### Debian base, `npm` global install for the CLI

`ghcr.io/home-assistant/{arch}-base-debian:bookworm`: Python 3.11 from `apt`,
which is the floor this project already requires, and glibc, which is what the
Claude Code CLI is built against.

The CLI is installed with `npm install -g @anthropic-ai/claude-code`, landing in
`/usr/lib/node_modules` with a launcher in `/usr/bin`. The alternative —
`claude.ai/install.sh` — installs under `$HOME`, and this image deliberately
moves `HOME` to a volume that is empty at build time and mounted at runtime.
A binary whose location depends on `HOME` is the wrong shape for that.

The Dockerfile is the whole build: no `pip install`, no `npm install` for
ClaudeLoop itself, no build step for the dashboard. `COPY claudeloop /app/claudeloop`
is the entire application layer.

### The image is prebuilt, because the supervisor cannot build this one

The supervisor builds an addon with **the addon's own folder as the docker
context** (`AddonBuild.get_docker_args` passes `path=addon.path_location`), and
a Dockerfile cannot `COPY` from outside its context. ClaudeLoop's source is the
repository root. The three ways out:

1. Duplicate `claudeloop/` into `addon/`. Two copies of the orchestrator in one
   repository, kept in step by hand or by a test that only ever fails after
   someone has already forgotten.
2. Have the Dockerfile `git clone` this repository at build time. It would
   build whatever is on the remote rather than the checkout the operator
   installed, and needs the network for what is otherwise a local copy.
3. Publish a prebuilt image and have `config.yaml` name it. The supervisor
   pulls rather than builds, and the build happens where the context can be the
   repository root.

The third is what every published addon does anyway, so that is what this
slice does: `image: ghcr.io/OWNER/claudeloop-{arch}`, produced by
`.github/workflows/addon.yml` from the repository root with
`docker/build-push-action` — not `home-assistant/builder`, which has the same
addon-folder context the supervisor does. `addon/Dockerfile` keeps a default
`BUILD_FROM` so the same file builds by hand from the repository root.

**The consequence, and it is a real one: `OWNER` is a placeholder in three
files** — `addon/config.yaml`, `repository.yaml`, `addon/DOCS.md` — because
this repository has never been pushed and has no remote. Nothing installs
until the first push fills them in and the workflow has run once.

### Sessions run unprivileged, and that is not a preference

`claude --permission-mode bypassPermissions` refuses to start under uid 0:
"--dangerously-skip-permissions cannot be used with root/sudo privileges for
security reasons". An addon container is root by default, so every session
would exit before producing a byte, the loop would see no result file, and
every task would fail in about three seconds each.

So the image creates `claudeloop` (uid 1000) with `/data` as its home,
`run.sh` hands `/data` to it, and the loop is started through `setpriv`, which
drops privileges without touching the environment the script has just built.

This was found by running the image, not by reading about it. It is invisible
to every test in this repository, because the fake `claude` in the suite has no
opinion about who runs it.

### `run.sh` does the four things a container needs and the loop cannot do

1. `HOME=/data`, so `config.HOME` (`Path.home() / ".claudeloop"`) and the CLI's
   own state both resolve into the persistent volume.
2. Git identity from the addon options, and `commit.gpgsign false` — the first
   landmine, addressed at the container's global git config rather than through
   `[session_env]`'s `GIT_CONFIG_COUNT` trio. It reaches sessions the same way:
   `child_env` starts from `os.environ`, and a `[session_env]` entry still wins.
3. Seeds `~/.claude.json` with `hasCompletedOnboarding` and
   `bypassPermissionsModeAccepted` when it does not exist — the third landmine.
   A headless `claude -p` that stops to ask whether this folder is trusted
   produces no result file, and the loop would nudge it until `max_resumes`.
4. `exec python3 -m claudeloop`, so the loop is PID 1's child and the
   supervisor's stop signal reaches it rather than a shell.

### Landmines, and which of them this slice actually closes

| Landmine | Outcome |
|---|---|
| Commit signing | Closed, by `commit.gpgsign false` in the image's global git config. The `[session_env]` route still exists and still wins for an operator who needs a real key. |
| Claude-in-Chrome cannot run headless | Not closed, and not closeable here: it is a statement about what an operator writes in `instructions.md` for a repository that mandates a browser sweep. Documented in `DOCS.md`. |
| First-run trust prompts | Closed, by seeding `~/.claude.json`. |
| Secrets the target repo needs | Already `[session_env]`, and it reaches the wizard. Nothing to build. |
| Claude authentication | Closed, by the `claude_code_oauth_token` option. |

### An unwritable tasks file is a paid infinite loop, and the config now says so

The second thing running the image found, and it is not an addon defect: it is
`FileSource._rewrite` suppressing the `OSError` from its write. That suppression
is deliberate and documented — the checklist is the operator's file and may
vanish mid-run — but the consequence was never followed through. An unwritten
mark leaves the line `- [ ]`, so the loop offers that task again on the next
poll, and again, and pays every time. Nothing is logged.

The addon is what makes it ordinary rather than exotic: the loop now runs as
uid 1000 and a checklist on `/share` belongs to root. Measured: **37 runs of
one task in fifteen minutes, $1.10**, and the only reason it stopped is that a
human was watching a terminal — which is the one thing this project assumes
nobody is doing.

Two fixes, at both ends:

- `tasks_file` gains a writability check in `SCHEMA`, so `load_config` refuses
  it and the wizard marks the field. A file that does not exist yet still
  passes: `pending()` reads a missing checklist as an empty backlog, so there
  is nothing to mark and nothing to loop on.
- `_rewrite` still does not raise, but it now logs an error naming the task and
  the file. Permissions can change under a running loop, and the check at load
  cannot cover that.

## What is not being built

- **No addon-options mirror of `config.toml`.** Above.
- **No auto-update, no watchdog, no `homeassistant_api` access.** The loop has
  no reason to talk to Home Assistant, and an addon that can is a bigger blast
  radius for an agent running with bypassed permissions.
- **No published port.** Ingress only. An operator who wants the dashboard on
  the LAN can still say so — but then they are back to `web_host` and
  `web_token`, which already work and are already refused without a token.
- **No multi-arch CI.** `build.yaml` names `aarch64` and `amd64`, the two
  architectures Home Assistant OS actually ships on hardware people leave
  running.
