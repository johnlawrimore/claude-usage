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
  version: 1.0.0
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

The logic lives in `scripts/check-usage.py` (Python 3.8+, standard library only); the launchers just
find a Python interpreter and run it, so on any platform you can also invoke
`python3 .../check-usage.py` (or `python` / `py -3` on Windows) directly.

It prints a human-readable summary followed by one machine-readable JSON line. Add `--json` for
only the JSON line. The script caches the raw endpoint response, never the token, for 5 minutes
(`CLAUDE_USAGE_TTL` to change) at `<config>/.usage-cache.json`, where `<config>` is
`CLAUDE_CONFIG_DIR` or `~/.claude` (`CLAUDE_USAGE_CACHE` overrides the full path); within the TTL
it makes no network call at all, so calling it repeatedly is free and instant.

Do not curl the endpoint yourself, and do not pass `--fresh` unless the user explicitly asks for
a forced refresh: the underlying endpoint (`/api/oauth/usage`, private, undocumented) rate-limits
after only a few requests, and the cache is what makes frequent checks safe. Never print, log, or
echo the OAuth token; the script reads it in-process, in order, from the `CLAUDE_CODE_OAUTH_TOKEN`
env var, the macOS Keychain, then `<config>/.credentials.json`, and never outputs it.

## Reading the output

**Invariant vs objective.** One rule here is non-negotiable: remaining runway must always cover a
clean checkpoint of everything currently in flight (see `imminent` below). Everything else — the
rung ladder, fleet size, model tier — serves a softer objective: maximize the value of admitted
work net of the downtime you expect to pay for it. Rungs are strong guidance toward that
objective, not the invariant itself; the only hard stop is "can I checkpoint cleanly before the
wall", and that is what `imminent` answers, not `posture`.

Posture reacts to pace, never to raw fullness. `percent` is still in the output because it's the
number a human reads, but it does not by itself decide anything — only the rate at which a limit is
being spent, measured against the clock, does. There are two pace readings, and the worse of the two
wins: `pace` is a short (~15-minute) burn sample projected forward, good at catching a fresh burst;
`window_pace` compares total consumed to total elapsed for the whole window, good at catching
sustained overconsumption days before a weekly reset, exactly where a burn sample goes null. A limit
can be low on percent yet still binding, if either reading says it is on track to run dry before the
window resets. The same 97% that is fine with two hours left in a week is alarming with two hours
left in a session — only the clock tells them apart, which is why fullness alone was retired as a
posture input.

The JSON line has `available`, `stale`, `age_seconds`, `source` (where the numbers came from:
`network`, `cache`, or a stale-cache variant), `account`, `posture`, `posture_driver`,
`posture_reason`, and a `limits` array. `account` is the authoritative non-secret identity and
subscription metadata resolved from the OAuth profile with the same token as the usage reading:
`email`, `account_uuid`, `organization_uuid`, `subscription_type`, and `rate_limit_tier`. Consumers
must bind both UUIDs to their own registry before display; never use credential-embedded package
labels as a fallback. `posture_reason` is a one-line string explaining why `posture` is what it is, present
even at `normal` — for example `"Weekly (all models): 99% spent with 99% of the window elapsed
(1.00x budget)"`. It is returned at every rung, not just
elevated ones, because "why is it normal" needs an answer as much as "why is it wind_down" does; say
it when you report `posture` rather than only naming the bare word. Each limit:

- `kind`: `session` (rolling 5-hour window), `weekly_all` (whole weekly allotment), or
  `weekly_scoped` (a sub-limit for one model or surface). This set is **not fixed**: model
  line-ups and billing change often, so treat any unfamiliar `kind` as a real limit and surface
  it rather than ignoring it (see "New and non-percent limits" below).
- `percent`: 0 to 100 utilization, or `null` for a meter that is not a percent (again, see below).
  A `null` percent does **not** mean "fine".
- `scope_model`: the model a scoped limit is tied to, as a display name (for example `"Fable"`).
  Read whatever appears here; never assume a particular model is or isn't present.
- `extra`: any fields the endpoint attached that aren't normalized above, preserved verbatim (for
  example a `remaining_credits` balance on a usage-credit meter). `null` when there are none.
- `is_active`: appears to indicate the limit currently binding (private-API field, semantics
  unverified).
