# aide-plugins

Builtin plugins and the registry index for [aide](https://github.com/matheus-meneses/aide).

## Layout

- `registry/index.yaml` — the registry index. Published as a GitHub Release asset so the
  aide CLI can resolve it from the latest release. This is the builtin registry; aide merges
  it with any user-configured registries (builtin wins on name collisions).
- `plugins/<name>/` — one self-contained plugin per directory: `plugin.yaml`, `requirements.txt`
  (Python), and the plugin source/binary.

## Registry index format

```yaml
plugins:
  <name>:
    latest: <version>
    versions:
      - version: <version>
        manifest_url: "<url to plugin.yaml>"
        artifacts:
          python: { url: "...", sha256: "...", signature: "..." }
          go:
            darwin_arm64: { url: "...", sha256: "...", signature: "..." }
```

## Auth

This repository is private during development; the aide CLI authenticates registry and asset
requests with `GH_TOKEN`/`GITHUB_TOKEN` or `gh auth token`. It will be made public before the
open-source release.
