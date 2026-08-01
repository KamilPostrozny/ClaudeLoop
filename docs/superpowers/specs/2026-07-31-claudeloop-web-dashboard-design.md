# ClaudeLoop — S2a Read-Only Web Dashboard — Design

Date: 2026-07-31
Status: approved, ready for implementation planning

## Goal

A live web view of what the orchestrator is doing, reachable from a phone on
the local network. It answers, at a glance: is the loop alive, what is it
working on right now, what is the session saying, what is still queued, what
finished and how did it go, and how much quota is left.

S2a reads. It writes nothing and changes nothing in S1.

## Scope

S2 as described in the S1 design splits along one real seam. Everything except
the question box reads what S1 already writes. The question box writes: it
needs the session protocol to permit asking, the loop to pause on a `blocked`
result, and a resume carrying the human's answer — changes to `loop.py` and
`session.py`.

- **S2a (this document):** the read-only dashboard.
- **S2b (later):** the question and answer channel.

Splitting them keeps the loop's semantics still while the view of those
semantics is being built, and S2b is better specified once there is a
dashboard to look at.

## What S1 already provides

No new plumbing is required. S1 emits exactly what this needs:

- `~/.claudeloop/state.db` in WAL mode — concurrent reads while the loop
  writes were a stated design goal, not an accident.
- `~/.claudeloop/runs/<task-id>/events.jsonl` — the complete, unfiltered
  `stream-json` stream, appended per attempt. `session.run` filters only the
  list it *returns*; the file on disk keeps everything.
- The `tasks` table carries `status`, `summary`, `question`, `cost_usd`, and
  timings. The `runs` table distinguishes a quota wait (`exit_reason =
  "RateLimited"`) from a plain nudge (`"Nudge"`), so lost wall time is
  answerable.
- The `rate_limit_event` stream message carries current quota headroom
  continuously, including while the quota is fine.

## Decisions

### Process model: same process, side thread

`ThreadingHTTPServer` on a daemon thread, started by `main_loop` before it
enters the task loop. One `python -m claudeloop`, one port, one container for
S4, and liveness is known rather than inferred.

The thread opens its own read-only sqlite connection and reads `events.jsonl`
directly off disk. It never touches the loop's objects, so no fault in the web
layer can corrupt loop state.

The standard library has no asyncio HTTP server, so the UI cannot simply live
on the loop's existing event loop; a thread is the stdlib answer. Each SSE
viewer holds one thread for the life of its connection, which is fine for the
handful of viewers this will ever have.

Rejected: a separate process. It survives a UI crash independently, but costs
a second process to supervise in the S4 addon and forces liveness to be
inferred from database timestamps rather than simply known.

### The loop/web boundary: an atomic snapshot

One value crosses the thread boundary — a frozen dataclass in
`claudeloop/status.py`. The loop *replaces* the module-level instance on every
transition; the web thread reads the reference.

```python
@dataclass(frozen=True)
class Status:
    state: str                 # "idle" | "running" | "waiting" | "error"
    task_id: str | None
    task_text: str | None
    run_dir: Path | None
    session_id: str | None
    attempt: int
    started_at: float | None
    wait_until: float | None   # set while sleeping off a quota block
    rate_limit: dict | None    # last rate_limit_info seen, for the gauge
    last_error: str | None
    heartbeat: float
```

An atomic reference swap needs no lock and cannot tear: a reader sees either
the old snapshot whole or the new one whole. A mutable shared object with
per-field assignment would allow a torn read across fields — the reason for
the frozen replace.

`heartbeat` is what makes liveness real. Same process means that if the loop
task dies while the thread survives, the timestamp goes stale and the UI says
so instead of showing a cheerful green light over a dead orchestrator.

### Live output: Server-Sent Events

`EventSource` is native, handles reconnection itself, and needs no library.
The handler tails `events.jsonl` by byte offset: on connect it replays the
last ~200 rendered entries, then polls for new bytes every 0.5s. The stream
ends on `BrokenPipeError` (client gone) or when the run directory changes.

