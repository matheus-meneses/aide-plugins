# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-10

### Added

- Public plugin registry with `gitlab`, `jira`, and `outlook` plugins.
- Tag-driven release pipeline: a `vX.Y.Z` tag stamps every plugin artifact, a
  guard step aborts when a `plugin.yaml` version does not match the tag, and
  artifact URLs are pinned to the tagged release.
- Cross-compilation of `runtime: go` plugins for `darwin/amd64`,
  `darwin/arm64`, `linux/amd64`, `linux/arm64`, and `windows/amd64`.
- Lint CI running `ruff check` and `ruff format --check` on every push and pull
  request.

### Changed

- Plugins honor the `verify_ssl` runtime value from the request context; the
  `gitlab` plugin no longer hardcodes `ssl_verify=False`.

[Unreleased]: https://github.com/matheus-meneses/aide-plugins/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/matheus-meneses/aide-plugins/releases/tag/v0.1.0
