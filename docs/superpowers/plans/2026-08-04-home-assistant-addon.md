# S4 — Home Assistant OS addon: implementation plan

Spec: `docs/superpowers/specs/2026-08-04-claudeloop-home-assistant-addon-design.md`

Two halves. Steps 1–4 are the ingress mode, which is code with tests. Steps 5–7
are the packaging, which is four files no unit test can meaningfully execute —
they are verified by building and running the image.

---

## Step 1 — `config.ingress()`

**Test** (`tests/test_config.py`):

```python
class IngressTest(unittest.TestCase):
    def _set(self, value: str | None):
        old = os.environ.get(config.INGRESS_ENV)
        if value is None:
            os.environ.pop(config.INGRESS_ENV, None)
        else:
            os.environ[config.INGRESS_ENV] = value
        self.addCleanup(
            lambda: os.environ.__setitem__(config.INGRESS_ENV, old)
            if old is not None
            else os.environ.pop(config.INGRESS_ENV, None)
        )

    def test_off_by_default(self):
        self._set(None)
        self.assertFalse(config.ingress())

    def test_on_when_set_to_one(self):
        self._set("1")
        self.assertTrue(config.ingress())

    def test_anything_else_is_off(self):
        # run.sh sets exactly "1"; a stray "false"/"0"/"" from a hand-written
        # docker run must not drop the Host and token checks.
        for value in ("0", "", "false", "true", "yes"):
            self._set(value)
            self.assertFalse(config.ingress(), value)
```

**Code** (`claudeloop/config.py`, next to the host constants):

```python
INGRESS_ENV = "CLAUDELOOP_INGRESS"
INGRESS_HOST = "0.0.0.0"
INGRESS_PORT = 8765
"""Must match addon/config.yaml's ingress_port. Under ingress the bind is
ClaudeLoop's own decision, not the operator's: web_host and web_port describe a
listener a browser reaches directly, and there isn't one."""


def ingress() -> bool:
    """Whether this process is behind Home Assistant's ingress proxy."""
    return os.environ.get(INGRESS_ENV) == "1"
```

## Step 2 — the dashboard binds and answers through the proxy

**Test** (`tests/test_web.py`): a subclass of `WebTestBase` that sets the
variable in `setUp` before `web.serve` runs.

```python
class IngressTest(WebTestBase):
    web_host = "127.0.0.1"

    def setUp(self):
        os.environ[config.INGRESS_ENV] = "1"
        self.addCleanup(os.environ.pop, config.INGRESS_ENV, None)
        super().setUp()

    def test_it_binds_every_interface(self):
        self.assertEqual(self.server.server_address[0], config.INGRESS_HOST)

    def test_home_assistants_host_header_is_accepted(self):
        self.assertEqual(self._request("homeassistant.local:8123"), 200)

    def test_the_token_is_not_required(self):
        # web_token is set, and no query string carries it: the supervisor
        # authenticated the user before this request existed.
        ...
```

plus, in the existing non-ingress `HostHeaderTest`, an assertion that the same
foreign `Host` is still a 403 — the point being that the ingress path is the
only thing that relaxes it.

**Code** (`claudeloop/web.py`): early returns in `_host_allowed` and
`_authorized`, and

```python
def serve(cfg: Config) -> ThreadingHTTPServer:
    host, port = (INGRESS_HOST, INGRESS_PORT) if ingress() else (cfg.web_host, cfg.web_port)
    server = _Server((host, port), Handler, cfg)
```

`server_port` is what the log line reports, since `cfg.web_port` may be 0 in
tests.

## Step 3 — the setup wizard binds and answers through the proxy

**Test** (`tests/test_setup.py`): the same shape against `setup.serve`, with
the wizard's own extra assertion that a request carrying **no token at all**
gets the schema.

**Code** (`claudeloop/setup.py`): the same tuple in `serve`, and `run_setup`'s
console line becomes a sidebar instruction under ingress — there is no URL to
print that an operator could use.

## Step 4 — both pages derive their base path

**Code** (`claudeloop/static/index.html`, `claudeloop/static/setup.html`):

```js
// Under Home Assistant ingress the page is served from
// /api/hassio_ingress/<session>/, and an absolute path escapes that prefix.
const BASE = location.pathname.replace(/\/[^/]*$/, "");
```

with `url()` prefixing it. `""` at `/`, so the direct case is unchanged.

**Test:** none that runs. No automated test executes either page's JavaScript
(a known gap in `ROADMAP.md`), so this is checked in a browser and with `node`
against the three pathnames that matter.

## Step 5 — `addon/Dockerfile`, `addon/build.yaml`

Base image plus a copy, per the spec. Verified by building it.

## Step 6 — `addon/config.yaml`, `repository.yaml`

`ingress: true`, `ingress_port: 8765`, no `ports:`, four options.

## Step 7 — `addon/run.sh`, `addon/DOCS.md`

`HOME=/data`, git identity, `~/.claude.json` seed, `exec`.

---

## Verification

- `python -m unittest discover -s tests -t .`
- `podman build` the image, run it with `CLAUDELOOP_INGRESS=1`, and check
  through a proxy that sends Home Assistant's own `Host` header that the wizard
  is served and the page's requests resolve.
- The live smoke test: two tasks on `haiku` against a scratch repository,
  driven **from inside the container**, since that is the environment the whole
  slice is about.