Rejected: polling the whole log. Simpler server, but laggy and chatty, and
`EventSource` already solves reconnection.

### Output detail: assistant text plus tool names

What Claude Code itself shows. Prose as it arrives, plus one line per tool
call; tool results collapsed behind a click. Raw output is unreadable on a
phone — a single file read can be tens of thousands of characters and would
bury the prose entirely.

### Access: localhost by default, token to go wider

```toml
web_host  = "127.0.0.1"
web_port  = 8765
web_token = ""
```

**Binding a non-loopback host with an empty `web_token` is a startup error,
not a warning.** This dashboard watches an agent that holds production
credentials; exposure must be a deliberate act, never the result of a
forgotten flag.

The token travels as a query parameter because `EventSource` cannot set
request headers, and is compared with `secrets.compare_digest`. The page
carries it forward from `location.search`. In S4 the Home Assistant ingress
sits in front and handles authentication.

### Theme: both, following the system

`prefers-color-scheme` with an explicit toggle, matching what the Claude apps
do. Roughly double the colour decisions, accepted deliberately.

## Visual design

The logo is effectively the palette: an orange loop, a navy loop, a warm
field.

| Token | Dark | Light |
|---|---|---|
| Background | `#1B222B` | `#FDF4EC` |
| Surface | `#242D37` | `#FFFFFF` |
| Text | `#F2E8E0` | `#242D37` |
| Muted text | `#9AA6B2` | `#6B7885` |
| Accent (fills, indicator) | `#FD7C33` | `#FD7C33` |
| Accent (text, links) | `#FD7C33` | `#C4551A` |

`#FD7C33` on white is about 2.6:1 and fails as text, so the light theme uses a
darkened accent for type while keeping the true brand orange for fills and the
indicator. On `#242D37` the brand orange is around 5.5:1 and is used directly.

The logo ships as `claudeloop/static/logo.png` and doubles as the favicon and,
later, the Home Assistant sidebar icon. Its background is baked in with no
alpha, so it sits inside a deliberate rounded warm chip in the header — that
reads as a brand lockup on either theme and needs no new asset. The infinity
mark alone carries the small sizes; the wordmark appears only at header size.

### Status indicator

| Colour | State | Meaning |
|---|---|---|
| Green, pulsing | `running` | a session is working |
| Amber | `waiting` | sleeping off a quota block, with a live countdown to `resetsAt` |
| Grey | `idle` | no pending tasks, polling every 30s |
| Red | `error` | last task `failed` or `interrupted`, or the crash handler fired |

The state is always rendered as a dot **and** a text label. Colour alone never
carries meaning.

"Orange while waiting for you" arrives with S2b; nothing in S2a can block on a
human.

### Quota gauge

The meter's fill comes straight from `rate_limit_info.utilization` (0..1),
clamped defensively and hidden when the field is absent — older payloads
won't carry it. This replaced an earlier build that tried to reconstruct the
window itself: it kept a `WINDOWS` table of known `rateLimitType` values
(`five_hour`, `weekly`, `opus_weekly`) and subtracted a window length back off
`resetsAt` to get a start time. A live smoke test's real `rateLimitType` was
`seven_day`, which matched nothing in that table, so the meter always fell to
its unknown-window branch and rendered an empty track. `utilization` makes the
whole reconstruction unnecessary — it's the ratio the meter wants, delivered
directly, for any `rateLimitType` including ones never seen before.

The countdown to `resetsAt` is unaffected and orthogonal: it still just needs
a reset time, not a window length, so it renders independently of whether
`utilization` is present. `rateLimitType` is shown as a label, formatted by
spelling out the number word in the string (`seven_day` → "7 day") rather
than a name-to-duration lookup, for the same reason the fill no longer uses
one.

`status` colours the meter — `allowed_warning` or a blocking status render
distinct from plain `allowed` — using the same `"allowed"`-prefix rule as
`blocking_reset()` in `loop.py`, so the dashboard and the loop's own decision
never disagree about what a status means. `surpassedThreshold` (also 0..1) is
drawn as a thin marker on the track showing where the warning line sits.

