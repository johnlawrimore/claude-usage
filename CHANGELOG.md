# Changelog

All notable changes to claude-usage are documented here. Versioning follows
[semver](https://semver.org/): breaking changes to the JSON output contract bump the major
version, new fields or flags bump the minor, fixes bump the patch.

## [2.0.0] - 2026-10-27

- Replace the old posture vocabulary with Accelerate, Cruise, Coast, Brake and Park.
- Target useful quota consumption through reset, with explicit expansion guidance and a
  five-minute checkpoint reserve. Preserve all quota schedule and multiplier display fields.
- Emit `posture_instruction` and `posture_policy`; recompute legacy hysteresis states.
- Keep required review and verification standards invariant while changing work admission.

## [1.1.0] - 2026-07-14

### Added

- **Pace-aware posture.** Posture is now scored on two independent axes, worst rung wins: the
  percent a limit sits at, and the pace it is burning at. A new `pace` field per limit exposes
  `{ratio, exhaust_seconds}`, where `ratio` = current burn rate / the rate affordable before reset
  (equivalently `seconds_to_reset / seconds_to_exhaustion`). A `ratio` above 1 means the window
  empties before it resets, so a low-percent limit burning too fast now escalates instead of reading
  as "all clear". This fixes the case where a 20% session on a fast burn was reported as fine.
- **Finer five-rung posture ladder:** `normal` / `measured` / `frugal` / `conserve` / `wind_down`,
  replacing the three-rung ladder. The new `measured` rung reacts early (session ~50%+, or pace
  ratio ~1.0-1.25) instead of staying silent until 75%; `conserve` sits between `frugal` and
  `wind_down`. An absolute-runway backstop floors pace at `conserve` when a window will empty within
  ~30 minutes.

### Changed

- Percent bands per kind: session / scoped / unknown escalate at 50/75/90/97%; `weekly_all` at
  55/80/90/95%. Previously a limit was `frugal` at 75/80% and `wind_down` at a flat 90%; readings in
  the 90-97% range are now `conserve` rather than `wind_down`.
- The no-op floor is now `posture == normal` (both axes clear), so a pace-critical limit is no
  longer silenced by a low percent.

### Note on the output contract

The `posture` string is advisory guidance for a reader (an agent adapts its plan to it), not a
stable machine enum. Its set of values and the input-to-value mapping may change as the policy is
tuned, as they did here. The stable part of the JSON output contract is field presence and shape
(a `posture` string is always present; each limit carries `percent`, `burn`, `pace`, etc.), not the
specific posture words. Callers should not hard-code a switch over posture values; read the numeric
fields (`percent`, `pace.ratio`) if they need stable branching. This is why the posture change ships
as a minor (1.1.0) rather than a major: the field contract is unchanged, only the advisory values.

## [1.0.0] - 2026-09-25

First public release.

### Added

- Claude Code skill (`SKILL.md`) that checks live claude.ai subscription usage before large work
  and at phase boundaries, answers "how much have I got left?" questions, and announces every
  throttling decision it makes.
- Standalone CLI (`scripts/check-usage.py`, with `check-usage.sh` and `check-usage.cmd`
  launchers) reporting the 5-hour session, weekly, and per-model weekly limits. Flags: `--json`,
  `--fresh`, `--cache-only`, `--version`. Python 3.9+, standard library only; macOS, Linux, and
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
