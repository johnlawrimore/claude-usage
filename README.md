# claude-usage

A cache-served Claude subscription meter and work-admission policy. Its goal is to use useful
capacity through reset without exceeding quota. Python 3.9+, standard library only.

## Driving postures

**Accelerate → Cruise → Coast → Brake → Park**

| Posture | Color | Meaning |
| --- | --- | --- |
| Accelerate | Blue | Increase useful work toward the affordable rate. |
| Cruise | Green | Maintain the approach, targeting quota through reset. |
| Coast | Yellow | Ease new commitments and speculative extras. |
| Brake | Orange | Actively reduce admissions and parallelism. |
| Park | Red | Checkpoint, save, hand off; admit no new work until capacity returns. |

The script compares consumption with the rate affordable until reset. It prefers qualified
recent burn and otherwise uses the window average in the same units. Below 0.90 can justify
Accelerate; 0.90–1.00 is Cruise; above 1.00–1.25 is Coast; above 1.25 is Brake. Whole-window
expansion needs at least 5% elapsed and 2 points projected unused. Displayed 100% or a
margin-adjusted exhaustion forecast within 5 minutes before reset is Park. Short projected
interruptions can soften Brake to Coast, but never authorize more spending. The most
restrictive applicable limit wins. Easing is confirmed for 120 seconds; restrictions are immediate.

Posture governs new work, not quality. Required tests, review tiers and gates always stand.
Useful expansion is coordinated across the shared fleet; busywork is never a goal. The script
knows aggregate quota changes, not the agent count or each session's token draw.

## Output and widgets

Run `scripts/check-usage.sh --json` (or `python3 scripts/check-usage.py --json`). Normal reads
share a five-minute cache and respect rate-limit backoff. The output includes identity-bound
account metadata, freshness, every provider limit, a binding driver and explanation, and
`posture_instruction` so agents can act on the recommendation. `posture_policy` is
`ride_limit_v1`; `posture_computed` exposes the pre-smoothing result.

The quota ring's `color`, the independent `display_pace` multiplier, and the posture's
`posture_color` have distinct meanings. The ring compares consumption against elapsed time;
the multiplier compares consumption rate against remaining affordable rate; the posture
adds admission thresholds, checkpoint reserve, freshness and smoothing. Two quota clocks do
not replace the overall recommendation: a model-scoped limit can bind the account too.

`imminent` is an earlier 30-minute checkpoint warning from the conservative earliest forecast.
It can coexist with a recovered recent pace. Unknown and stale readings grant no expansion.
See [SKILL.md](SKILL.md) for the complete field and agent behavior contract.

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
Claude Code credentials. Runs on macOS, Windows, and Linux; requires only Python 3.9+. To update,
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

## Caveats

**It rides a private endpoint.** Usage data comes from `GET /api/oauth/usage`, a private,
undocumented Anthropic endpoint (the same backend behind claude.ai's usage page). It is
aggressively rate-limited, which is exactly why the script caches hard and backs off on a 429. It
could change or vanish without warning; when it does, the script falls back to the last known
reading (clearly labeled stale) and never makes a number up.

**Your token stays put.** The OAuth access token is resolved in order from
`CLAUDE_CODE_OAUTH_TOKEN`, the macOS Keychain, `<config>/.credentials.json`, or a running Claude
Code process's own environment (via `ps`, same-user only), and goes nowhere except Anthropic's own
`/api/oauth/usage` and `/api/oauth/profile` endpoints — never printed, logged, or sent anywhere
else. The cache file is `0600` on POSIX (a no-op on Windows/NTFS, where it inherits your profile's
ACLs anyway); it holds the raw usage response, resolved account/profile metadata (email, account
UUID, organization UUID), and a rolling diagnostic trail of the last 20 HTTP error bodies
(truncated to 2000 characters each) — never the OAuth token itself.

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
  failed. Before pasting the output into a public issue, strip the `account` block — it carries
  your email and account/organization UUIDs — or paste just the `reason` and `limits` fields,
  which carry usage numbers only, no personal data.
- **Check for a fix first:** shape drift is exactly what updates fix.
  `git -C ~/.claude/skills/claude-usage pull`
- **Report it:** [open an issue](https://github.com/johnlawrimore/claude-usage/issues) with
  that JSON line and your version (`check-usage.py --version`). Fast reports are what keep an
  endpoint-riding tool working for everyone.

### Versioning

The five driving posture names are a breaking JSON change in version 2.0.0. Consumers
should migrate together or temporarily map legacy names conservatively.

## Layout

```
claude-usage/                 # cloned as-is to ~/.claude/skills/claude-usage
├── SKILL.md                   # skill instructions Claude Code loads
├── README.md                  # this file
├── scripts/
│   ├── check-usage.py         # the logic (Python 3.9+, stdlib only)
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

Display multiplier: `display_pace` reports `{ratio, basis, color}`, where ratio is consumption rate divided by remaining quota / time until reset. A reliable recent sample is preferred; otherwise `basis: "window_average"` uses the whole-window average in the same units. Rate colors are blue below 0.9x, green 0.9–1.1x, yellow above 1.1 through 2x, orange above 2x; unavailable is null/gray. These display fields do not alter posture, admission, or forecasts.