- `resets_at`: ISO 8601 reset time. Always surface this when a limit influences a decision, so
  the user can choose to wait instead.
- `resets_in_seconds`: signed seconds to that reset, computed at read time. Prefer it over doing
  date math on `resets_at` yourself. A value at or below zero means the reset moment is in the past
  (the limit is flagged `reset-elapsed`). The script refetches on its own when a reset happened
  *after* the cached data was fetched, so you should not reach for `--fresh` on a `reset-elapsed`
  limit; an already-past reset time is intentionally left as-is to avoid a needless refetch loop.
- `burn`: `{delta_percent, over_seconds}` when an earlier snapshot is available, else `null`. This
  is the climb over roughly the last 15+ minutes (for example `+6%` over the last `45m`), not a
  session-long average, which answers "will this plan fit in what's left" far better than a bare
  percent. `null` just means no baseline yet (first check, or the last one was too recent); it is
  not zero burn.
- `pace`: `{ratio, exhaust_seconds}` when `burn` is present and pace can be judged (including a
  sustainable `ratio` below 1), else `null`. `ratio` is your current burn rate divided by the rate you could afford and still
  coast to reset, which is exactly `seconds_to_reset / seconds_to_exhaustion`. `1.0` means you land
  on empty right at reset; `1.5` means you hit the wall at two-thirds of the way there; `2.0` means
  halfway. `exhaust_seconds` is the projected wall-clock time to reach 100% at the current pace.
  This is the signal a bare percent misses: **a `ratio` above 1 means the window empties before it
  resets, no matter how low the percent is.** `null` means pace cannot be judged from a burn sample
  (no burn baseline yet, a non-percent meter, a tiny sub-2% wiggle too noisy to extrapolate, the
  reset already past, or the reset more than ~6 hours out, since a 15-minute burst does not project
  meaningfully across a multi-day window). A `null` `pace` does **not** mean "unknown, fall back to
  percent" — falling back to fullness is exactly the bug this field's sibling exists to close. Read
  `window_pace` instead: it covers every window `pace` can't, including the multi-day tail where a
  burn sample goes null by construction.
- `window_pace`: `{ratio, headroom, exhaust_seconds}`, the whole-window counterpart to `pace`,
  needing no sample, no baseline and no history. Two readings of the same window, and it matters
  which one drives posture:
  - `ratio = (percent / 100) / elapsed_fraction` — how you have spent **so far**. Display data.
  - `headroom = (1 - consumed_fraction) / (1 - elapsed_fraction)` — remaining budget over remaining
    time, in budget-pace units. **This is what `posture` bands on.** `1.0` means you can spend at
    exactly budget pace the rest of the way; `0.5` means half speed from here; `0` means stop.
    Escalation runs *downward* as headroom falls.

  Headroom rather than ratio because a flat threshold on `ratio` cannot serve a window that drains.
  The ratio's own ceiling is `1 / elapsed_fraction`, so a `wind_down` rung at `4.0` is
  arithmetically unreachable past the first quarter of any window, while early on the same ratio
  explodes on a tiny denominator and needs a cap bolted on to stay sane. Headroom is well behaved at
  both ends: early it is approximately `1 - consumed`, and late it collapses precisely when the
  runway does. Banding headroom is algebraically an elapsed-dependent threshold on the ratio — the
  bands tighten as the window drains — but it falls out of one formula instead of being hand-tuned
  per rung.

  It also removes two special cases rather than adding them: a window seconds from reset gives a
  large headroom and reads `normal` without the session-only near-reset relief that used to do that
  job, and a fully consumed window gives headroom `0` and reads `wind_down` without the
  `remaining <= 0` null that used to hand the question to the percent axis.

  Window lengths: session 5h, weekly 7d. `null` only when the window itself can't be located: an
  unfamiliar `kind` (no window length to express the rate in), a missing `percent`, or reset data
  that contradicts the window length (clock skew, or the provider redefining the window).

  `pace` and `window_pace` see different failure modes — `pace` catches a fresh burst near reset
  that a window average would wash out; `window_pace` catches sustained overconsumption days out,
  where `pace` is null by construction — which is why `posture` takes the worse of the two rather
  than picking one, and why they do **not** share a band table.
