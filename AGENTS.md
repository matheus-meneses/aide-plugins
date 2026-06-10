# AGENTS.md — aide-plugins

## Purpose

This repo contains community plugins for [aide](https://github.com/matheus-meneses/aide). Each plugin is
an isolated Python package run in a sandboxed subprocess by the aide CLI.

---

## Identity

- Write idiomatic Python 3.11+. Use type annotations everywhere.
- No narrating comments. Only comments that explain *why*.
- **NEVER write to stdout.** The `aide_sdk` runtime redirects `sys.stdout → stderr` at startup to
  reserve stdout for the JSON protocol.
- Use `$AIDE_HOME`, not hardcoded `~/.aide`. See the sessions helper below.
- Declare every outbound hostname in `capabilities.network`. The sandbox denies undeclared hosts.
- Secrets injected at runtime must never be written to disk or logged.

---

## Logging

Every scraper has a `self.log` attribute (a `Logger` instance from `aide_sdk`) configured
automatically by `runtime.serve()` from the request context before any action runs. You do not
need to instantiate it.

```python
self.log.debug("Connecting to service...")   # hidden by default, shown with aide -v
self.log.info("Run complete")                # always visible
self.log.warning("No items returned; check credentials")  # always visible
self.log.error(f"Authentication failed: {e}")             # always visible
```

Rules:
- Progress chatter (connecting, page counts, pagination steps) → `self.log.debug`.
- Degraded-but-recoverable situations → `self.log.warning`.
- Failures that result in partial or empty data → `self.log.error`.
- **Never** call `print()` in scraper code. Never write to `sys.stdout` or `sys.stderr` directly.
- `self.log` writes to the real stderr, unaffected by the `sys.stdout` redirect in `runtime.py`.

The log level and format are selected by the user via `aide -v` / `aide --log-format json` and
passed through `Request.Context` as `log_level` and `log_format`. The `Logger.from_context()`
class method reads them; this is called by the runtime — you do not need to call it yourself.

## TLS

TLS is the **CLI's** concern, never the plugin's. The CLI resolves the policy (flag >
per-source config > global config > secure default) and injects two runtime values into every
plugin run via `Request.Context`:

| Key | Default | Set by |
|-----|---------|--------|
| `verify_ssl` | `true` | `aide --verify-ssl=false`, or `settings.tls.verify_ssl` / `sources.<name>.tls.verify_ssl` |
| `ca_bundle` | `""` | `aide --ca-bundle <pem>`, or `settings.tls.ca_bundle` / `sources.<name>.tls.ca_bundle` |

**Trusting a corporate CA is automatic** and needs **zero plugin code**. When verification is on,
the Python SDK resolves trust in this order before your scraper runs:

1. **Explicit `ca_bundle`** → exported as `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` so `requests`,
   `python-gitlab`, `httpx`, and `urllib` use it.
2. **Otherwise the OS trust store** → the SDK calls `truststore.inject_into_ssl()`, so any CA your
   IT/MDM already installed in the system keychain (corporate MITM proxies like Zscaler, internal
   roots) is trusted without a PEM file. This is the common corporate case.

For the bypass case (which no env var can express for `requests`), pass the SDK's single resolved
value to your client. `self.tls_verify` is `False`, `True`, or the CA path — exactly the shape
`requests`/`python-gitlab` expect:

```python
client = SomeClient(base_url, token=token, ssl_verify=self.tls_verify)
```

Go plugins read `plugin.VerifySSL` / `plugin.CABundle`, or use the prebuilt `plugin.TLSConfig()`.
Both SDKs default to secure (`verify_ssl: true`, no bundle) when the keys are absent. Never
hardcode `verify=False` / `ssl_verify=False`, and never read TLS settings from plugin config.

---

## Plugin layout

```
plugins/<name>/
├── plugin.yaml       manifest (required)
├── requirements.txt  pip deps (required)
├── __main__.py       entry-point (required, see template below)
└── scraper.py        your BaseScraper subclass
```

### `__main__.py` (copy verbatim, change only the import)

```python
from aide_sdk.runtime import serve
from scraper import MyScraper

if __name__ == "__main__":
    serve(MyScraper)
```

---

## Manifest — `plugin.yaml`

All keys verified against `plugins/jira/plugin.yaml`:

```yaml
name: my-plugin          # snake_case, matches the directory name
version: 1.0.0
runtime: python
description: "One-line description"
categories: [task]       # subset of: absence | approval | metric | alert | task | event

entrypoint:
  python:
    script: __main__.py

requirements: requirements.txt

config:                  # list of config fields shown in aide's TUI
  - { key: base_url, label: "Service URL", required: true }
  - key: queries         # complex field example
    label: "Query list"
    required: true
    type: object_list    # or: string_list, integer, string (default)
    fields:
      - { key: name, label: "Name", required: true }
      - { key: jql,  label: "Query", required: true }
      - { key: mode, label: "Mode (items/metric)", default: "items" }

credentials:             # stored in OS keychain, injected as env at runtime
  - { key: email, label: "Email" }
  - { key: token, label: "API Token", secret: true }

capabilities:
  network: ["*.atlassian.net"]   # glob list of allowed outbound hosts
  filesystem: []                 # list of allowed paths (usually empty)
  # browser: true                # uncomment for Playwright plugins

render:
  custom: false           # true → implement render() in your scraper

tools:                   # optional: expose named query actions to the agent
  - name: fetch_item
    description: "Fetch a single item by ID."
    params:
      id: "required, e.g. PROJ-123"
```

---

## `BaseScraper` contract

From `aide_sdk/base.py`:

```python
class BaseScraper(ABC):
    name: str = ""
    version: str = "0.1.0"
    categories: ClassVar[list[str]] = []

    # Required
    @abstractmethod
    def scrape(self, config: dict[str, Any], secrets: dict[str, Any]) -> list[ScraperEntry]: ...

    # Optional — return [] by default
    def scrape_team(self, config, secrets) -> list[TeamMemberEntry]: ...
    def scrape_metrics(self, config, secrets) -> list[MetricEntry]: ...

    # Optional — no-ops by default
    def authenticate(self, config, secrets) -> None: ...
    def validate_config(self, config) -> None: ...

    # Optional — raise NotImplementedError by default
    def query(self, name, params, config, secrets) -> str: ...
    def render(self, heading, items, config) -> list[str]: ...
```

Execution order for the `scrape` action:
`validate_config` → `authenticate` → `scrape` → `scrape_team` → `scrape_metrics`

---

## Models

From `aide_sdk/models.py` (pydantic v2):

```python
class ScraperEntry(BaseModel):
    member: str
    category: Literal["absence", "approval", "metric", "alert", "task", "event"]
    title: str
    detail: str | None = None
    entry_date: date               # datetime.date
    priority: Literal["info", "warning", "critical"] = "info"
    link: str | None = None
    metadata: dict[str, Any] | None = None

class TeamMemberEntry(BaseModel):
    name: str
    email: str = ""
    role: str = ""
    department: str = ""
    branch: str = ""
    registration: str = ""
    manager_registration: str = ""  # empty string = no manager (root)

class MetricEntry(BaseModel):
    name: str
    value: float

PluginEntry = ScraperEntry  # alias
```

---

## JSON protocol (aide_sdk/runtime.py)

The CLI sends a single JSON object on **stdin** and reads a single JSON object from **stdout**.

### Inbound (stdin)

| Action     | Additional fields                              |
|------------|------------------------------------------------|
| `describe` | —                                              |
| `scrape`   | `config`, `secrets`                            |
| `render`   | `heading`, `items: list[dict]`, `config`       |
| `query`    | `name`, `params: dict`, `config`, `secrets`    |

### Outbound (stdout)

Success:
```json
{
  "protocol_version": "1",
  "ok": true,
  "entries": [...],
  "team_members": [...],
  "metrics": [...]
}
```

Failure:
```json
{ "ok": false, "error": "description" }
```

**Critical:** `sys.stdout` is redirected to `sys.stderr` by `runtime.serve()` before your code
runs. Any `print()` call goes to stderr and is never seen by the CLI. This is intentional.

---

## Sandbox

| OS      | Mechanism             | Network              | Write access                    |
|---------|-----------------------|----------------------|---------------------------------|
| macOS   | `sandbox-exec`        | declared hosts only  | plugin dir under `AIDE_HOME`    |
| Linux   | `bwrap`               | `--unshare-net` if no network declared | plugin dir |
| Windows | none (warning logged) | unrestricted         | unrestricted                    |

`browser: true` in the manifest relaxes the macOS/Linux sandbox to allow Playwright's browser path.

---

## Go plugins

Plugins are not limited to Python. A plugin may declare `runtime: go` and ship a compiled binary
instead of a Python package. The host runs `<plugin_dir>/bin/<entrypoint.go.binary>` over the same
JSON-on-stdin/stdout protocol — there is no `.venv`, and the sandbox table above still applies.

Layout:

```
plugins/<name>/
├── plugin.yaml       runtime: go, entrypoint.go.binary: <name>
├── go.mod
├── main.go
└── bin/<name>        compiled binary (one per platform in the registry)
```

Manifest delta from the Python form:

```yaml
runtime: go
entrypoint:
  go:
    binary: my-go-source     # → executed at bin/my-go-source (bin/my-go-source.exe on Windows)
```

The SDK is `github.com/matheus-meneses/aide-sdk-go` (the `sdk/go` package in the aide repo).
Implement a `Handler` and pass it to `plugin.Serve`. There is no `BaseScraper`; you construct the
`Response` directly.

```go
package main

import sdk "github.com/matheus-meneses/aide-sdk-go"

type handler struct{}

func (handler) Handle(req *sdk.Request) (*sdk.Response, error) {
	sdk.Log.Debugf("connecting...")          // hidden unless `aide -v`
	return &sdk.Response{OK: true, Entries: []any{ /* ScraperEntry-shaped maps */ }}, nil
}

func main() { sdk.Serve(handler{}) }
```

Rules unchanged: stdout is reserved for the protocol (`plugin.Serve` writes the response there) —
log only via `sdk.Log.Debugf/Infof/Warnf/Errorf`, which go to stderr. Declare every outbound host
in `capabilities.network`. Never persist secrets. In the registry, Go plugins publish one artifact
per platform under the keys `go/<goos>_<goarch>` (e.g. `go/darwin_arm64`, `go/linux_amd64`).

---

## State and sessions

Use `$AIDE_HOME` (not `~/.aide`). Canonical helper:

```python
import os
from pathlib import Path

def _sessions_dir() -> Path:
    aide_home = os.environ.get("AIDE_HOME") or str(Path.home() / ".aide")
    p = Path(aide_home) / "plugins" / "<name>" / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p
```

---

## Lint

Plugins must pass `ruff check` using the shared `ruff.toml` at the repo root:

```
cd aide-plugins && ruff check plugins --fix && ruff format plugins
```

Rules in effect: `E`, `F`, `W`, `I` (import order), `UP`, `B`, `C4`, `SIM`, `RUF`. `E501` (line length)
is ignored — use your judgement.

---

## Adding a new plugin checklist

1. Scaffold: `aide dev new <name> --runtime python --category <cat>` (creates `plugin.yaml`, `requirements.txt`, `__main__.py`, `scraper.py`, `AGENTS.md`).
2. Subclass `BaseScraper`; implement `scrape()` at minimum.
3. Set `name`, `version`, `categories` as class attributes.
4. Declare all outbound hosts in `capabilities.network`.
5. Use `self.log.debug/info/warning/error` for all output. No `print()`, no direct `sys.stderr`.
6. Run `ruff check plugins/<name> --fix && ruff format plugins/<name>`.
7. Validate the manifest: `aide dev validate plugins/<name>` (add `--json` for structured errors).
8. Test locally: `aide dev test plugins/<name>` runs `scrape` and prints entries; `--json` emits `{ok, entries, ..., logs, exit_code}`; add `-v` for debug logs. No install required.
