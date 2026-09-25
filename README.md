# claude-usage

[![tests](https://github.com/johnlawrimore/claude-usage/actions/workflows/test.yml/badge.svg)](https://github.com/johnlawrimore/claude-usage/actions/workflows/test.yml)
[![version](https://img.shields.io/badge/version-1.0.0-informational)](CHANGELOG.md)
[![python](https://img.shields.io/badge/python-3.8%2B-blue)](#standalone-cli)
[![platforms](<https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey>)](#install)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

### Tired of your agent runs dying at "usage limit reached"?

**claude-usage** is a [Claude Code](https://claude.com/claude-code) skill that teaches Claude to
watch the fuel gauge and drive accordingly: full speed when there's headroom, leaner when it's
tight, a graceful landing before it hits the wall. It reads your live claude.ai limits (session,
weekly, and per-model) and, the part that actually matters, exercises judgment about spending
against them. And it narrates every throttling decision it makes, so nothing changes silently.

**TL;DR**

- Claude checks your session, weekly, and per-model limits before starting anything big and at
  phase boundaries inside long work. Checks are cache-served, so they cost basically nothing.
- Below the thresholds — on pace, never on raw fullness — it says nothing and runs at full scale.
  No nagging, no hedging.
- Above them it works through a five-rung ladder, weighted by what running dry would actually
  *cost* in wall-clock downtime: first it right-sizes fleets and drops speculative passes, then it
  shrinks agent fleets and moves grunt work to cheaper models. The deliverable itself is never
  thinned.
- The 5-hour session window is use-it-or-lose-it: near its wall, Claude finishes and checkpoints
  in-flight work rather than throttling budget that was about to expire anyway.
- Near a multi-day ceiling it stops taking on new work and lands what's in flight, saved to disk,
  with the reset time so you can decide whether to wait — and if a weekly budget is instead
  running under-spent, it flags that too, so you can spend it down before it's gone.
- Every one of those calls is announced: which limit, what percent, when it resets, what changed,
  and a one-line way to override it.
- It also just answers "how much have I got left?" with live numbers and reset times, instead of
  a shrug.

### What it looks like

When there's headroom, nothing changes and nothing is said. When there isn't:

> Heads up: the Fable weekly limit is at 82% (resets Wednesday 19:00 UTC), so I'll run the design
> review on Opus instead. Say the word if you want Fable anyway.

Under the hood, decisions like that begin with a single cached script call. Here is a reading that
triggers a pullback, this time driven by the 5-hour session rather than a model budget — burning
fast enough to hit `measured`, and close enough to its own wall for `imminent` to fire too:

```
Claude usage (fetched 0s ago, source: network)
  Session (5h)              90%  resets in 50m    [active]  +8.0% / 15m  pace 2.67x (empties in 18m)
  Weekly (all models)       72%  resets in 2d 4h
  Weekly (Fable)            75%  resets in 2d 4h
  posture: measured (driven by Session (5h) at 90%)
  ⚠ Session (5h) exhausts in ~22m, reset in ~50m — checkpoint now
```

<!-- TODO: replace the block above with a short terminal GIF (asciinema/vhs) of a real governed run -->

**Jump to:** [The problem](#the-problem-claude-code-flies-blind) ·
[How it works](#intelligent-throttling-how-it-works) ·
[Who it&#39;s for](#who-this-is-for) ·
[vs. other usage tools](#why-not-the-usage-tools-you-already-have) ·
[Trust](#lightweight-secure-and-auditable) ·
[Install](#install) ·
[CLI](#standalone-cli) ·
[Caveats](#caveats) ·
[If it breaks](#if-it-breaks)

## The problem: Claude Code flies blind

It has no idea how much of your usage it's burned, so it can't factor that into anything it does,
not how many agents it spawns, not which model it reaches for, not whether it should start a big
job at all. Which is fine right up until a run is large enough to hit the ceiling, at which point
it is emphatically not fine.

Left to its own devices, Claude Code will cheerfully spin up sixteen agents for a job it has no
budget to finish, then discover the limit the hard way: mid-migration, three verifiers deep into a
review, nothing shipped, the reset hours out. It didn't do anything wrong. It just couldn't see the
fuel gauge.

`claude-usage` bolts one on.

## Intelligent throttling: how it works

Printing a percentage is easy. The value is in what Claude *does* with it, and a raw percent is a
terrible basis for a decision: it tells you how full the tank is and nothing about whether you'll
make the next exit. A limit can be low on percent yet still binding, if it's burning fast enough
to run dry with hours left on the clock. So the skill scores usage pressure on two independent
axes and hands Claude the signals behind them:

1. **Time-to-reset.** 90% with four hours to go and 90% ten minutes before the window flips are
   completely different emergencies. The first means wind down; the second means keep working,
   relief is almost here. The skill distinguishes them, and even relaxes its own recommendation
   when a session reset is imminent.
2. **Burn rate and pace.** How fast a limit is *climbing* over roughly the last 15+ minutes, not a
   session-long average, e.g. `+6% / 45m`. 74% that crept up 2% today is a shrug; 74% that jumped
   30% in the last hour is a wall with your name on it. From the burn rate the script derives
   `pace.ratio`, current burn rate divided by the rate you could afford and still coast to reset.
   A ratio above 1 means the window empties before it resets, no matter how low the percent is,
   which is exactly the case a percent-only reading misses.
3. **A computed posture.** The script scores two independent *pace* readings — a short burn
   sample and a whole-window average, never raw percent — and takes the worse of the two as a
   provisional rung. Then it weighs that rung against what exhausting the limit would actually
   **cost**: a downtime cap looks at the wall-clock wait a burnout would force (`resets_in −
   projected_exhaust`) and can only relax the rung, never escalate it. A 5-hour session's
   downtime-derived cap can never exceed `frugal` — there's never more than 5 hours of downtime
   on the table — while a weekly limit, which can be days from reset, still reaches `wind_down`.
   A displayed 100% is terminal exhaustion and bypasses that cap, so it is always `wind_down`.
   The result — `normal`, `measured`, `frugal`, `conserve`,
   or `wind_down` — is deterministic and lightly smoothed run to run: it escalates the instant a
   worse reading appears, but a de-escalation has to hold for a couple of minutes before Claude
   trusts it, so two runs a few seconds apart can't flap between rungs on a noisy sample.

The whole decision loop at a glance:

```mermaid
flowchart LR
    subgraph signals["Live usage signals (cached, effectively free)"]
        direction TB
        pct["utilization %<br>session / weekly / per-model"]
        burn["burn rate<br>e.g. +6% / 45m"]
        reset["time to reset<br>in 40m vs in 2 days"]
    end

    burn --> paceR["burn pace ratio<br>burst rate vs. rate<br>affordable before reset"]
    reset --> paceR
    pct --> windowR["window pace headroom<br>whole-window average,<br>no sample needed"]
    reset --> windowR

    provisional{"provisional rung<br>(worse of the two<br>pace readings)"}
    paceR --> provisional
    windowR --> provisional

    cap["downtime cap<br>D = resets_in − margin-adjusted<br>earliest exhaust; can only LOWER"]
    provisional --> cap
    reset --> cap

    hyst{"hysteresis<br>escalate instantly,<br>de-escalate after ~2min held"}
    cap --> hyst

    posture{"posture<br>(deterministic, cost-weighted)"}
    hyst --> posture

    normal["normal<br>full scale, total silence"]
    measured["measured<br>right-size fleets,<br>drop speculative passes"]
    frugal["frugal<br>fewer agents, cheaper tiers<br>for grunt work"]
    conserve["conserve<br>stop expanding scope,<br>checkpoint often"]
    wind["wind_down<br>land in-flight work, save it,<br>report the reset time"]

    posture -->|"cost-free"| normal
    posture -->|"~15min+ of downtime"| measured
    posture -->|"~1h+ of downtime"| frugal
    posture -->|"~6h+ of downtime"| conserve
    posture -->|"provisional was wind_down,<br>and ~1 day+ of downtime<br>(no cap)"| wind

    announce["announced out loud:<br>which limit, what %, when it resets,<br>what changed + one-line override"]
    measured --> announce
    frugal --> announce
    conserve --> announce
    wind --> announce

    classDef success fill:#a7f3d0,stroke:#047857,color:#374151
    classDef decision fill:#fef3c7,stroke:#b45309,color:#374151
    classDef trigger fill:#fed7aa,stroke:#c2410c,color:#374151
    classDef error fill:#fecaca,stroke:#b91c1c,color:#374151
    normal:::success
    provisional:::decision
    cap:::decision
    hyst:::decision
    posture:::decision
    measured:::trigger
    frugal:::trigger
    conserve:::trigger
    wind:::error
```

`D` bands into the cap at 15 minutes / 1 hour / 6 hours / 1 day of downtime — under a day, the cap
applies and can only lower the provisional rung; a day or more of downtime is left uncapped (the
provisional rung stands as-is, whatever it is). A 5-hour session window can never accumulate more
than 5 hours of downtime, so its cap never crosses the 6-hour boundary: `frugal` is the cap's
ceiling. Terminal exhaustion is always `wind_down` and does not pass through the cap. A weekly
window, up to 7 days from reset, has no such ceiling.

Each posture maps to concrete behavior:

| Posture     | What Claude does                                                                             |
| ----------- | ---------------------------------------------------------------------------------------------- |
| `normal`    | Full scale, and blessedly quiet: no hedging, no "just so you know, you're at 60%."             |
| `measured`  | Right-size fleets instead of maximum fan-out, drop purely speculative extra passes. The task itself is unconstrained. |
| `frugal`    | Reduce parallelism, prefer cheaper tiers for sub-work, checkpoint sooner, warn before big work. |
| `conserve`  | Stop expanding scope, cheapest viable tier for all sub-work, checkpoint often, warn up front.   |
| `wind_down` | Stop growing the job. Land what's in flight, save it, and tell you when the window resets.      |

### What a governed run actually looks like

Say you kick off a 40-file migration with the session limit at 78% and climbing about 8% an hour,
three hours before reset.

**Without the governor**, Claude fans out a full agent fleet, hits the ceiling around file 23, and
you come back to a half-migrated repo, unverified changes, and a two-hour wait.

**With it**, the pre-launch check comes back `frugal`, so Claude runs a smaller fleet, hands the
mechanical transforms to a cheaper model, and checkpoints each batch to disk as it lands. At the
next phase boundary the picture has worsened, so it finishes the batch in flight, writes down
exactly where the migration stands, and tells you: which limit, what percent, and when the window
resets. You lose some parallelism. You don't lose the run.

### Per-model budgets: the limit you didn't know you had

Sometimes a model carries its own weekly limit on top of the shared one. Not always, and not
always the same model: which models are scoped varies by plan and shifts as the line-up changes
(Opus, Sonnet, and Fable have each been scoped at various points, and the examples on this page
use whichever it was when they were written). When a scoped limit exists, a top-tier model's
budget can run dry while your overall usage looks fine. That's the expensive surprise: you
delegate a design review to your best model and eat a limit warning even though the dashboard
says 40%.

The governor checks the scoped limit for the specific model it's about to use, before it uses it.
If that budget is tight, it swaps the sub-task to an adjacent tier and says so; that's the
heads-up quoted at the top of this page.

If the budget is fine, it doesn't needlessly downgrade: the top tier stays the right tool for
judgment-heavy work. And in unattended runs (a `/loop`, a scheduled job) it takes the cheaper-tier
default rather than blocking, and logs the swap so the decision is visible after the fact.

### Two rules keep the throttling honest

1. **It throttles the machinery, never the answer.** Tight budget means fewer agents and cheaper
   models for mechanical sub-work. It does *not* mean skimmed reads, skipped edge cases, or a
   thinner deliverable. If the real work genuinely can't be done well on what's left, Claude says
   so and lets you call it, instead of quietly shipping you less and hoping you don't notice.
2. **No silent throttling, and no silent over-throttling either.** When usage changes the plan,
   Claude tells you the limit, the number, the reset time, and what it did, with a one-line escape
   hatch ("say the word and I'll run it at full scale"). And when there's plenty of headroom, it
   shuts up about usage entirely, so checking constantly never turns into nagging. That silence
   rule is load-bearing: it's what makes it safe for Claude to check often.

## Who this is for

If you mostly use Claude for chat, this skill is not for you. This is for power-users who run
Claude Code hard. Multi-agent orchestration and long-haul work, where budget is a real constraint
and running dry halfway is a real failure:

- Workflows and wide agent fan-outs
- Long migrations, audits, and codemods
- `/loop` and other recurring or unattended runs
- Anything big enough that hitting a limit mid-run actually costs you something

## Why not the usage tools you already have?

|                                          | Data source                                           | Changes Claude's behavior?                                                             |
| ---------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `/usage` in Claude Code                | Live official quota                                   | No. It's a display; you look, interpret, and re-plan by hand.                          |
| Log-based monitors (ccusage and friends) | Spend estimated from local session logs               | No. Great retrospective dashboards, but estimates, not your actual quota.              |
| **claude-usage**                 | Live official quota, plus burn rate and time-to-reset | **Yes. Feeds directly into fleet size, model choice, and scope, automatically.** |

They're complementary: keep your cost dashboard for retrospectives. This fills the gap none of
them touch, the moment where the number should change what Claude *does next*.

Tools that watch your account need to earn their keep and your trust. This one is built to do
both:

- **Near-zero footprint.** Usage reads are cache-served (no network call inside the TTL), the
  output is a single JSON line, and below the thresholds the skill adds *nothing* to your
  conversation. No polling loops, no background processes, no chatter.
- **Zero dependencies.** One Python file, standard library only. Nothing to `pip install`, no
  dependency tree to audit, no supply chain to worry about.
- **Your token never leaves.** The OAuth token is read in-process and used for exactly one
  request to Anthropic's own API. It is never printed, logged, cached, or sent anywhere else,
  and the entire token-handling path (`get_access_token` + `fetch`) is about 40 lines, short
  enough to audit yourself in a couple of minutes.
- **No writes outside its lane.** It touches one cache file (`0600` on POSIX), and that's it. It
  doesn't modify your projects, your settings, or your credentials.
- **Tested and cross-platform.** A stdlib-only test suite runs in CI on Linux and Windows
  (Python 3.9 and 3.13); the skill is developed and used daily on macOS. Python 3.8+ should
  work; 3.9+ is what CI proves.
- **Fails safe.** If the endpoint changes, rate-limits, or disappears, you get last-known data
  clearly labeled stale, or an honest "usage unavailable". It never invents a number, and Claude
  is instructed to proceed conservatively rather than guess.

## Install

**Requirements:** a claude.ai subscription (Pro or Max) with Claude Code logged in. The skill
reads *subscription* limits; if you use Claude Code with API-key billing only, there are no
subscription limits to read and this skill has nothing to govern.

Clone this repo straight into your Claude Code skills directory:

```bash
# macOS / Linux
git clone https://github.com/johnlawrimore/claude-usage.git ~/.claude/skills/claude-usage
```

```bat
:: Windows
git clone https://github.com/johnlawrimore/claude-usage.git %USERPROFILE%\.claude\skills\claude-usage
```

Claude Code discovers the skill from `SKILL.md`. No setup beyond the clone; it reads your existing
Claude Code credentials. Runs on macOS, Windows, and Linux; requires only Python 3.8+. To update,
`git -C ~/.claude/skills/claude-usage pull`.

To confirm it's wired up, run the CLI once (next section): if it prints your limits, Claude can
see them too. To uninstall, delete the folder (`rm -rf ~/.claude/skills/claude-usage`) and, if
you like, the cache file at `~/.claude/.usage-cache.json`. Nothing else is touched.

## Developing

If you work on the skill from a separate checkout rather than editing the cloned copy in place,
run:

```bash
bin/deploy
```

which rsyncs the checkout over `~/.claude/skills/claude-usage` (excluding `.git/` and `bin/`,
and deleting anything there that no longer exists in the checkout) and prints each changed file.
Set `TARGET_DIR` to deploy somewhere else, e.g. for testing:

```bash
TARGET_DIR=$(mktemp -d) bin/deploy
```

## How to use it

There's nothing to call. You just work normally, and Claude reaches for the skill on its own in
two ways:

- **On its own, during big work.** This is the point of the thing. Before it spins up a wide
  agent fan-out, a long migration, or a `/loop`, and at phase boundaries inside them, Claude
  checks usage and adjusts, unprompted. You don't have to remember to ask.
- **When you ask about usage.** "How much have I got left?", "am I close to my limit?", "what's
  my quota look like?", Claude recognizes the intent and checks. No skill name, no special syntax.

If you ever want to force a check by hand, `/claude-usage` (or just "check my usage first")
always works.

One requirement worth repeating: this runs inside **Claude Code**, not claude.ai chat or the API,
since it reads your local Claude Code credentials.

## Standalone CLI

The brains live in `scripts/check-usage.py` (Python 3.8+, standard library only). Run it through the
launcher for your platform, or call the `.py` directly:

```bash
# macOS / Linux
~/.claude/skills/claude-usage/scripts/check-usage.sh

# Windows
%USERPROFILE%\.claude\skills\claude-usage\scripts\check-usage.cmd

# any platform, directly
python3 scripts/check-usage.py        # or: python scripts\check-usage.py  (Windows)
```

It prints a human-readable summary followed by one machine-readable JSON line:

```
Claude usage (fetched 0s ago, source: network)
  Session (5h)              90%  resets in 50m (2026-...)  [active]  +8.0% / 15m  pace 2.67x (empties in 18m)
  Weekly (all models)       72%  resets in 2d 4h (2026-...)
  Weekly (Fable)            75%  resets in 2d 4h (2026-...)
  posture: measured (driven by Session (5h) at 90%)
  ⚠ Session (5h) exhausts in ~22m, reset in ~50m — checkpoint now
---
{"available": true, "posture": "measured", "limits": [ ... ]}
```

Flags: `--json` (JSON only), `--fresh` (bypass the cache TTL; still respects the 429 backoff),
`--cache-only` (read and recompute existing cached data with a structural guarantee that the
cache, credentials, and network are never touched), `-h`/`--help`, `--version`.

| Variable                     | Default                        | Meaning                                                                   |
| ---------------------------- | ------------------------------ | ------------------------------------------------------------------------- |
| `CLAUDE_CONFIG_DIR`        | `~/.claude`                  | where credentials and the cache live                                      |
| `CLAUDE_USAGE_CACHE`       | `<config>/.usage-cache.json` | cache file path (overrides the above for the cache)                       |
| `CLAUDE_USAGE_TTL`         | `300`                        | cache TTL in seconds (negatives fall back to default)                     |
| `CLAUDE_USAGE_429_BACKOFF` | `900`                        | seconds to serve stale cache after a 429 (negatives fall back to default) |
| `CLAUDE_CODE_OAUTH_TOKEN`  | (unset)                        | explicit token override, checked before Keychain/file                     |

Requires Python 3.8+; the launchers find a working interpreter, though the order differs by
platform: the Unix `.sh` launcher tries `python3` then `python`, while the Windows `.cmd` launcher
tries `py -3`, then `python`, then `python3` (stepping around the Windows Store stub along the
way). Credentials are read in order from
`CLAUDE_CODE_OAUTH_TOKEN`, the macOS Keychain, then `<config>/.credentials.json` (where Claude Code
keeps them on Windows and Linux).

## Output fields

Each entry in the `limits` array carries:

- `kind`: `session`, `weekly_all`, `weekly_scoped`, or any future kind the endpoint dreams up
  (unknown kinds are passed through, not dropped).
- `percent`: 0-100 utilization, or `null` for a meter that isn't a percent. A `null` percent does
  **not** mean "fine", and percent never by itself decides `posture` — see below.
- `scope_model`: for a scoped limit, the model's display name (e.g. `"Fable"`).
- `is_active`: appears to indicate the limit currently binding (private-API field, semantics
  unverified).
- `resets_at` / `resets_in_seconds`: when the window rolls over (ISO 8601 and signed seconds).
- `burn`: `{delta_percent, over_seconds}` vs an earlier snapshot, or `null` if there's no baseline
  yet.
- `pace`: `{ratio, exhaust_seconds}` when `burn` is present and pace can be judged (including a
  sustainable `ratio` below 1), else `null`. `ratio` is current burn rate divided by the rate affordable before reset
  (`seconds_to_reset / seconds_to_exhaustion`); above 1 means the window empties before it resets,
  regardless of percent. `null` means unknown, not sustainable — read `window_pace` instead.
- `window_pace`: `{ratio, headroom, exhaust_seconds}`, the sample-free whole-window counterpart to
  `pace`. `headroom` (remaining budget over remaining time, in budget-pace units) is what `posture`
  actually bands on; it is what governs a weekly limit days from reset, where `pace` is null by
  construction.
- `downtime`: `{seconds, cap}` — the wall-clock downtime cost (`D`) and the posture cap it derives
  (see below), or `null` when neither pace reading has an exhaustion projection to cap against.
- `runway_seconds` / `imminent`: the same earliest exhaustion projection, plus a flag for "about to
  run dry, checkpoint now" (under 30 minutes and before the reset). Never a reason to throttle
  admission on its own — see "The downtime cap" below.
- `surplus`: `{projected_final_percent, unspent_percent}` for a pool limit (`weekly_all` /
  `weekly_scoped`, never `session`) on track to expire with budget unspent, else `null`.
- `posture`, `pace_judgeable`, and `color`: the canonical per-limit pace classification and its
  display color (`green`, `blue`, `yellow`, `orange`, `red`, or `gray`). A limit displayed as
  100% is exhausted and always red. Gray denotes an unavailable or unjudgeable meter, whether
  the source data is fresh or stale.
- `extra`: any fields the endpoint attached that we don't normalize (e.g. a `remaining_credits`
  balance on a usage-credit meter), kept verbatim so a new kind of budget surfaces instead of
  vanishing.

Top-level `account` is the authoritative non-secret identity and subscription metadata resolved
from the OAuth profile using the same token as the usage reading: `email`, `account_uuid`,
`organization_uuid`, `subscription_type`, and `rate_limit_tier`. Consumers must bind both UUIDs
to their own account registry before display. It is cached atomically with the usage response;
credential-embedded package labels are not part of the contract.

Top-level `posture` is computed deterministically as a five-rung ladder, weakest to strongest:
`normal`, `measured`, `frugal`, `conserve`, `wind_down`. There is no percent axis — raw fullness
never decides posture, only pace does. Terminal exhaustion is the sole narrow exception: a value
that renders as 100% is always `wind_down`, because exhaustion is a fact rather than a fullness
band. Each limit takes the worse of two pace readings as a
**provisional** rung: burn pace (`pace.ratio`, banded upward at `1.0`/`1.25`/`2.0`/`4.0`, judged
only within ~6h of reset since a ~15-minute burst does not extrapolate across a multi-day window)
and window pace (`window_pace.headroom`, banded downward at `0.75`/`0.50`/`0.25`/`0.10`, needing no
burn sample so it alone governs a weekly limit days out).

### The downtime cap

Unless the limit is already exhausted, that provisional rung is then **capped by what exhausting the limit would actually cost**:
`D = resets_in_seconds − exhaust_effective`, the wall-clock downtime a burnout would force. The
final rung is `min(provisional, cap)` — the cap only ever *lowers* a rung, on a table banded at 15
minutes / 1 hour / 6 hours / 1 day of downtime (`normal`/`measured`/`frugal`/`conserve`; a day or
more is left uncapped). `exhaust_effective` is margin-adjusted per kind — `session` gets an
optimistic `0.85` (use-it-or-lose-it budget, so a rosier read favors letting it run), every other
kind (unknown kinds included) gets a pessimistic `1.15` (a weekly overshoot costs days, so lean
toward catching it sooner). This is what makes a 5-hour session window's downtime cap top out at
`frugal` — its `D` can never reach the 6-hour boundary — while a weekly window, up to 7 days from
reset, has no such ceiling. An exhausted limit bypasses this calculation and remains `wind_down`.

The provider's fullness-derived `severity` field is deliberately discarded during normalization;
it is neither a decision input nor part of the public JSON contract.
`posture` is also lightly smoothed run to run: it escalates the instant a worse rung computes, but
a de-escalation is held at the prior rung until the improvement has computed for ~2 minutes
straight, surfaced as `posture_computed` (this run's raw, pre-smoothing reading) differing from
`posture`. Worst limit wins across the whole `limits` array, and `posture_driver` names the
culprit.

## Caveats

**It rides a private endpoint.** Usage data comes from `GET /api/oauth/usage`, a private,
undocumented Anthropic endpoint (the same backend behind claude.ai's usage page). It is
aggressively rate-limited, which is exactly why the script caches hard and backs off on a 429. It
could change or vanish without warning; when it does, the script falls back to the last known
reading (clearly labeled stale) and never makes a number up.

**Your token stays put.** The OAuth access token is read in-process from `CLAUDE_CODE_OAUTH_TOKEN`,
the macOS Keychain, or `<config>/.credentials.json`, and goes nowhere except that one Anthropic
request, never printed, logged, or cached. The cache file is `0600` on POSIX (a no-op on
Windows/NTFS, where it inherits your profile's ACLs anyway); it holds the raw usage response, never
the token.

**Built to outlive the current limit line-up.** Model rosters and billing change constantly, so the
script hard-codes no model name and assumes no fixed set of limit kinds. It reads whatever
`scope_model` values show up, waves through unfamiliar `kind`s, and surfaces non-percent meters
(like a usage-credit balance) under `extra` instead of tossing them. So the day a model slides from
a scoped weekly limit onto metered usage credits, the new meter still shows up rather than quietly
reading as "all clear".

**Not provider-generic (yet).** The idea, read a quota, adapt posture, throttle out loud, is
general. This implementation is Anthropic-specific end to end. A future version could factor out a
usage-provider interface (the network fetch and the `parse_limits` normalizer are the obvious seam)
and reuse the posture, burn-rate, and communication layer unchanged.

## If it breaks

The private endpoint will drift someday. Here's what that looks like and what to do:

- **Symptoms:** every reading labeled `[STALE: showing last known data]`, or
  `Usage unavailable: endpoint returned 200 but the response shape was not recognized`, or Claude
  reporting that usage is unknown.
- **Diagnose:** run the CLI with `--json`. The JSON line's `reason` field says exactly what
  failed, and it contains usage numbers only, no secrets, so it's safe to paste anywhere.
- **Check for a fix first:** shape drift is exactly what updates fix.
  `git -C ~/.claude/skills/claude-usage pull`
- **Report it:** [open an issue](https://github.com/johnlawrimore/claude-usage/issues) with
  that JSON line and your version (`check-usage.py --version`). Fast reports are what keep an
  endpoint-riding tool working for everyone.

### Versioning

Semantic versioning, tracked in [CHANGELOG.md](CHANGELOG.md). Current release: **1.0.0**. Last
verified against the live endpoint: **2026-09-06** (the endpoint's response shape; the 1.0.0
posture algorithm itself is verified against the test suite only, not a fresh live call).

## Layout

```
claude-usage/                 # cloned as-is to ~/.claude/skills/claude-usage
├── SKILL.md                   # skill instructions Claude Code loads
├── README.md                  # this file
├── scripts/
│   ├── check-usage.py         # the logic (Python 3.8+, stdlib only)
│   ├── check-usage.sh         # macOS / Linux launcher
│   └── check-usage.cmd        # Windows launcher
├── tests/
│   ├── __init__.py                     # empty, for test discovery
│   ├── test_check_usage.py             # stdlib unittest suite
│   └── test_posture_is_pace_only.py    # standalone posture table test
├── bin/
│   └── deploy                 # rsyncs a dev checkout to ~/.claude/skills/claude-usage
├── .github/
│   └── workflows/
│       └── test.yml           # CI: runs the test suite on Linux and Windows
├── .gitattributes             # pins line endings (LF scripts, CRLF .cmd)
├── .gitignore
├── CHANGELOG.md
└── LICENSE
```

## Tests

The test suite is stdlib `unittest`, zero dependencies, in `tests/test_check_usage.py`. Run
it with:

```bash
python3 -m unittest discover tests
```

`tests/test_posture_is_pace_only.py` is a standalone table test (posture reacts to pace,
never percent) run directly rather than through `unittest`:

```bash
python3 tests/test_posture_is_pace_only.py
```

CI (`.github/workflows/test.yml`) runs the same suite on GitHub Actions across Linux and Windows,
on Python 3.9 and 3.13.

## License

MIT © 2026 [John Lawrimore](https://github.com/johnlawrimore)
