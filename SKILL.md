---
name: claude-usage
description: >-
  Check the user's live claude.ai subscription usage (5-hour session limit, weekly limit, and
  per-model weekly limits, which some models sometimes have, such as Fable) and adapt execution plans to it. Use this whenever the
  user asks about usage, quota, limits, "how much do I have left", "am I close to my limit", or
  when a limit warning appears. Just as importantly, use it proactively on your own initiative:
  BEFORE launching anything large (a Workflow, a wide Agent fan-out, a /loop, a long migration or
  audit), at phase boundaries inside long-running work, and BEFORE delegating sub-work to a
  top-tier model (Fable/Opus), since model-scoped weekly budgets may be tighter than overall
  usage. Reads are cache-served and effectively free (a check in the first few minutes after a
  limit resets may make one live call to refresh the rolled-over window), so checking at these
  decision points is safe.
license: MIT
metadata:
  author: John Lawrimore
  source: https://github.com/johnlawrimore/claude-usage
  version: 2.0.0
---

# claude-usage

Check live subscription usage and make execution decisions with it. This skill has two jobs:
answering "how much have I used" questions, and governing your own resource decisions during
long or expensive work.

## How to check

Run the launcher for the platform you are on (both take the same flags and print the same output):

```bash
# macOS / Linux
~/.claude/skills/claude-usage/scripts/check-usage.sh
```

```bat
:: Windows
%USERPROFILE%\.claude\skills\claude-usage\scripts\check-usage.cmd
```

The logic lives in `scripts/check-usage.py` (Python 3.9+, standard library only); the launchers just
find a Python interpreter and run it, so on any platform you can also invoke
`python3 .../check-usage.py` (or `python` / `py -3` on Windows) directly.

It prints a human-readable summary followed by one machine-readable JSON line. Add `--json` for
only the JSON line. The script caches the raw endpoint response, never the token, for 5 minutes
(`CLAUDE_USAGE_TTL` to change) at `<config>/.usage-cache.json`, where `<config>` is
`CLAUDE_CONFIG_DIR` or `~/.claude` (`CLAUDE_USAGE_CACHE` overrides the full path); within the TTL
it makes no network call at all, so calling it repeatedly is free and instant.

Do not curl the endpoint yourself, and do not pass `--fresh` unless the user explicitly asks for
a forced refresh: the underlying endpoint (`/api/oauth/usage`, private, undocumented) rate-limits
after only a few requests, and the cache is what makes frequent checks safe. The script calls
exactly two Anthropic endpoints: `https://api.anthropic.com/api/oauth/usage` for the limits data,
and `https://api.anthropic.com/api/oauth/profile` for account/organization metadata, fetched only
when needed. The OAuth token is sent only to Anthropic, to these two endpoints, and is never
printed, logged, or echoed. The script reads the token in-process, in this order: the
`CLAUDE_CODE_OAUTH_TOKEN` env var; the macOS Keychain (account = `CLAUDE_CODE_KEYCHAIN_ACCOUNT` if
set, else the current OS user); `<config>/.credentials.json`; then a running Claude Code process's
environment (read via `ps`, same-user only, macOS/Linux).

## Objective and posture

Ride the available quota through reset without exceeding it or wasting useful capacity.
The script reports an account-wide work-admission recommendation. Read `posture`,
`posture_instruction`, and `posture_reason` together. The posture is not a performance score.

| Posture | Color | Agent behavior |
| --- | --- | --- |
| `accelerate` | blue | Increase **useful** work toward the affordable rate. Revisit valuable deferred work, add useful parallelism, or use stronger models where they add value. Never invent busywork to burn quota. |
| `cruise` | green | Maintain the approach, aiming to use the available quota through reset. |
| `coast` | yellow | Ease off new commitments and speculative extras. Preserve the rigor of running work. |
| `brake` | orange | Actively reduce admissions and parallelism. Cheaper models are allowed only where the task and required gates permit. |
| `park` | red | Finish and save; admit no new work until capacity returns. Persist and hand off held work before waiting. |

Coast and Brake change admission and staffing, not the standard of correctness. Never skip,
weaken, or downgrade required review tiers, tests, safety checks, or gates to fit a budget.
If an obligatory step cannot fit, checkpoint and defer it explicitly. A Swarm runner in Park
records progress, clears its claim, and releases the held card to its Pending lane; it must
not strand a claim or leave an unexplained blocked card.

A fleet shares its account pools. The coordinator owns net expansion: Accelerate is not
permission for every worker to independently multiply its fleet. The script measures
account aggregate quota changes, not agent count, per-session token draw, context size,
future tasks, or a guaranteed completion budget. Match useful admission to the evidence.

## How the policy works

Both recent and whole-window rates are converted to **consumption rate divided by the
rate affordable until reset**. A 1.0 ratio aims to exhaust capacity exactly at reset;
2.0 would exhaust it halfway there at an unchanged rate.

- Prefer a recent burn sample only when it moved at least 2 percentage points and reset is
  within 6 hours. Otherwise use the whole-window average in the same affordable-rate units.
  The average is a fallback, not a second veto after a reliable recent recovery.
