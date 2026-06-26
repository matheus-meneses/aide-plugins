# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Outlook** — three new config keys and richer event data:
  - `fetch_events` (integer, default `1`) — set to `0` to disable calendar event scraping entirely.
  - `email_count` (integer, default `0`) — when > 0, surfaces up to N unread inbox messages as
    individual `email` category items alongside the existing unread-count metric.
  - `email_detail` (string, default `metadata`) — controls how much information is included per
    email item: `metadata` (subject, sender, received time), `preview` (+body preview ≤ 300 chars),
    or `full` (+full body text, HTML stripped, hard-capped at 2 000 characters to protect the LLM
    token budget).
  - Calendar events now include the online meeting join URL (Teams, Zoom, etc.) when available,
    rendered as `↗ <url>` below the meeting line.

## [0.2.0] - 2026-06-20

### Added

- **SailPoint** — a new builtin source for SailPoint IdentityNow access-request
  approvals and certifications (browser-based, SSO via Microsoft login).
- Every builtin plugin now ships an `icon` (embedded `data:` SVG) so the
  Marketplace shows a per-plugin logo instead of a generic placeholder.
  GitLab and Jira use their official brand marks; Outlook and SailPoint use
  brand-colored marks (official assets are not freely redistributable).

### Fixed

- **Outlook** — calendar events are now bucketed and displayed in your local
  time zone instead of UTC, so meeting times match what you see in Outlook.

## [0.1.0] - 2026-06-12

### Added

- Three ready-to-use sources, each installable with `aide plugin install <name>`:
  - **GitLab** — your merge requests, work items, and reviews.
  - **Jira** — assigned issues, pending approvals, and ticket metrics.
  - **Outlook** — upcoming calendar events and unread inbox count.
- A public plugin registry, so every plugin installs with a single command.
- Automatic support for corporate TLS / MITM proxies: plugins honor aide's
  `verify_ssl` / `ca_bundle` and otherwise fall back to the OS trust store, so
  roots already installed by IT just work.

### Changed

- Plugins install their SDK from PyPI (`aide-plugin-sdk`), so a clean install
  resolves it without setting `AIDE_SDK_PATH`.

[Unreleased]: https://github.com/matheus-meneses/aide-plugins/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/matheus-meneses/aide-plugins/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/matheus-meneses/aide-plugins/releases/tag/v0.1.0