## Architecture

### `claudeloop/status.py`

The frozen `Status` dataclass above, a module-level `current` holding the
latest instance, and `set_status(**changes)` which builds a replacement from
the existing one. Imported by both the loop and the web thread; imports
neither.

### `claudeloop/render.py`

`render_event(event: dict) -> dict | None` — pure, and tested against captured
fixtures exactly as `decide()` is. This is the only real logic in S2a.

| Input | Output |
|---|---|
| `assistant` with text content | `{"kind": "text", "text": ...}` |
| `assistant` with `tool_use` | `{"kind": "tool", "name": ..., "summary": ...}` |
| `user` with `tool_result` | `{"kind": "result", "preview": ...}` |
| `result` | `{"kind": "done", "cost": ..., "duration": ...}` |
| `rate_limit_event` | `None` — feeds the gauge, not the transcript |
| anything else | `None` |

`summary` is one line pulled from the tool input — `file_path`, `command`,
`pattern`, whichever is present — so a tool call reads as `Edit
src/foo.py` or `Bash npm test`.

### `claudeloop/web.py`

The request handler and the server thread. Named `web.py`, with its static
assets under `claudeloop/static/` — a `claudeloop/web/` directory alongside a
`claudeloop/web.py` module would be an import ambiguity.

| Route | Returns |
|---|---|
| `GET /` | the single-file page |
| `GET /logo.png` | the logo, also the favicon |
| `GET /api/state` | status snapshot, pending list, recent completed, quota |
| `GET /api/events` | SSE stream of rendered entries for the current run |
| `GET /api/tasks/<id>` | one task's row, its runs, and the last 2000 rendered entries of its log |

Pending tasks come from `FileSource(cfg.tasks_file).pending()` — the file
remains the source of truth for what is queued. Completed tasks come from the
`tasks` table, the only place summaries and cost live.

`serve(cfg)` starts the thread and returns; `main_loop` calls it before
entering the task loop.

### `claudeloop/static/index.html`

One file: inline CSS, inline `<script type="module">`, no build step and no
`node_modules`. The same no-build pattern as `assimo-store` and
`assimo-admin`, and the reason the S4 Dockerfile stays a base image plus a
copy. Mobile-first.

### Changes to existing files

- `config.py` gains `web_host`, `web_port`, `web_token`, with the non-loopback
  validation.
- `loop.py` calls `web.serve(cfg)` at startup and `status.set_status(...)` at
  each transition: task start, each attempt, entering a quota wait, task
  finish, crash, idle.

## Verification

`tests/test_render.py` — `render_event` against captured event fixtures,
covering every row of the table above including the `None` cases.

`tests/test_web.py` — the server booted on port 0 and driven with `urllib`:

1. `/api/state` reflects a `Status` set by the test.
2. A request to a non-loopback bind with an empty token is refused at startup.
3. A request with a wrong token is rejected; the right one is accepted.
4. A line appended to `events.jsonl` reaches an SSE client.

## Out of scope for S2a

Answering questions, pausing or resuming the loop, retrying a task, editing
the task list — every write. Home Assistant ingress and the sidebar are S4.
Authentication beyond the shared token; the ingress provides it where it
matters.

## Acceptance criteria

- With the loop idle, the page loads on `127.0.0.1:8765`, shows a grey `Idle`
  indicator, and lists the unchecked tasks from the task file.
- While a task runs, the indicator is green and pulsing, the current task text
  is shown, and assistant prose appears in the output block without a reload.
- A tool call appears as a single labelled line; its result is collapsed and
  expands on click.
- During a quota wait the indicator is amber and counts down to `resetsAt`.
- A finished task moves to the completed list with its status, summary, and
  cost, and opens to its log.
- Setting `web_host` to a non-loopback address with an empty `web_token`
  fails at startup with a message naming the token.
- The page is legible and usable at 380px wide, in both themes.
