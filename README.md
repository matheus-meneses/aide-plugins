# aide-plugins

**The official plugin collection for [aide](https://github.com/matheus-meneses/aide) — and a blueprint for building your own.**

[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

## What this repo is

This repository ships the plugins that power aide out of the box — the connectors that turn aide from an empty shell into something that knows about your tickets, reviews, approvals, calendar, and absences.

More importantly, it is a **working example of an aide plugin registry**. Fork it, drop in your organisation's internal scrapers, publish the index, and point your team's `config.yaml` at it. Everyone then installs your private connectors with a single `aide plugin install my-company-tool` — no public disclosure, no extra workflow. This is how you build a private plugin marketplace.

## Installing plugins

```sh
aide plugin install <name>        # fetch and set up a plugin from the registry
aide config source add <name>     # interactive wizard to configure it
aide run                          # collect from every enabled source
```

Browse what is available:

```sh
aide plugin list --available
```

Each plugin declares the credentials it needs; `aide credential set <name>` prompts you for exactly those fields and stores secrets in your OS keychain.

## Writing a plugin

A plugin is a self-contained directory:

```
plugins/<name>/
├── plugin.yaml       manifest (required)
├── requirements.txt  pip dependencies (required)
├── __main__.py       entry-point (required)
└── scraper.py        your BaseScraper subclass
```

`__main__.py` is boilerplate — copy it verbatim and change only the import:

```python
from aide_sdk.runtime import serve
from scraper import MyScraper

if __name__ == "__main__":
    serve(MyScraper)
```

Your scraper subclasses `BaseScraper` and implements `scrape()`:

```python
from datetime import date

from aide_sdk import BaseScraper, ScraperEntry


class MyScraper(BaseScraper):
    name = "my-source"
    version = "1.0.0"
    categories = ["task"]

    def scrape(self, config, secrets):
        self.log.debug("connecting...")     # hidden unless `aide -v`
        return [
            ScraperEntry(
                member="alice",
                category="task",
                title="Something needs attention",
                entry_date=date.today(),
                priority="warning",
            )
        ]
```

The manifest declares config fields, credentials, the sandbox policy, and any agent tools:

```yaml
name: my-source
version: 1.0.0
runtime: python
description: "My internal source"
categories: [task]            # subset of: absence | approval | metric | alert | task | event
entrypoint:
  python:
    script: __main__.py
requirements: requirements.txt
config:
  - { key: base_url, label: "Service URL", required: true }
credentials:
  - { key: token, label: "API Token", secret: true }
capabilities:
  network: ["api.my-company.com"]   # only these hosts are reachable from the sandbox
  filesystem: []
  # browser: true                   # uncomment for Playwright-based plugins
tools:
  - name: fetch_item
    description: "Fetch a single item by ID."
    params:
      id: "required, e.g. PROJ-123"
```

A few rules that matter:

- **Never write to stdout.** It is reserved for the JSON protocol; the SDK redirects `sys.stdout` to stderr at startup. Use `self.log.debug/info/warning/error` for everything.
- **Declare every outbound host** in `capabilities.network` — the sandbox blocks anything you do not list.
- **Secrets are injected at runtime** as the `secrets` dict. Never write them to disk or log them.
- **Use `$AIDE_HOME`** for any state (e.g. browser sessions under `$AIDE_HOME/plugins/<name>/sessions/`), not a hardcoded path.

[AGENTS.md](AGENTS.md) is the exhaustive contract: the full `BaseScraper` API, the JSON protocol, the model definitions, and the per-OS sandbox table.

### Writing a plugin in Go

The host talks the same JSON-over-stdin/stdout protocol to any runtime, so a plugin can also be written in Go. Instead of a `.venv`, a Go plugin ships a compiled binary at `bin/<binary>`:

```
plugins/<name>/
├── plugin.yaml       runtime: go, entrypoint.go.binary: <name>
├── go.mod
├── main.go
└── bin/<name>        compiled binary (per-platform in the registry)
```

The manifest sets the Go runtime and entry-point binary:

```yaml
name: my-go-source
version: 1.0.0
runtime: go
entrypoint:
  go:
    binary: my-go-source
capabilities:
  network: ["api.my-company.com"]
```

The code uses the Go SDK (`github.com/matheus-meneses/aide-sdk-go`, in the [aide repo](https://github.com/matheus-meneses/aide) under `sdk/go`): implement a `Handler` and hand it to `plugin.Serve`. There is no `BaseScraper` in Go — you build the `Response` yourself.

```go
package main

import sdk "github.com/matheus-meneses/aide-sdk-go"

type handler struct{}

func (handler) Handle(req *sdk.Request) (*sdk.Response, error) {
	sdk.Log.Infof("collecting from my source")
	return &sdk.Response{
		OK: true,
		Entries: []any{
			map[string]any{
				"member":     "alice",
				"category":   "task",
				"title":      "Something needs attention",
				"entry_date": "2026-01-01",
				"priority":   "warning",
			},
		},
	}, nil
}

func main() { sdk.Serve(handler{}) }
```

The same rules apply: stdout is reserved for the protocol (use `sdk.Log.*`, which writes to stderr), declare every outbound host in `capabilities.network`, and never persist secrets. In the registry, Go plugins publish one artifact per platform under the keys `go/<os>_<arch>` (e.g. `go/darwin_arm64`, `go/linux_amd64`).

## Running your own registry

A registry is a single `index.yaml` listing each plugin, the version(s) available, and where to download the packaged artifact. The aide CLI fetches the manifest first (to show you what the plugin can access), then downloads the artifact and verifies its SHA-256 before extracting it:

```yaml
plugins:
  my-source:
    latest: 1.0.0
    versions:
      - version: 1.0.0
        manifest_url: "https://github.com/my-org/my-aide-plugins/releases/latest/download/my-source-1.0.0.plugin.yaml"
        artifacts:
          python:
            url: "https://github.com/my-org/my-aide-plugins/releases/latest/download/my-source-1.0.0.tar.gz"
            sha256: "<sha256 of the tarball>"
```

You don't write this by hand. [`scripts/build-registry.sh`](scripts/build-registry.sh) packages every plugin under `plugins/` into a `.tar.gz`, computes its checksum, and generates `index.yaml`:

```sh
REPO=my-org/my-aide-plugins bash scripts/build-registry.sh
# → registry/index.yaml + dist/*.tar.gz + dist/*.plugin.yaml + dist/index.yaml
```

Publishing is automated by [`.github/workflows/release.yml`](.github/workflows/release.yml): push a `v*` tag and it runs the script and uploads `index.yaml` and every artifact as assets on the GitHub Release. Because the assets live at `releases/latest/download/`, the index always resolves to the newest release. Then point aide at it:

```yaml
# config.yaml
registries:
  - https://github.com/my-org/my-aide-plugins/releases/latest/download/index.yaml
```

aide merges every configured registry with the builtin one (builtin wins on name collisions). Private GitHub repositories authenticate with `GH_TOKEN` / `GITHUB_TOKEN` or `gh auth token`. Any HTTPS host works — an internal S3 bucket, Artifactory, or a static site — as long as the `index.yaml` and the artifact URLs it references are reachable.

## Dev workflow

Test a plugin straight from the working tree before publishing:

```sh
aide plugin install --local plugins/my-source --yes
ruff check plugins --fix && ruff format plugins
```

## Contributing

New connectors are very welcome — follow the checklist in [AGENTS.md](AGENTS.md) and make sure `ruff check plugins` passes.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