- Below 0.90: Accelerate when there is evidence of spare capacity. A whole-window fallback
  also requires at least 5% of the window elapsed and 2 percentage points projected unused.
  A fresh empty window alone does not justify expansion.
- From 0.90 through 1.00: Cruise. Above 1.00 through 1.25: Coast. Above 1.25: Brake.
- A displayed 100% is Park. Park also applies when the selected margin-adjusted exhaustion
  forecast is within 5 minutes **and before reset**, reserving capacity to save and hand off.
- A short projected interruption can soften Brake to Coast (under 1 hour); it never turns
  overspending into permission to Accelerate or Cruise. Park bypasses this cap.
- The most restrictive real limit wins, including model-scoped and unfamiliar limits.
  Every applicable limit must allow Accelerate. An unjudgeable limit grants no expansion.
- Restrictions take effect immediately. Easing must persist for 120 seconds, with existing
  reset and old-state safeguards. Legacy posture cache states are recomputed on migration.

Session exhaustion forecasts use a 0.85 burn margin; pooled weekly limits use 1.15. These
are estimates, not promises of exact remaining work. Bursts and future load can change them.
`imminent` is an earlier 30-minute checkpoint warning from the conservative earliest
recent/window forecast; it can be true while selected recent pace permits Cruise or
Accelerate. Checkpoint warnings preserve safety even when a recent recovery permits work.

## Reading the output

The summary is followed by a JSON object (or JSON alone with `--json`). Top-level fields:

- `available`: whether a real usage response could be read. False is unknown, never zero use.
- `source`, `stale`, `age_seconds`: freshness and provenance. A stale posture is last known
  guidance, not evidence for expansion. State that quota is unknown and recheck.
- `account`: authoritative non-secret OAuth profile metadata: `email`, `account_uuid`,
  `organization_uuid`, `subscription_type`, `rate_limit_tier`. Bind both UUIDs before using
  this metadata for a registered account; do not infer a plan from credential package labels.
- `posture`: the smoothed admission recommendation; `posture_computed` is this read's raw
  recommendation. `posture_driver` identifies the binding limit. `posture_reason` explains
  the calculation and any smoothing. `posture_instruction` gives the corresponding action.
  `posture_policy` is `ride_limit_v1`.
- `next_check_seconds`: the real scheduled retry when rate limiting prevents a fresh read.
- `limits`: all known provider meters, not just the two clocks visible in a widget.

Each limit includes:

- `kind`: `session`, `weekly_all`, `weekly_scoped`, or a future provider kind; `scope_model`
  names a scoped budget. `percent` is quota consumed, or null for a non-percent meter.
- `resets_at`, `resets_in_seconds`: the provider reset timestamp and remaining wall time.
  Use the complete timestamp, never its hour-of-day. Missing or expired reset data is unknown.
- `burn`: observed percentage-point change and sample duration, or null if no usable baseline.
- `pace`: recent consumption/affordable-rate ratio and exhaustion estimate, when qualified.
- `window_pace`: sample-free consumed/elapsed diagnostic ratio, remaining headroom, and
  whole-window exhaustion. Its diagnostic ratio uses a different denominator from `pace`.
- `display_pace`: recent consumption/affordable rate or the converted window-average fallback,
  with its basis and independent color. This is the external `x` multiplier in widgets.
- `posture`, `posture_color`: this limit's admission direction and its color.
- `color`, `pace_judgeable`: quota-ring schedule color and whether it can be judged. The ring
  compares actual percent consumed with the ideal percent elapsed. Behind is blue, near
  target green, ahead yellow/orange; displayed 100% is red. Gray is unjudgeable. Display
  colors never feed back into posture: ring, multiplier, and posture measure different things.
- `runway_seconds`, `imminent`, `downtime_seconds`, `downtime_cap`, `unspent_percent`:
  conservative checkpoint runway, early warning, projected wait, cap, and projected surplus.
  Surplus is useful evidence for spend-down, not a separate command to expand every worker.
- `is_active`, `verdict`, `extra`: provider activity, a readable assessment, and unfamiliar
  fields preserved verbatim. Never silently discard an unknown or non-percent limit.

## Acting on uncertainty and scoped limits

Check before wide fan-out, at phase boundaries, and before delegating to a model with its
own budget. In-flight work still needs enough capacity for a clean checkpoint. Do not wait
for Park to start saving progress when `imminent` is true.

If a scoped model is binding, route eligible sub-work to an unconstrained model while keeping
required tiers and gates intact. A high weekly percent alone is not a stop instruction, and
a low percent alone is not permission to fan out. Quote the actual reason when changing
admission, and say what changed. Cruise needs no repetitive warning.

No credentials, network failure, stale data, unknown meters, or cache rate limiting are not
healthy zero readings. Explain the uncertainty, avoid new-scope expansion, recheck at the
next boundary, and honor the server's backoff. Never repeatedly force live calls. Do not
print, echo, log, or hand tokens to another tool or agent.