- `downtime`: `{seconds, cap}` (v3) — the wall-clock downtime cost and the posture cap it derives
  (see "The downtime cap" below), or `null` when neither pace axis has an exhaustion projection to
  cap against. `cap` itself can be `null` inside a non-null `downtime` when the cost is a day or
  more (beyond every band) — transparency for whether, and why, the provisional rung got capped.
- `runway_seconds` (v3): the same margin-adjusted earliest exhaustion projection as `downtime`, or
  `null`. This is the number to read when deciding whether a checkpoint is due right now.
- `imminent` (v3): `true` iff `runway_seconds` is under 30 minutes (`IMMINENT_RUNWAY_S`) **and**
  lands before the reset itself; `false` (never `null`) otherwise, including when `runway_seconds`
  is `null`. This is the invariant signal, not an escalation one: finish and persist the current
  unit of work, then either wait out the (short) reset or admit nothing new until it rolls over.
  It replaces the v2 30-minute conserve backstop, which used to floor `posture` at `conserve` on
  the same condition — `imminent` carries the same "about to run dry" fact without pretending it's
  a reason to throttle admission (see "Admission control" below).
- `surplus` (v3): `{projected_final_percent, unspent_percent}` for a pool limit (`weekly_all` or
  `weekly_scoped`, never `session`) on track to expire with budget unspent, else `null`. Emitted
  once at least half the window has elapsed (`SURPLUS_MIN_ELAPSED_FRACTION = 0.5`) and the
  projected final percent — spend so far divided by elapsed fraction, capped at 100 — is under
  `75.0` (`SURPLUS_PROJECTED_FINAL_MAX`). See "Surplus and spend-down" below. Never moves
  `posture`, any rung, or `posture_driver`.

Top-level `posture` is the script's own recommendation, derived deterministically so the reading
is consistent run to run. It is a five-rung ladder, weakest to strongest:

- `normal`: no throttling. Proceed at full scale.
- `measured`: do not waste. Keep the fleet at the task's natural size instead of reaching for
  maximum fan-out, and drop purely speculative extra passes, but do not yet reduce parallelism below
  what the task needs or downgrade model tiers. The earliest, gentlest nudge. (`frugal` is where you
  start actively shrinking fleets and moving sub-work to cheaper tiers.)
- `frugal`: bias toward smaller fleets and cheaper tiers for mechanical sub-work; skip speculative
  extra-thoroughness passes unless the user opts in.
- `conserve`: stop expanding scope. Cheapest viable tiers for all sub-work, checkpoint to disk
  often, and warn before starting anything nontrivial.
- `wind_down`: stop expanding scope; finish and persist current work, no new work.

It is computed per limit, from pace alone, and the worst rung across every limit wins. Terminal
exhaustion is the narrow exception: a percentage that displays as 100% is a terminal fact, not a
fullness band, and is always `wind_down`. Each non-exhausted limit
takes the worse of two pace readings, scored against **two different band tables**, because the two
measure different things — a burst rate and a running average — and a threshold that is right for
one is wrong for the other:

- **Burn pace** (`pace.ratio`), banded **upward** at `1.0`/`1.25`/`2.0`/`4.0` →
  `measured`/`frugal`/`conserve`/`wind_down`. Escalation begins at ratio `1.0` because for a burst
  reading that is exactly the point where the current rate would empty the window before it resets.
  Burn below 2 percentage points is ignored as noise, and it is judged only when the reset is
  within ~6 hours (see the `pace` field above); beyond that horizon it reads `null` and contributes
  nothing, leaving window pace to govern alone. (The v2 30-minute-runway backstop that used to live
  here is gone — see "The downtime cap" and `imminent` below for where its job went.)
