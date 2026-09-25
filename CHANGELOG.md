# Changelog

All notable changes to claude-usage are documented here. Versioning follows
[semver](https://semver.org/): breaking changes to the JSON output contract bump the major
version, new fields or flags bump the minor, fixes bump the patch.

## [1.0.0] - 2026-09-25

First public release.

### Added

- Claude Code skill (`SKILL.md`) that checks live claude.ai subscription usage before large work
  and at phase boundaries, answers "how much have I got left?" questions, and announces every
  throttling decision it makes.
- Standalone CLI (`scripts/check-usage.py`, with `check-usage.sh` and `check-usage.cmd`
  launchers) reporting the 5-hour session, weekly, and per-model weekly limits. Flags: `--json`,
  `--fresh`, `--cache-only`, `--version`. Python 3.8+, standard library only; macOS, Linux, and
  Windows.
- Per-limit `posture` on a five-rung ladder (`normal`, `measured`, `frugal`, `conserve`,
  `wind_down`) driven by sustainable pace rather than raw fullness, capped by the wall-clock
  downtime that exhausting the limit would cause. A displayed 100% is always `wind_down`.
- Per-limit display `color` (`green`, `blue`, `yellow`, `orange`, `red`, `gray`) mapped from the
  final posture; gray when pace cannot be judged.
- Posture hysteresis: escalation is immediate, de-escalation is held for about two minutes.
- Top-level `account` block with subscription metadata from the same OAuth identity as the usage
  request.
- Cache-served reads with 429 backoff, so frequent checks are effectively free.
- Credential lookup from `CLAUDE_CODE_OAUTH_TOKEN`, the macOS Keychain (scoped to the current
  user, overridable with `CLAUDE_CODE_KEYCHAIN_ACCOUNT`), `.credentials.json`, then a running
  Claude Code process. Expired stored tokens are skipped.