- **Window pace** (`window_pace.headroom`), banded **downward** at `0.75`/`0.50`/`0.25`/`0.10` →
  `measured`/`frugal`/`conserve`/`wind_down`. Escalation begins as headroom falls below `0.75`, i.e.
  when less than three-quarters of budget pace is still affordable between now and reset. It needs
  no burn sample, so it is what governs a weekly limit days from reset, where burn pace is null by
  construction. No early-window cap is needed: headroom approaches `1 - consumed` early rather than
  exploding, so a fresh window reads `normal` on its own arithmetic — while a genuine early blowout
  (90% gone in a week's first five hours → headroom `0.10`) still escalates, which a cap would have
  wrongly softened.

  Worked examples, all from the test table: 97% spent with 2h left on a weekly → headroom `2.27` →
  `normal` (the incident case). The same 97% on day 3 → `0.05` → `wind_down`. 40% of a week spent
  on day 1 → `0.70` → `measured`, and if that sprint persists it tightens on its own (70% by day 2
  → `0.42` → `frugal`). 99% spent with 18% of a session still to run → provisional `wind_down` on
  headroom `0.06` alone, the opposite verdict from the same 97%-with-hours-to-spare case, decided
  entirely by the clock — but see "The downtime cap" immediately below for the v3 correction to
  this specific example: it lands one layer further at `measured`, not `wind_down`.

### The downtime cap

The worst-of-two-pace-readings rung above is **provisional**. v3 caps it by what exhausting the
limit would actually **cost**: `D = resets_in_seconds − exhaust_effective`, the wall-clock downtime
you would sit through if the window empties before it resets. `exhaust_effective` is the earliest
of the two axes' own `exhaust_seconds` (from `pace` and `window_pace`), each adjusted by a
kind-dependent margin applied to the projected *rate*, not to the raw seconds:
`exhaust_adjusted = exhaust_raw / margin`. `session` gets `0.85` (optimistic — a session is
use-it-or-lose-it, so a rosier read favors letting it run rather than throttling budget that was
about to expire anyway); every other kind, unknown kinds included, gets `1.15` (pessimistic — a
weekly overshoot costs days, so lean toward catching it sooner). `D` itself is banded: under 15
minutes costs nothing extra (`normal`), under 1h → `measured`, under 6h → `frugal`, under 1 day →
`conserve`; a full day or more of downtime is left uncapped. The final per-limit rung is
`min(provisional, cap)` — the cap can only **lower** a rung, never raise one. An exhausted limit
bypasses the cap and remains `wind_down`.

This derives the session/weekly asymmetry instead of hand-tuning it. A session window is at most
5h (18000s), so `resets_in_seconds` — and therefore `D` — can never reach the 21600s frugal/conserve
boundary: **a session's downtime cap structurally maxes out at `frugal`**, no matter how hard the
provisional pace rung escalates. Terminal exhaustion still produces `wind_down`. A weekly
window's reset can be up to 7 days out, so its `D` has no such
ceiling and the full ladder up to `wind_down` stays reachable. It also closes the v2 gap for
unfamiliar kinds: window pace has no length to judge an unknown `kind` by, but burn pace is
kind-agnostic, so an unknown kind with a burn sample still gets a provisional rung off pace alone
*and* a (pessimistic-margin) downtime cap — governance where v2 had none.

Worked example, continuing the one above: 99% spent, 82% of a session elapsed (18% / ~53m left).
Provisional `wind_down` on headroom alone. The margin-adjusted earliest exhaustion is only ~176s
out, leaving `D ≈ 50m` before the session's own reset — inside the `[900s, 3600s)` band, so the
cap is `measured`. Final rung: `measured`, `imminent: true`. Read together, that pair says: the
wall costs about 50 minutes, and the right move is to finish and checkpoint, not to throttle down
the ladder as if a multi-hour overrun were coming — `posture` alone would have said `wind_down`
and been misleading about *how much* was actually at stake. When `downtime` shows a capped rung,
`posture_reason` gets a `"; capped at <rung>: exhaustion at current pace would cost only ~<Xm> of
downtime before reset"` suffix, naming the cost that justified relaxing it.

**If the wall is soft:** should `extra` ever expose an overage or purchase option (metered usage
credits beyond the base allotment, for instance), exhaustion stops being pure downtime and becomes
a purchase decision to surface — weigh `D` accordingly, and say so explicitly rather than treating
a purchasable overage the same as a hard wait.

There is no percent axis: raw fullness (`percent`) never moves `posture`, except that a value which
displays as 100% is terminal exhaustion and always `wind_down`. An unjudgeable non-exhausted limit
is gray whether its data is fresh or stale. The provider's
fullness-derived `severity` field is discarded during normalization. There is likewise no near-reset
session relief and no early-window cap — the first existed to soften a high *percent* reading
shortly before reset, and with percent gone there is nothing left for it to soften; the second
existed to tame a ratio that exploded on a small denominator, and headroom does not explode.
Both are handled by the arithmetic now rather than by a carve-out. `posture_driver` names
the limit that set the rung (on the post-cap, pre-hysteresis reading), and `posture_reason`
explains why in one line, present even at `normal`. These thresholds are exactly the ones in the
sections below, so `posture` and the prose never disagree. Treat `posture` as a strong default,
not a hard gate: you may override it up or down using `burn`, `pace`, `window_pace`, task size,
and how close the reset is. If you diverge from `posture`, say why.

**Hysteresis.** `posture_computed` is this run's raw reading, before smoothing — usually identical
to `posture`. `posture` escalates the instant a worse rung computes (a real problem is never
hidden behind a delay); a de-escalation is held at the prior, worse rung until the improvement has
computed for at least 120 seconds straight, and a run that re-escalates in between resets that
clock. When the two diverge, `posture_reason` gets a `"; holding <stored> pending confirmation
(computed <computed> just now)"` suffix. This trades a little responsiveness on the way down for
not flapping every few minutes on a burn sample wobbling across a band edge.

### Admission control

Rungs govern what you **start**, never the speed or rigor of what is already running. The one
exception: genuinely divisible batch work (a migration processed file by file, a sweep over N
independent items) may shrink its *remaining* batch size mid-flight — that is still an admission
decision about the next unit of work, not a change to how thoroughly the current one gets done.

Before any large launch, run a fit test: estimate the task's cost from burn history of similar
work, then check it against what is left in the current window. If the estimate fits a *fresh*
window but not the remainder of *this* one, waiting for the reset is a first-class move — name
`resets_in_seconds` when you say so, not a vague "later." This is the flip side of the no-op floor:
doing nothing costs nothing, and a short wait for a clean reset is often cheaper than a throttled
run that limps across the wall anyway.

Caveat: endpoint burn (`burn`, `pace`) is **account-level** — every session sharing this account
contributes to the same sample. A fit test needs a *per-task* cost estimate (how much will THIS
job burn), not the aggregate pace number, which mixes in whatever else happens to be running.

If `stale` is true, the data is the last known snapshot (endpoint rate-limited or unreachable);
say so when reporting it. If `available` is false, usage is unknown: proceed normally but
conservatively, and do not guess numbers.

### The no-op floor

The floor is `posture == normal` **and** no `surplus` flag **and** no `imminent` flag on any
limit (v3: the last two are new conditions; either one is worth a single line even at `normal`,
since both are actionable — "about to run dry, checkpoint soon" or "budget on track to expire
unspent" — even when the rung itself says nothing is wrong). Below the floor, take no action and
say nothing about usage unless the user asked. A proactive check that comes back clear should
leave no trace in your plan or your reply. Do not hedge, do not pre-emptively shrink a fleet, do
not mention percentages. Silence below the floor is what keeps proactive checking from quietly
taxing everyday work. The floor is not a bare percent: a low-percent limit burning fast enough to
escalate on either pace axis is above the floor and worth surfacing, and conversely a high-percent
limit that is merely dead-on-budget for how far through its window it is stays below the floor —
which is exactly the 97%-with-hours-to-spare case a percent-only floor used to misread as urgent.

### New and non-percent limits

The skill does not assume today's limit taxonomy is permanent. Model line-ups and billing shift
often, a model can move from a scoped weekly limit to metered usage credits, a new surface can get
its own budget, so the script passes through every limit the endpoint returns, unknown `kind`s
included, and never drops one just because it lacks a `percent`. When you see one:

- **An unfamiliar `kind` with a percent** still gets burn pace (`pace`, kind-agnostic) but not
  window pace: `window_pace` has no window length to express the rate in for a `kind` it doesn't
  recognize, so it comes back `null` and says so in its half of `posture_reason` rather than
  guessing a length. It is not scored on percent — there is no percent-based fallback for an
  unfamiliar kind, by design; report it by whatever name the endpoint gave it and lean on `pace`
  and the raw numbers. It still gets a downtime cap (v3, pessimistic `MARGIN_POOL_RATE` margin,
  since nothing is known about the kind's shape) whenever a burn sample exists — the one piece of
  governance a v2 unfamiliar kind had none of.
- **A meter with `percent: null` and data in `extra`** (typically a usage-credit balance or spend)
  is not a utilization percent, so neither pace axis can be computed for it (both need `percent`),
  and it carries no `posture` contribution at all. Do not read the missing percent as "all
  clear": with no posture signal available for this meter, you are the only backstop. Surface it to
  the user in plain terms, and remember that credit-based usage is usually **real money**
  (pay-as-you-go), so it warrants an explicit heads-up before you spend a lot of it, not silent
  consumption. If the shape is unclear, say what the raw `extra` fields show rather than guessing at
  a percentage.

## When to check

- The user asks about usage, quota, or limits.
- Before starting anything large: a Workflow, a multi-agent fan-out, a /loop, a big migration or
  audit. One check, then decide scale.
- At phase boundaries inside long work (between finder and verifier rounds, between migration
  batches). These reads come from cache and cost nothing.
- Before delegating sub-work to a specific top-tier model: check that model's scoped limit first
  (see below).
- Not on every trivial turn, and never in a tight polling loop with `--fresh`.

## Turning usage into decisions

Thresholds below are guidance, not hard laws; combine them with task size and how far away the
reset is. A 90% session reading 10 minutes before reset is a very different situation from 90% with
4 hours to go — and it's the clock, not the percent, that tells them apart. Percent is a display
number, not a decision input: a 20% session that `pace.ratio` says will empty in under three hours,
with four hours to reset, binds like a much fuller window. Read `pace` and `window_pace`; treat
percent as color, never as the trigger.

### What frugality may and may not touch

Usage pressure changes *how much machinery* you throw at a task, never *how well you do the
task the user asked for*. When a limit pushes you toward frugality you may reduce parallelism
(smaller agent fleets), drop speculative extra-thoroughness passes, and downgrade the model tier
for mechanical sub-work. You may not shorten your reads, skip edge cases, cut corners on the
primary deliverable, or lower the depth and rigor of the actual answer. Budget is a reason to use
fewer agents, not a reason to think less. And it never lowers verification or Definition-of-Done
review below the level that work requires (see the model-scoped section). If the real work cannot
be done well within the remaining budget, say so and let the user decide, rather than silently
delivering a thinner result.

Each rung of the ladder is reached by either pace reading, whichever is worse (see "Reading the
output"), so the guidance below is written per rung, not per percent. A `frugal` driven by a fresh
burn spike and a `frugal` driven by a multi-day overspend get the same response below: act on the
rung, not on which pace reading raised it.

### Session (5-hour) limit: use-it-or-lose-it, governed by admission and the invariant

The session window is **use-it-or-lose-it** — whatever is unspent when it resets is simply gone,
not carried forward. Throttling to avoid its wall wastes budget that was about to expire anyway.
This is now arithmetic, not a hand-tuned exception: a session's downtime cap structurally
maxes out at `frugal` no matter how hard the provisional pace rung escalates (its window is at
most 5h, so `D` can never reach the 6-hour frugal/conserve boundary — see "The downtime cap"
above). Terminal exhaustion bypasses the cap and is `wind_down`. So the session's non-exhausted
rung mostly governs a gentle admission nudge:

- `measured`: right-size the fleet instead of reaching for maximum fan-out, and drop purely
  speculative extra passes. No constraint on the actual task yet.
- `frugal` (the session cap's practical ceiling): reduce parallelism and prefer cheaper model tiers
  for new sub-work.

What actually governs "act now" for a session is `imminent`, not the rung: `true` means the
margin-adjusted earliest exhaustion projection is under 30 minutes and lands before the reset.
That is when to checkpoint — finish the current unit of work, persist it, and either wait out the
(short) reset or admit nothing new until it rolls over (see "Admission control" above). It is
never a reason to throttle what is already in flight.

A high percent with pace still near or below 1.0 (dead on budget, or ahead of it) is `normal` — say
nothing, per the no-op floor — no matter how full the bar looks; only pace actually crossing these
thresholds moves the rung, and only `imminent` decides whether the wall is worth acting on at all.

### Weekly (all models) limit: governs the multi-day budget, admission-scaled

The same pace machinery applies — nothing shifts for the multi-day window, because both pace
readings already express themselves relative to their own window's length (`window_pace` in
particular is exactly `spent / elapsed`, in units of that window). What differs from the session is
the downtime cap's ceiling: a weekly window's reset can be up to 7 days out, so its `D` has no
structural cap the way a session's does, and the full ladder through `wind_down` stays reachable —
read the rung as how far to scale back admission of new work, per "Admission control" above, not a
speed dial on what's running. A weekly limit sitting at a high percent well ahead of the week's
elapsed fraction still reads `normal` — the 2026-08-11 case that motivated pace-only posture: 97%
spent with 2 hours (about 1.2% of a week) left to reset is a ratio near 0.98, not `wind_down`. The
same percent on day 3 of 7, roughly 43% elapsed, reads headroom `0.05` and lands at `wind_down`
(D ≈ 3.9 days, far past the 1-day cap ceiling, so nothing softens it). At `conserve` or `wind_down`,
scale back admission for the rest of the week's work and mention the
weekly reset time — and check `surplus` too: a weekly limit can just as easily be under-spent as
over-spent (see "Surplus and spend-down" below).

If `is_active` is true on a limit, that appears to be the budget
currently being drawn down; give it extra weight when deciding which limit's numbers to lead with.

## Surplus and spend-down

Pool budgets (`weekly_all`, `weekly_scoped`) are use-it-or-lose-it: whatever is unspent at reset is
gone, not rolled over. `surplus` flags a limit on track to expire with budget unused — past the
window's midpoint (50% elapsed) and projected, at the current average rate, to land under 75% at
reset. That is a **positive** signal (spend more), the mirror of posture's escalation signal
(spend less); the two never share a rung or a driver, because a single number cannot mean both at
once.

The response is to spend from a **deferral ledger**: every time posture (or plain judgment) makes
you defer a pass — a speculative thoroughness pass, extra verification breadth, a nice-to-have
sweep you would otherwise have run — write it down (your own notes or todo list, no special
mechanism needed). When `surplus` shows up, that ledger is what you spend it down against, and for
a scoped top-tier budget specifically (Fable, say), a surplus is licence to upgrade judgment-heavy
work to the scarce tier rather than defaulting to the cheaper one. Mention it once: it is
actionable, not urgent, so it clears the no-op floor (see
above) but should not be nagged about beyond that one line.

Deferral itself should be value-ordered: under pressure, defer the lowest-value work first (the
nice-to-have sweep, not the edge-case read), rather than shaving every pass by the same amount
uniformly.

## Fleets share the pools

Every session reading this skill draws against the same account-level limits and computes posture
from the same shared numbers. If several sessions in an orchestrated fleet each independently
shrink on `frugal`, they all pull back at once, the pace reading recovers because nobody is
spending, and they all resume and spike together — the same herd oscillation a single feedback
loop would avoid but N independent ones do not. When you are one session in an orchestrated fleet
(a Swarm board, a coordinated team of sessions), throttle authority belongs to the orchestrator:
report your posture reading rather than unilaterally shrinking, and let the coordinator decide the
fleet-wide response. Act on posture directly only when you are running solo, or stagger
and soften the response (a smaller step, not a full retreat) when you know siblings exist but no
orchestrator is coordinating.

## Model-scoped limits

The `limits` array can contain `weekly_scoped` entries tied to a specific model via
`scope_model`. Some models sometimes carry their own weekly budget on top of the shared one;
this is not guaranteed for any model, and the scoped set changes with plans and model line-ups.
When present, these per-model budgets can run out even when overall usage is fine, and they
matter most for top-tier models used deliberately for judgment-heavy sub-work.

Which models are scoped, and whether they are scoped at all, is not stable, a model can carry a
scoped weekly limit for a while and later move to metered usage credits (surfaced as a non-percent
meter, see "New and non-percent limits"). So read whatever `scope_model` values actually appear
this run; do not assume any specific model is present, and do not treat the absence of a scoped
limit as meaning that model is unlimited (it may now bill via credits instead).

Rules, generalized to whatever scoped models appear:

- Before delegating sub-work to a model that has a scoped limit, read that limit's own `posture`
  (computed the same pace-only way as any other limit) alongside its percent for context.
- If the scoped limit's `pace` or `window_pace` puts it at `frugal` or worse — it is on track to
  empty before its own reset, not merely sitting at a high percent — while overall usage is fine,
  prefer a different tier for that sub-work (for example Opus or Sonnet instead of Fable), or ask
  the user whether this task is worth spending the scarce budget on. A high percent that is still
  ahead of the window's elapsed fraction is not, on its own, a reason to downgrade. If you are
  running autonomously and cannot ask (a scheduled run, a /loop, an unattended workflow), take the
  safe default instead of blocking: drop to the cheaper tier, note the swap in your output, and let
  the user reverse it later.
- If the scoped limit has plenty of headroom, do not needlessly downgrade: the top tier is the
  right tool for its designated work (architecture, design decisions, adversarial verification,
  final reviews).
- This composes with the user's standing model-selection discipline (cheapest reliable tier for
  mechanical work, top tier for judgment work): usage state is one more input that can push a
  borderline choice down a tier when the relevant budget is tight. It never justifies
  downgrading verification or Definition-of-Done review work below what the discipline requires;
  if that work cannot be afforded at the right tier, say so and let the user decide.
- **Tier routing dominates weekly burn.** Model tier is the single biggest lever on weekly spend,
  and top-tier work draws down the scoped limit *and* the shared all-models pool simultaneously.
  When the weekly pool — not a scoped one — is what is binding, downgrading mechanical sub-work to
  a cheaper tier buys more headroom than any fleet-size shrink: a smaller fleet still running a
  top-tier model for grunt work spends the same weekly budget faster than a full fleet running a
  cheaper one for it.

## Communicating throttling decisions

When usage changes your plan, say so briefly and concretely: which limit, its percent (for color),
which pace reading actually drove it and the ratio, when it resets, and what you changed (fewer
agents, cheaper tier, deferred pass). The percent alone will often look harmless or look alarming in
a way that's wrong, so always name the pace number and the projected exhaustion time — that's the
part that's actually true. Offer the override in one line: "say the word and I'll run it at full
scale" or "tell me to use Fable anyway". Never silently throttle, and never silently blow through a
budget without warning.

Example (window-pace-driven): "Heads up: the Fable weekly limit is at 82% with only 40% of the
week elapsed (window_pace ~2.1x budget, resets Wednesday 19:00 UTC), so I'll run the design review
on Opus instead. Say the word if you want Fable anyway."

Example (burn-pace-driven): "The 5-hour session is only at 20%, but at the current pace it'll hit
the limit in ~2h54m, before it resets in 4h16m — the downtime cap puts the actual wait at only
~51m, so posture reads `measured`: I'll right-size the fan-out and drop the speculative extra
pass, but keep the same model tier (no downgrade at `measured`). Say the word if you'd rather I
just let it run at full scale."

Example (imminent — checkpoint, don't throttle, v3): "The session is at 99% with 53 minutes left,
but the wall only costs about 50 minutes of downtime (`imminent`, capped at `measured` rather than
`wind_down`), so I'm finishing this batch, checkpointing it, and picking back up after the reset —
not throttling work that was about to expire anyway."

In an autonomous run where no one is watching, you cannot offer a live override, but the
transparency rule still holds: record the same one-liner in your output or the run log, so the
decision is visible after the fact rather than silent.

## Failure modes

- `stale: true`: report the numbers but label them as last-known, with their age.
- 401 in the reason field: tell the user the token is stale and that running any Claude Code
  command refreshes it, then retry.
- No credentials: the script found no token in `CLAUDE_CODE_OAUTH_TOKEN`, the macOS Keychain, or
  `<config>/.credentials.json`. Usage is unavailable; say so plainly and proceed conservatively.
  On Windows the token lives at `%USERPROFILE%\.claude\.credentials.json` (or under
  `CLAUDE_CONFIG_DIR`); logging in via Claude Code creates it.
- Unrecognized response shape: usage is unavailable (the private format may have changed). Say so,
  proceed conservatively, never invent numbers, and also suggest the user check
  https://github.com/johnlawrimore/claude-usage for an updated version, since shape drift is
  exactly what an update fixes.
- Last-resort estimate only: local session logs (`~/.claude/projects/**/*.jsonl`, summed by tools
  like `npx ccusage`) can give a rough retrospective token estimate. It is known to be very
  inaccurate and measures spend, not quota. Only mention it if the endpoint is entirely
  unreachable, and clearly label it as a rough local estimate.
