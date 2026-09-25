#!/usr/bin/env python3
"""check-usage: report claude.ai subscription usage (session, weekly, per-model, and any
future limit kinds such as usage-credit meters).

Cross-platform (macOS, Windows, Linux), Python standard library only. Normally invoked through
the small OS launchers next to it (check-usage.sh on Unix, check-usage.cmd on Windows), but it can
be run directly with Python 3.8+.

Data source: the private endpoint GET https://api.anthropic.com/api/oauth/usage (the same backend
behind claude.ai /settings/usage and Claude Code /usage). It is aggressively rate-limited, so this
script caches responses and only hits the network on a cache miss.

Beyond the raw percents it derives several decision aids so callers do not have to re-implement
the policy each time:
  - resets_in_seconds : time-to-reset per limit (models are unreliable at date math)
  - burn              : climb over roughly the last 15+ minutes vs an earlier snapshot ("+6% / 45m")
  - pace              : {ratio, exhaust_seconds} -- current burn vs the rate you can afford before
                        reset; ratio > 1 means the window empties before it resets
  - window_pace       : {ratio, headroom, exhaust_seconds} -- the same question asked across the
                        WHOLE window (consumed fraction vs elapsed fraction), needing no burn
                        sample at all
  - downtime          : {seconds, cap} -- the wall-clock downtime a reset-driven wait would cost at
                        the margin-adjusted earliest exhaustion projection, and the posture cap
                        that cost derives (v3)
  - runway_seconds, imminent : the same earliest projection per limit, plus a flag for "about to
                        run dry -- checkpoint now, don't throttle" (v3)
  - surplus           : {projected_final_percent, unspent_percent} -- underspend detection for a
                        pool budget on track to expire unspent (v3)
  - posture           : normal | measured | frugal | conserve | wind_down, from pace, the downtime
                        cap, then hysteresis-smoothed run to run (v3)
  - posture_computed  : this run's raw posture before hysteresis (v3)
  - posture_reason    : one line saying why posture is what it is

Design note on brittleness: the endpoint's set of limit "kinds" is NOT assumed to be fixed. Model
line-ups and billing change often (for example a model moving from a scoped weekly limit to
metered usage credits). Every limit the endpoint returns is passed through, unknown kinds
included, and any meter that is not a 0-100 percent is surfaced under `extra` rather than dropped,
so a new kind of budget never silently reads as "all clear".

Credentials (checked in order): the CLAUDE_CODE_OAUTH_TOKEN env var; the macOS Keychain item
"Claude Code-credentials"; then <config>/.credentials.json where <config> is CLAUDE_CONFIG_DIR or
~/.claude. The token is used in-process for the provider requests and is never printed or logged.

Flags:  --json (print only the JSON line)   --fresh (ignore the cache TTL; still respects backoff)
        --cache-only (read/recompute cached data; mechanically skips credentials and network)
        --help / -h (print usage, exit 0)   --version (print version, exit 0)
Env:    CLAUDE_USAGE_CACHE, CLAUDE_USAGE_TTL, CLAUDE_USAGE_429_BACKOFF, CLAUDE_CONFIG_DIR
Exit:   0 = printed usage data (fresh or stale) OR --help/--version output, 2 = no data
        available / bad flag.
Version: 1.0.0

Author: John Lawrimore (https://github.com/johnlawrimore/claude-usage). MIT license.
"""

import getpass
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Don't let an odd byte in a display name crash output on a legacy Windows console (cp1252/cp437);
# the JSON line is ASCII-safe already, but the human summary passes scope names through print().
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

NOW = time.time()
VERSION = "1.0.0"


def is_num(v):
    """True for a real, finite number. Excludes bool (a subclass of int) and NaN/Infinity, which
    json.loads accepts and which would otherwise crash round() or emit non-strict JSON."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _num_or_none(v):
    """Normalize a percent to a finite number or None, so NaN/Infinity (which json.loads accepts)
    never reach the output as non-strict JSON literals. Shared utility: used by the provider
    section's parse_limits() below, but kept here because it's generic (not endpoint-specific)."""
    return v if is_num(v) else None

HISTORY_MAX = 72          # cap snapshots kept in the cache (~6h at the 5-min TTL)
HISTORY_MAX_AGE = 21600   # 6h: don't compute burn across gaps older than this
BURN_MIN_ELAPSED = 120    # need >=2 min between samples for a meaningful delta
BURN_TARGET_WINDOW = 900  # 15 min: preferred baseline age for a "recent climb" reading
# Two resets_at count as the same window if within this many seconds. The endpoint jitters the
# sub-second part of resets_at across responses (e.g. ...59.760271 vs ...59.935632) for one and the
# same window, so an exact string match would reject every valid baseline and leave burn null. A
# genuine window flip moves the reset by the window length (session 5h, weekly 7d), far beyond this
# tolerance, so it is still caught. 5 min is comfortably between the two scales.
WINDOW_MATCH_TOLERANCE_S = 300

# The posture ladder, weakest to strongest. _worst walks this list, so inserting rungs Just Works.
#
# POSTURE REACTS TO PACE, NEVER TO PERCENT (John, 2026-08-11). Percent stays in the JSON because
# it is the number a human reads, but it is a display fact and never a decision input. Exhaustion
# is the narrow exception: a displayed 100% is a terminal fact, not a fullness band.
#
# What that rule is protecting against, concretely: on 2026-08-11 the weekly limit sat at 97%
# with 2 hours left before reset. Two hours is ~1.2% of a week, so 3% remaining was AHEAD of
# pace and nothing was wrong -- but a percent band said wind_down, worst-wins discarded the
# correct pace reading, and all six runners in the fleet stopped working on a healthy account.
# Fullness alone cannot tell you that, because the same 97% is fine on day 6.9 and alarming on
# day 3. Only the clock distinguishes them, which is why the axis has to be a rate.
LEVELS = ["normal", "measured", "frugal", "conserve", "wind_down"]
POSTURE_COLORS = {
    "normal": "green",
    "measured": "blue",
    "frugal": "yellow",
    "conserve": "orange",
    "wind_down": "red",
}
EXHAUSTED_PERCENT = 99.5  # render() rounds this and higher to the displayed value "100%"


def is_exhausted(l):
    """True exactly when this limit's displayed rounded percentage is 100% or more."""
    return is_num(l.get("percent")) and l["percent"] >= EXHAUSTED_PERCENT

# --- Pace axis -----------------------------------------------------------------------------
# Where a limit is heading. pace_ratio = actual burn rate / the rate you could afford and still
# coast to reset, which is exactly seconds_to_reset / seconds_to_exhaustion. 1.0 means you land
# on empty right at reset; >1 means you hit the wall early (2.0 = at the halfway mark). Bands are
# high-to-low like the percent table. Escalation only ever starts at ratio >= 1.0, i.e. only when
# the current pace would genuinely exhaust the window before it resets.
PACE_BANDS = [(4.0, "wind_down"), (2.0, "conserve"), (1.25, "frugal"), (1.0, "measured")]
# Ignore burn smaller than this (percentage points) for pace escalation: a sub-2% wiggle over a
# short baseline extrapolates to wildly noisy rates, so it must not drive posture on its own.
PACE_MIN_BURN_DELTA = 2.0
# Runway threshold for the `imminent` flag (v3). A limit is "about to run dry" when the
# margin-adjusted earliest exhaustion projection (see the Downtime cap section below) lands
# within this many seconds AND before the reset itself. This constant used to floor posture at
# 'conserve' directly -- the v2 absolute-runway backstop, deleted from pace_posture below. Its job
# moved here: imminent is a signal for checkpoint discipline, never a posture escalation on its
# own, because throttling a use-it-or-lose-it session window wastes budget that was about to
# expire anyway (see "admission control" in SKILL.md).
IMMINENT_RUNWAY_S = 1800
# Only judge pace when the reset is within this horizon. Burn is a ~15-minute reading; extrapolating
# it linearly across a multi-day weekly window is meaningless (nobody sustains a 15-minute burst for
# three days), and doing so makes the ratio explode past every middle band so a heavy session would
# slam a weekly limit straight to wind_down. Within the horizon -- the whole 5-hour session window,
# and the tail of a weekly window near its reset -- a short burst does project usefully, and the
# measured/frugal/conserve rungs become reachable. Beyond it, BURN pace reads null; the window
# pace below still governs. 6h ties to HISTORY_MAX_AGE, the oldest baseline burn will ever use.
PACE_MAX_HORIZON_S = 21600

# --- Window pace: the same question, asked without a burn sample ----------------------------
# Burn pace above is a ~15-minute sample extrapolated, so it needs guards against its own noise
# (PACE_MIN_BURN_DELTA, PACE_MAX_HORIZON_S) and each guard makes it read null. Null pace used to
# be harmless because the percent axis governed underneath it; with percent gone, a null would
# mean no governance at all, and on 2026-08-11 the weekly limit was null for exactly this reason
# (its burn moved 1.0 point, under the 2.0 floor).
#
# Nothing external is missing, though: percent-consumed and the clock are both always present,
# so pace can be computed directly with no sampling, no baseline and no history --
#
#     ratio = (consumed / 100) / (elapsed_window_seconds / window_length_seconds)
#
# -- the fraction of the budget spent against the fraction of the window used to spend it. 1.0
# means dead on budget; >1 means this window empties before it resets. It is defined at every
# instant of every window, including at 100% consumed, where it degrades exactly right rather
# than needing a special case.
#
# This is a rate over a rate, not a threshold on fullness: 97% reads 0.98 (normal) on day 6.9 and
# 2.26 (wind_down, via headroom 0.05 -- see WINDOW_HEADROOM_BANDS below) on day 3. The clock is
# load-bearing, which is what separates it from the percent axis that was removed. It consumes
# the percent FIELD, as burn pace always has (`remaining = 100 - pct`); what the rule bans is
# deciding on fullness alone.
#
# Window length is a physical property of each window, the unit this formula is expressed in --
# not per-kind posture logic. One formula, one band table, these are its units. An unknown kind
# gets no window pace (and says so) rather than a guessed length.
WINDOW_LENGTHS = {
    "session": 5 * 3600,
    "weekly_all": 7 * 24 * 3600,
    "weekly_scoped": 7 * 24 * 3600,
}
# The window axis does NOT band on that ratio, and does not share PACE_BANDS. It bands on
# HEADROOM: the multiple of budget pace you can still afford from now until reset.
#
#     headroom = (1 - consumed_fraction) / (1 - elapsed_fraction)
#              = remaining budget / remaining time, in budget-pace units
#
# h = 1.0 means you can spend at exactly budget pace the rest of the way; 0.5 means half speed
# from here; 0 means stop. Escalation runs DOWNWARD -- the bands trip as h falls.
#
# Why headroom rather than the ratio: a flat threshold on consumed/elapsed cannot serve a window
# that drains. The ratio's own ceiling is 1/elapsed_fraction, so `wind_down` at 4.0 is
# arithmetically unreachable past the first quarter of any window, while early on the same ratio
# explodes on a tiny denominator and needs a cap bolted on to stay sane. Headroom is well behaved
# at both ends: early it is ~= 1 - consumed, and late it collapses precisely when the runway does.
# Banding h is algebraically an elapsed-dependent threshold on the ratio -- the bands tighten as
# the window drains -- but it falls out of one formula instead of being hand-tuned per rung.
#
# It also subsumes two things that used to be special cases. A window seconds from reset gives a
# huge h and reads normal without the session-only near-reset relief that was deleted; a window
# fully consumed gives h = 0 and reads wind_down without the `remaining <= 0` null that used to
# hand the question to the percent axis.
WINDOW_HEADROOM_BANDS = [(0.10, "wind_down"), (0.25, "conserve"), (0.50, "frugal"), (0.75, "measured")]

# Snapshots kept for burn-rate math; populated from the cache in main().
HISTORY = []


# --- PROVIDER: Anthropic /api/oauth/usage ---------------------------------------
# A provider exposes two things to the rest of this script:
#   fetch(token) -> (status, data)         : perform the one network request
#   parse_limits(data) -> [normalized limit dict, ...] or None : normalize the raw response
# Everything OUTSIDE this section (posture, burn, cache, render, credentials) must not
# reference the endpoint URL or its raw field names -- swapping providers means
# replacing only this section.

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
PROFILE_ENDPOINT = "https://api.anthropic.com/api/oauth/profile"
PROFILE_TTL = 3600

# Fields of a raw limit that we normalize by name; everything else a limit carries is preserved
# under `extra` so new meters (e.g. a usage-credit balance) are surfaced rather than lost.
KNOWN_LIMIT_KEYS = {"kind", "percent", "severity", "is_active", "resets_at", "scope"}


def fetch(token):
    req = urllib.request.Request(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Preserve WHY the endpoint refused. This used to `return e.code, None`,
        # which discarded the body and every header -- so a 429 was
        # indistinguishable from any other 429, with no way to tell a "slow
        # down for N seconds" from a longer-horizon refusal. A two-hour outage
        # was diagnosed blind because of it. `Retry-After` in particular is the
        # server telling us exactly how long to wait; throwing it away and
        # substituting our own fixed 900s guess is strictly worse information.
        # Errors here are swallowed: a diagnostic must never turn a handled
        # HTTP error into an unhandled exception.
        detail = {"headers": {}, "body": None}
        try:
            detail["headers"] = {k.lower(): v for k, v in e.headers.items()}
        except Exception:
            pass
        try:
            raw = e.read().decode("utf-8", "replace")
            detail["body"] = raw[:2000] if raw else None
        except Exception:
            pass
        _record_http_error(e.code, detail)
        return e.code, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None, None


def fetch_account(token):
    """(status, metadata) for the non-secret account bound to this exact token."""
    req = urllib.request.Request(
        PROFILE_ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200 or resp.url != PROFILE_ENDPOINT:
                return resp.status, None
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = {"headers": {}, "body": None}
        try:
            detail["headers"] = {k.lower(): v for k, v in e.headers.items()}
        except Exception:
            pass
        try:
            raw = e.read().decode("utf-8", "replace")
            detail["body"] = raw[:2000] if raw else None
        except Exception:
            pass
        _record_http_error(e.code, detail, endpoint="profile")
        return e.code, None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        _record_http_error(None, {
            "headers": {},
            "body": f"{type(error).__name__}: {error}",
        }, endpoint="profile")
        return None, None
    if not isinstance(payload, dict):
        return 200, None
    account = payload.get("account")
    organization = payload.get("organization")
    if not isinstance(account, dict) or not isinstance(organization, dict):
        return 200, None
    email = account.get("email")
    account_uuid = account.get("uuid")
    organization_uuid = organization.get("uuid")
    if not all(isinstance(value, str) and value for value in
               (email, account_uuid, organization_uuid)):
        return 200, None
    raw_type = organization.get("organization_type")
    subscription_type = None
    if isinstance(raw_type, str) and raw_type:
        subscription_type = (raw_type[len("claude_"):] if raw_type.startswith("claude_")
                             else raw_type)
    raw_tier = organization.get("rate_limit_tier")
    return 200, {
        "email": email.strip(),
        "account_uuid": account_uuid,
        "organization_uuid": organization_uuid,
        "subscription_type": subscription_type,
        "rate_limit_tier": raw_tier if isinstance(raw_tier, str) and raw_tier else None,
    }


def _record_http_error(status, detail, endpoint="usage"):
    """Append one endpoint-failure observation to the cache's diagnostic ring.

    Kept to the last 20 entries: this is a diagnostic breadcrumb trail, not a
    log, and the cache file is read on every invocation by every session on
    this machine. Re-loads the cache from disk before writing for the same
    reason the 429 branch does -- another process may have landed a successful
    fetch since this one started, and a naive read-modify-write would clobber
    it. Wrapped whole: a diagnostic that can break the tool it is diagnosing
    is worse than no diagnostic.
    """
    try:
        entry = {"at": NOW, "status": status, "endpoint": endpoint}
        headers = detail.get("headers") or {}
        for key in ("retry-after", "x-ratelimit-reset", "x-ratelimit-remaining",
                    "x-ratelimit-limit", "x-should-retry", "request-id"):
            if key in headers:
                entry[key] = headers[key]
        if detail.get("body"):
            entry["body"] = detail["body"]
        fresh = load_cache()
        errors = fresh.get("http_errors")
        if not isinstance(errors, list):
            errors = []
        errors.append(entry)
        fresh["http_errors"] = errors[-20:]
        # Promote the server's own wait out of the breadcrumb ring and into the
        # matching endpoint's backoff field. Only for 429 and only when it parses as
        # a plain delta-seconds integer: RFC 9110 also permits an HTTP-date, and
        # a value we cannot read must fall back to our own floor rather than
        # silently become 0 and defeat the backoff entirely.
        if status == 429:
            raw = headers.get("retry-after")
            try:
                seconds = int(str(raw).strip())
            except (TypeError, ValueError):
                seconds = 0
            if seconds > 0:
                fresh["profile_retry_after_s" if endpoint == "profile"
                      else "retry_after_s"] = seconds
        if endpoint == "profile":
            fresh["profile_failed_at"] = NOW
            fresh["profile_status"] = status
        save_cache(fresh)
    except Exception:
        pass


def _backoff_cause(cache):
    """Describe what actually armed the backoff, rather than assuming a 429.

    `last_429_at` is armed on ANY HTTP status, not just 429 -- deliberately,
    so a recurring non-retryable error cannot hot-loop the endpoint. The cost
    of reusing that one field is that the wait was *reported* as a rate limit
    whatever caused it. Observed misleading readers across several sessions
    (2026-08-16): 19 of the last 20 recorded errors were an HTTP 403
    "does not meet scope requirement user:profile", exactly one was a real
    429, and every report named only the 429 -- so the actionable cause (a
    credential without the required scope) stayed invisible while the meter
    sat 83 hours stale.

    Returns (label, detail): label is short enough for the `source` field,
    detail is a full clause for `fail()`.
    """
    latest = None
    status = None
    try:
        errors = cache.get("http_errors")
        if isinstance(errors, list) and errors and isinstance(errors[-1], dict):
            latest = errors[-1]
            status = latest.get("status")
    except Exception:
        pass
    if status is None or status == 429:
        return "429 backoff", "endpoint rate-limited (429)"
    body = latest.get("body") or ""
    if status == 403 and "user:profile" in body:
        return ("403 scope backoff",
                "the OAuth token in use lacks the 'user:profile' scope this endpoint "
                "requires (HTTP 403 -- a permission error, not a rate limit). Note that "
                "CLAUDE_CODE_OAUTH_TOKEN, when set, takes precedence over the Keychain "
                "item, so a scope-less token in the environment shadows a good one")
    message = ""
    try:
        message = ((json.loads(body).get("error") or {}).get("message") or "").strip()
    except Exception:
        pass
    return ("HTTP %s backoff" % status,
            "endpoint returned HTTP %s%s (not a rate limit)"
            % (status, ": " + message if message else ""))


def _scope_model(l):
    scope = l.get("scope")
    if isinstance(scope, dict):
        model = scope.get("model")
        if isinstance(model, dict):
            return model.get("display_name")
    return None


def _extra_fields(l):
    """Every field of a raw limit we don't normalize by name, preserved so a new meter (e.g. a
    usage-credit balance) is surfaced rather than dropped -- including nested objects, since a
    balance may well arrive as {"balance": {"remaining": 42, "currency": "usd"}}. Everything here
    came from json.loads, so it is JSON-serializable by construction."""
    extra = {k: v for k, v in l.items() if k not in KNOWN_LIMIT_KEYS}
    return extra or None


def parse_limits(data):
    """Normalize the endpoint's limits into a stable list; None if nothing is recognizable.

    Every dict entry is kept, even one without a `percent` (its percent is None), so an unfamiliar
    limit kind or a non-percent meter still appears in the output. Callers must not read a missing
    percent as 'fine'."""
    limits = data.get("limits") if isinstance(data, dict) else None
    out = []
    if isinstance(limits, list):
        for l in limits:
            if not isinstance(l, dict):
                continue
            resets_at = l.get("resets_at")
            s = seconds_until(resets_at)
            out.append({
                "kind": l.get("kind"),
                "percent": _num_or_none(l.get("percent")),
                # assumption, unverified as of 2026-07: appears to mean the currently binding limit
                "is_active": l.get("is_active"),
                "resets_at": resets_at,
                "resets_in_seconds": int(s) if s is not None else None,
                "scope_model": _scope_model(l),
                "scope": l.get("scope"),
                "extra": _extra_fields(l),
            })
    if out:
        return out
    # Fallback: older/simpler shape with just five_hour / seven_day blocks.
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        blk = data.get(key) if isinstance(data, dict) else None
        if isinstance(blk, dict) and "utilization" in blk:
            util = blk.get("utilization")
            # 'utilization' is conventionally a 0-1 fraction; scale to 0-100. Values already above
            # 1 are assumed to be percents and left as-is. Heuristic caveat: a genuine sub-1% value
            # (0.5 meaning 0.5%) would be misread as 50%. This only affects the legacy fallback
            # shape, which the live endpoint does not currently emit.
            if isinstance(util, (int, float)) and 0 <= util <= 1:
                util = util * 100
            resets_at = blk.get("resets_at")
            s = seconds_until(resets_at)
            out.append({
                "kind": kind,
                "percent": _num_or_none(util),
                "is_active": None,
                "resets_at": resets_at,
                "resets_in_seconds": int(s) if s is not None else None,
                "scope_model": None,
                "scope": None,
                "extra": None,
            })
    return out or None

# --- end PROVIDER section ---------------------------------------------------


# --------------------------------------------------------------------------- config / flags

def config_dir():
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return override or os.path.join(os.path.expanduser("~"), ".claude")


CACHE_PATH = os.environ.get("CLAUDE_USAGE_CACHE") or os.path.join(config_dir(), ".usage-cache.json")


def _int_env(name, default):
    try:
        v = int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default
    # Clamp to >= 0: a negative TTL/backoff has no sane meaning here, so fall back to the
    # default rather than accepting it. Zero stays allowed (e.g. TTL=0 means "always fetch").
    if v < 0:
        return default
    return v


TTL = _int_env("CLAUDE_USAGE_TTL", 300)
BACKOFF = _int_env("CLAUDE_USAGE_429_BACKOFF", 900)
# Upper bound on how long a server-sent `Retry-After` may park us, and the
# margin added to it so a retry never lands exactly on the boundary.
RETRY_AFTER_CAP = _int_env("CLAUDE_USAGE_RETRY_AFTER_CAP", 21600)
RETRY_AFTER_MARGIN = 15

JSON_ONLY = False
FORCE_FRESH = False
CACHE_ONLY = False

USAGE_TEXT = """usage: check-usage.py [--json] [--fresh] [--cache-only] [--help] [--version]

  --json      print only the machine-readable JSON line
  --fresh     ignore the cache TTL (still respects 429 backoff)
  --cache-only read and recompute cached data without loading credentials or contacting the API
  -h, --help  show this help message and exit
  --version   show the version number and exit
"""


def parse_args(argv):
    """Parse CLI flags, setting the JSON_ONLY/FORCE_FRESH globals. Called from main() so that
    importing this module never inspects sys.argv or exits (decision 9: testability).

    --help/--version are handled here and exit(0) before any cache/network work; this is the one
    deliberate exception to the "JSON on every exit path" contract -- they are not data queries."""
    global JSON_ONLY, FORCE_FRESH, CACHE_ONLY
    for _arg in argv:
        if _arg in ("-h", "--help"):
            print(USAGE_TEXT, end="")
            sys.exit(0)
        elif _arg == "--version":
            print(f"check-usage {VERSION}")
            sys.exit(0)
        elif _arg == "--json":
            JSON_ONLY = True
        elif _arg == "--fresh":
            FORCE_FRESH = True
        elif _arg == "--cache-only":
            CACHE_ONLY = True
        else:
            # Honor the "JSON on every exit path" contract even for a bad flag.
            sys.stderr.write(f"unknown flag: {_arg}\n")
            print(json.dumps({"available": False, "reason": f"unknown flag: {_arg}"}))
            sys.exit(2)


# --------------------------------------------------------------------------- credentials

def _keychain_blob():
    """The credentials JSON from the macOS Keychain, or None (non-mac or not found)."""
    if platform.system() != "Darwin":
        return None
    try:
        account = (os.environ.get("CLAUDE_CODE_KEYCHAIN_ACCOUNT", "").strip()
                   or getpass.getuser())
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials",
             "-a", account, "-w"],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        if out.returncode == 0 and out.stdout and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _file_blob():
    """The credentials JSON from <config>/.credentials.json, or None."""
    path = os.path.join(config_dir(), ".credentials.json")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _process_env_token():
    """Harvest CLAUDE_CODE_OAUTH_TOKEN from a running claude CLI process's environment
    (same-user only; macOS/Linux). Newer CLI versions broker auth in-memory and inject the
    token into each session's env instead of persisting claudeAiOauth to the Keychain, so
    a live process is the freshest source. Returns the token or None; never logs it."""
    if platform.system() not in ("Darwin", "Linux"):
        return None
    try:
        pids = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True,
                              timeout=10).stdout.split()
        for pid in pids[:8]:
            out = subprocess.run(["ps", "eww", "-p", pid], capture_output=True, text=True,
                                 timeout=10).stdout
            for chunk in out.split():
                if chunk.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                    tok = chunk.split("=", 1)[1].strip()
                    if tok:
                        return tok
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _sessionwatcher_token():
    """Token from SessionWatcher's credentials store (macOS), if present and unexpired.
    SessionWatcher keeps its own auto-refreshed Claude OAuth pair, making it a reliable
    fallback when no CLI process is running. Returns the token or None; never logs it."""
    path = os.path.expanduser(
        "~/Library/Application Support/SessionWatcher/credentials.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        exp = d.get("expiresAt")
        if isinstance(exp, (int, float)):
            exp_s = exp / 1000 if exp > 1e12 else exp
            if exp_s < time.time():
                return None
        tok = d.get("accessToken")
        return tok or None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _blob_access_token(raw):
    """Return a stored access token only while its declared lifetime is valid.

    Claude Code can refresh a token inside a running process without immediately
    replacing the Keychain record. Treating that expired record as authoritative
    prevents the live-process fallback below from ever being considered.
    """
    try:
        oauth = json.loads(raw)["claudeAiOauth"]
        tok = oauth["accessToken"]
    except (ValueError, KeyError, TypeError):
        return None
    expires_at = oauth.get("expiresAt")
    if is_num(expires_at):
        expires_at_s = expires_at / 1000 if expires_at > 1e12 else expires_at
        if expires_at_s <= time.time():
            return None
    return tok if isinstance(tok, str) and tok else None


def get_access_token():
    """Resolve the OAuth access token from (in order) the env override, the macOS Keychain,
    the credentials file, SessionWatcher's store, then a running claude process's env.
    Returns the token string or None. Never logs the value.

    SessionWatcher moved ahead of the process-env scavenge (2026-08-13, live incident):
    `_process_env_token()` greps `pgrep -x claude` and takes whichever of up to 8 matching
    PROCESSES happens to be found first -- on a machine running many concurrent sessions
    (interactive, runners, subagents), that is an ARBITRARY session, not necessarily the
    one whose account the human actually controls or intends. Confirmed live: the human
    reported switching accounts several times; the process-scavenged token kept returning
    HTTP 429/403 ("does not meet scope requirement user:profile") for 6+ hours regardless,
    while SessionWatcher's own token -- a real, separately-authenticated, auto-refreshed
    login this whole time -- succeeded on the very first live retry. The account-switching
    was real; it just could never have reached this script, because nothing about it
    changes which arbitrary background process pgrep finds first. Process-env stays as a
    fallback for when SessionWatcher isn't installed, not as the primary source."""
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok
    for raw in (_keychain_blob(), _file_blob()):
        if not raw:
            continue
        tok = _blob_access_token(raw)
        if tok:
            return tok
    return _sessionwatcher_token() or _process_env_token()


# --------------------------------------------------------------------------- cache / network

def fail(reason, hint=""):
    msg = {"available": False, "reason": reason}
    if hint:
        msg["hint"] = hint
    if not JSON_ONLY:
        print(f"Usage unavailable: {reason}")
        if hint:
            print(f"Hint: {hint}")
    print(json.dumps(msg))
    sys.exit(2)


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            c = json.load(f)
        # Schema version gate: only a cache stamped "v": 1 by this version's save_cache() is
        # honored. Missing (pre-version), older, or newer values are all treated as absent rather
        # than guessed at -- so the first run after upgrading from a pre-version cache simply
        # discards the old cache and makes one extra fetch. That's cheap and safer than reading a
        # shape this version didn't write.
        if isinstance(c, dict) and c.get("v") == 1:
            return c
    except (OSError, ValueError):
        pass
    return {}


def save_cache(cache):
    # Copy so we never mutate the caller's dict, then stamp the schema version so every call site
    # (the success path and the 429 path alike) writes a versioned cache automatically.
    cache = dict(cache)
    cache["v"] = 1
    try:
        cache_dir = os.path.dirname(CACHE_PATH) or "."
        os.makedirs(cache_dir, exist_ok=True)  # e.g. a fresh CLAUDE_CONFIG_DIR
        # Unique temp name in the same dir so two concurrent runs can't clobber each other's write.
        fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".usage-cache.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, CACHE_PATH)  # atomic on POSIX and Windows
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.chmod(CACHE_PATH, 0o600)  # best-effort; a no-op on Windows/NTFS
        except OSError:
            pass
    except OSError:
        pass  # caching is best-effort; never block the report on it


# --------------------------------------------------------------------------- time helpers

def seconds_until(iso_ts):
    """Signed seconds from now to an ISO 8601 timestamp; None if unparseable."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)).total_seconds()
    except (ValueError, AttributeError, TypeError):
        return None


def _reset_epoch(iso_ts):
    """ISO 8601 -> absolute POSIX seconds, or None if unparseable. Unlike seconds_until (which
    measures against a moving 'now'), this is stable for comparing two stored reset timestamps."""
    try:
        return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def same_window(base_reset, cur_reset):
    """True if two resets_at denote the same reset window, tolerant of the sub-second jitter the
    endpoint adds across responses (see WINDOW_MATCH_TOLERANCE_S). If either side is missing, treat
    as the same window: an older cache without a stored reset cannot prove a flip, and the
    negative-delta check in compute_burn still guards a genuine reset. If either fails to parse,
    fall back to exact string equality."""
    if base_reset is None or cur_reset is None:
        return True
    a, b = _reset_epoch(base_reset), _reset_epoch(cur_reset)
    if a is None or b is None:
        return base_reset == cur_reset
    return abs(a - b) <= WINDOW_MATCH_TOLERANCE_S


def rel_time(iso_ts):
    """Human 'in 1h 32m' from an ISO 8601 timestamp; '' if unparseable."""
    secs = seconds_until(iso_ts)
    if secs is None:
        return ""
    if secs <= 0:
        return "now"
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {mins}m"
    return f"in {mins}m"


def short_dur(secs):
    """Compact duration for burn display: '45m' or '3h05m'."""
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m"


# --------------------------------------------------------------------------- parsing (general)

def reset_passed(limits, fetched_at):
    """True if any limit reset AFTER the cached data was fetched, meaning the cached percent
    describes a window that has since rolled over.

    The `fetched_at` guard is essential: the endpoint can legitimately return a `resets_at`
    already in the past (an idle limit, server-side lag right after a boundary, or local clock
    skew). Invalidating on those would make every call refetch and spiral into the 429 backoff. We
    only invalidate when the reset moment falls after the fetch -- i.e. we hold pre-reset numbers."""
    for l in (limits or []):
        s = l.get("resets_in_seconds")
        if s is None or s > 0:
            continue
        reset_epoch = NOW + s  # absolute time the reset occurred
        if reset_epoch > fetched_at:
            return True
    return False


# --------------------------------------------------------------------------- burn rate

def limit_key(l):
    """Stable identity for burn/history matching. Uses the scoped model name when present, else a
    serialization of the raw scope, so two same-`kind` limits scoped differently (or two unknown
    kinds both lacking a model) don't collide and cross-contaminate each other's burn baseline."""
    sm = l.get("scope_model")
    if sm:
        return f"{l.get('kind')}|{sm}"
    scope = l.get("scope")
    if scope is not None:
        try:
            return f"{l.get('kind')}|{json.dumps(scope, sort_keys=True)}"
        except (TypeError, ValueError):
            return f"{l.get('kind')}|{scope!r}"
    return f"{l.get('kind')}|"


def compute_burn(limits, data_at):
    """Annotate each limit with a 'burn' delta representing the climb over roughly the last 15+
    minutes, not a session-long average.

    Baseline selection prefers the MOST RECENT same-window snapshot at least BURN_TARGET_WINDOW
    (900s / 15min) older than `data_at`, since a longer baseline gives a more stable rate reading;
    if none qualifies (e.g. the script has only run recently), it falls back to the most recent
    snapshot at least BURN_MIN_ELAPSED (120s) older. This intentionally favors recency over age --
    the opposite of picking the oldest snapshot in the history window, which would report a ~6h
    average instead of a recent climb.

    `data_at` is when the rendered percents were actually measured (which may be earlier than now,
    if served from cache). Measuring elapsed from data_at and only accepting baselines older than
    it stops a reading from being compared to the very snapshot it came from, which would fabricate
    a hard '+0%'. History entries are user-corruptible, so every field is type-checked."""
    usable = [h for h in HISTORY
              if isinstance(h, dict) and isinstance(h.get("limits"), dict)
              and is_num(h.get("at")) and h["at"] < data_at
              and (data_at - h["at"]) <= HISTORY_MAX_AGE]
    usable.sort(key=lambda x: x["at"], reverse=True)  # most recent first
    for l in limits:
        l["burn"] = None
        if not is_num(l.get("percent")):
            continue
        key = limit_key(l)
        cur_reset = l.get("resets_at")
        candidates = []  # (elapsed, delta), most-recent-first, for every snapshot passing guards
        for h in usable:
            elapsed = data_at - h["at"]
            if elapsed < BURN_MIN_ELAPSED:
                continue  # too close in time to be a meaningful baseline
            past = h["limits"].get(key)
            if not is_num(past):
                continue
            # Same-window guard: if the baseline belongs to a different reset window, a reset
            # boundary fell between the samples, so the delta understates the true burn (10% ->
            # reset -> 15% would read as a tame +5%). Compared with a tolerance because the endpoint
            # jitters the sub-second part of resets_at within one window (an exact match would drop
            # every valid baseline, leaving burn -- and pace -- perpetually null). Older caches with
            # no 'resets' map fall back to the negative-delta check below.
            base_reset = (h.get("resets") or {}).get(key)
            if not same_window(base_reset, cur_reset):
                continue
            delta = l["percent"] - past
            if delta < 0:
                continue  # a reset happened between samples; rate not meaningful
            candidates.append((elapsed, delta))
        if not candidates:
            continue
        # Prefer the most recent candidate that's at least 15min old (a stable "recent climb"
        # read); otherwise take the most recent candidate at least 2min old. `candidates` is
        # already most-recent-first because `usable` was sorted that way, so candidates[0] is
        # exactly that fallback.
        elapsed, delta = next((c for c in candidates if c[0] >= BURN_TARGET_WINDOW), candidates[0])
        l["burn"] = {"delta_percent": round(delta, 1), "over_seconds": int(elapsed)}


# --------------------------------------------------------------------------- posture

def _worst(a, b):
    return a if LEVELS.index(a) >= LEVELS.index(b) else b


def pace_info(l):
    """Return (ratio, exhaust_seconds) for a percent limit that is burning toward its reset, or
    (None, None) when pace cannot or should not be judged.

    ratio = actual_rate / sustainable_rate, where actual_rate is the recent burn (percent/sec)
    and sustainable_rate = remaining_percent / seconds_to_reset is the fastest you could spend and
    still land on empty exactly at reset. Algebraically this is also seconds_to_reset /
    seconds_to_exhaustion, so ratio > 1 means the current pace empties the window before it
    resets. exhaust_seconds is the projected wall-clock time to hit 100% at the current rate.

    Guards (each returns None, None): no burn baseline yet; non-percent meter; already full
    (exhaustion is handled as a terminal fact by limit_posture);
    reset in the past or unknown; reset further out than PACE_MAX_HORIZON_S (a short burst does not
    project meaningfully across a multi-day window); or a burn delta below PACE_MIN_BURN_DELTA,
    whose extrapolated rate is too noisy to steer on."""
    burn = l.get("burn")
    pct = l.get("percent")
    reset_s = l.get("resets_in_seconds")
    if not isinstance(burn, dict) or not is_num(pct) or not is_num(reset_s) or reset_s <= 0:
        return None, None
    if reset_s > PACE_MAX_HORIZON_S:
        return None, None  # too far out for a ~15-min burn to project across; percent axis governs
    delta = burn.get("delta_percent")
    over = burn.get("over_seconds")
    if not is_num(delta) or not is_num(over) or over <= 0 or delta < PACE_MIN_BURN_DELTA:
        return None, None
    remaining = 100 - pct
    if remaining <= 0:
        return None, None  # already full; limit_posture owns the terminal exhaustion fact
    actual_rate = delta / over               # percent per second, recent
    sustainable_rate = remaining / reset_s   # percent per second you can afford
    exhaust_seconds = remaining / actual_rate
    ratio = actual_rate / sustainable_rate   # == reset_s / exhaust_seconds
    return ratio, exhaust_seconds


def window_pace_info(l):
    """(ratio, headroom, exhaust_seconds), or (None, None, None) when the window cannot be located.

    Two readings of the same window, both sample-free. `ratio` (consumed/elapsed) says how you
    have spent SO FAR and is display data. `headroom` says what you can still afford FROM HERE --
    remaining budget over remaining time, in budget-pace units -- and it is what posture bands on,
    per WINDOW_HEADROOM_BANDS. exhaust_seconds is when this window hits 100% at the average rate.

    Unlike pace_info this takes no sample and keeps no history, so it has no noise to guard against
    and is defined at every instant of every window, including at 100% consumed (headroom 0) and
    seconds before a reset (headroom large) -- the two cases that used to need special handling.

    Nulls only for: an unknown kind (no window length to express the rate in), a missing percent,
    or reset data that contradicts the window length -- clock skew, or the provider redefining the
    window, either of which makes the constant wrong and judging on it worse than not judging.
    """
    pct = l.get("percent")
    reset_s = l.get("resets_in_seconds")
    length = WINDOW_LENGTHS.get(l.get("kind"))
    if not is_num(pct) or not is_num(reset_s) or not length:
        return None, None, None
    if reset_s <= 0 or reset_s > length:
        return None, None, None
    elapsed = length - reset_s
    elapsed_fraction = elapsed / length
    if elapsed_fraction <= 0:
        return None, None, None  # window just flipped; nothing elapsed to judge a rate over
    consumed = max(0.0, min(1.0, pct / 100.0))
    ratio = consumed / elapsed_fraction
    headroom = (1.0 - consumed) / (1.0 - elapsed_fraction) if elapsed_fraction < 1.0 else 0.0
    exhaust_seconds = ((1.0 - consumed) * (elapsed / consumed)) if consumed > 0 else None
    return ratio, headroom, exhaust_seconds


def _band(ratio):
    """Rung for a pace ratio. Escalation starts at 1.0 -- below that the window survives."""
    for thresh, level in PACE_BANDS:  # high-to-low; first threshold at/below ratio wins
        if ratio >= thresh:
            return level
    return "normal"


def pace_posture(l):
    """(rung, reason) from the short-horizon burn sample.

    The v2 absolute-runway backstop (floor at 'conserve' inside 30 minutes of projected
    exhaustion) is gone. v3 replaces it with two things that do a better job of the same
    invariant: `imminent` + the checkpoint discipline it names (a window about to run dry gets
    finished-and-persisted, not throttled -- see IMMINENT_RUNWAY_S), and the downtime cap in
    limit_posture, which can only LOWER a rung -- unlike the old backstop, it never raises one.
    """
    ratio, exhaust_seconds = pace_info(l)
    if ratio is None:
        return "normal", None  # window pace governs; see limit_posture
    p = _band(ratio)
    if p == "normal":
        return p, "burn pace %.2fx sustainable" % ratio
    return p, "burning %.2fx sustainable, empties %s before reset" % (
        ratio, short_dur(max(0, (l.get("resets_in_seconds") or 0) - (exhaust_seconds or 0))))


def _headroom_band(headroom):
    """Rung for a headroom figure. Escalation runs DOWNWARD: the bands trip as headroom falls."""
    for thresh, level in WINDOW_HEADROOM_BANDS:  # low-to-high; first threshold above h wins
        if headroom < thresh:
            return level
    return "normal"


def window_pace_posture(l):
    """(rung, reason) from how much budget-pace headroom is left between now and reset."""
    ratio, headroom, _exhaust = window_pace_info(l)
    if headroom is None:
        kind = l.get("kind")
        length = WINDOW_LENGTHS.get(kind)
        reset_s = l.get("resets_in_seconds")
        if not is_num(l.get("percent")):
            why = "no percent to judge"
        elif not length:
            why = "window length unknown for kind %r" % kind
        elif not is_num(reset_s) or reset_s <= 0:
            why = "reset missing or non-positive"
        elif is_num(reset_s) and reset_s > length:
            why = "reset outside window length"
        else:
            why = "window just reset"
        return "normal", "window pace not judged (%s)" % why
    p = _headroom_band(headroom)
    pct = l.get("percent") or 0
    reset_s = l.get("resets_in_seconds")
    runway = (" for the %s left" % short_dur(reset_s)) if is_num(reset_s) else ""
    if p == "normal":
        return p, "%.0f%% spent, %.2fx budget pace still affordable%s" % (pct, headroom, runway)
    return p, "%.0f%% spent: only %.2fx budget pace affordable%s (spent %.2fx budget so far)" % (
        pct, headroom, runway, ratio)


# --- Downtime cap: weighting escalation by the COST of exhausting a limit -------------------
#
# v2 banded purely on rate (pace ratio / headroom). v3 layers one structural idea on top: a
# limit's escalation should be weighted by the cost of exhausting it, and that cost is the
# DOWNTIME you'd actually sit through -- D = resets_in_seconds - exhaust_effective. A session
# limit that empties 40 minutes before its own reset costs 40 minutes of downtime; a weekly limit
# that empties on day 4 of 7 costs three days. Banding on D turns the session/weekly asymmetry (a
# session barely throttles, a weekly paces hard, for the IDENTICAL pace ratio) into derived
# arithmetic instead of a per-kind rule, and it covers any future unknown `kind` for free -- no
# window length needed, only a burn sample and a reset time.
#
# exhaust_effective is the EARLIEST of the two axes' own exhaust_seconds (pace_info's burst
# reading, window_pace_info's whole-window reading), each adjusted by a kind-dependent margin
# applied to the projected RATE, not to the raw seconds:
#
#     exhaust_adjusted = exhaust_raw / margin
#     margin = MARGIN_SESSION_RATE (0.85) for kind == "session", else MARGIN_POOL_RATE (1.15)
#
# Algebra, so the division (rather than multiplication) by margin is verified rather than assumed:
# adjusted_rate = raw_rate * margin, and exhaust = remaining / rate, so
#
#     exhaust_adjusted = remaining / (raw_rate * margin) = (remaining / raw_rate) / margin
#                       = exhaust_raw / margin
#
# MARGIN_SESSION_RATE < 1 divides by less than one, i.e. multiplies exhaust_raw by roughly 1.18,
# pushing the projection LATER (optimistic: a session is use-it-or-lose-it, so a rosier read
# favors letting it run rather than throttling budget that was about to expire anyway).
# MARGIN_POOL_RATE > 1 divides by more than one, pulling the projection EARLIER (pessimistic: a
# weekly overshoot costs days, so the margin leans toward catching it sooner). Unknown kinds get
# the pool margin -- nothing is known about their shape, so the pessimistic assumption is the
# safe default, and it is also what gives an unknown kind any downtime governance at all (window
# pace has no length to judge it by; burn pace alone, capped pessimistically, does).
MARGIN_SESSION_RATE = 0.85
MARGIN_POOL_RATE = 1.15

# D = max(0, resets_in_seconds - exhaust_effective). The table bands D itself, ascending (unlike
# PACE_BANDS/WINDOW_HEADROOM_BANDS, which band a RATE high-to-low or low-to-high): under 15
# minutes of downtime costs nothing extra (normal), under 1h -> measured, under 6h -> frugal,
# under 1 day -> conserve; a full day or more of downtime is left uncapped (None: the provisional
# rung, from pace alone, stands as-is).
DOWNTIME_CAP_BANDS = [(900, "normal"), (3600, "measured"), (21600, "frugal"), (86400, "conserve")]
# Except for terminal exhaustion, the final per-limit rung is min(provisional, cap) by LEVELS
# index -- the cap can only LOWER a rung, never raise one. A cheap wall is a reason to relax,
# never a reason to invent urgency pace didn't already find.
#
# The derived asymmetry this produces: a session window is at most 5h (18000s) long, so
# resets_in_seconds never exceeds that, which puts D's ceiling below the 21600s frugal/conserve
# boundary -- a session's CAP can structurally never exceed 'frugal', no matter how hard the
# provisional rung escalates. Terminal exhaustion bypasses that cap. A weekly window's reset can
# be up to 7 days out, so its D has no
# such ceiling and can reach any cap, including none at all. The session/weekly split used to
# need hand-tuned per-kind treatment (see the 2026-08-11 incident referenced at LEVELS above);
# here it falls out of one formula and the physical length of each window.


def _downtime_cap_band(D):
    """Cap rung for a downtime-cost figure in seconds. Ascending thresholds (D is a duration, read
    left to right as it grows) -- None when D lands beyond every band, meaning a day or more of
    downtime, exactly the case where the provisional rung should be trusted as-is."""
    for thresh, level in DOWNTIME_CAP_BANDS:
        if D < thresh:
            return level
    return None


def downtime_info(l):
    """(exhaust_effective, D, cap) for one limit -- the v3 downtime layer, computed independently
    of and after the provisional pace/window-pace rungs (which this feeds into, capping them, in
    limit_posture) and rendered again verbatim as the `downtime`/`runway_seconds`/`imminent`
    fields in render(). Recomputing pace_info/window_pace_info here rather than threading their
    results through is deliberate: it matches the rest of this file (render() and limit_posture
    already each call them independently), keeps this function usable standalone, and the
    computation is cheap dict arithmetic with no I/O.

    Returns (None, None, None) when there is no exhaustion projection on either axis (nothing to
    cap against) or `resets_in_seconds` is missing/non-positive (no window to be capped against).
    """
    reset_s = l.get("resets_in_seconds")
    if not is_num(reset_s) or reset_s <= 0:
        return None, None, None
    _, burn_exhaust = pace_info(l)
    _, _, win_exhaust = window_pace_info(l)
    margin = MARGIN_SESSION_RATE if l.get("kind") == "session" else MARGIN_POOL_RATE
    # is_num, not truthiness: window_pace_info's exhaust_seconds is legitimately 0.0 at 100%
    # consumed (already empty), and a falsy-style filter would silently drop that real, load-
    # bearing projection (the "fully exhausted midway through the window" case).
    candidates = [e / margin for e in (burn_exhaust, win_exhaust) if is_num(e)]
    if not candidates:
        return None, None, None
    exhaust_effective = min(candidates)
    D = max(0.0, reset_s - exhaust_effective)
    return exhaust_effective, D, _downtime_cap_band(D)


# --- Surplus: underspend detection for use-it-or-lose-it pool budgets ------------------------
#
# Pool budgets (weekly_all, weekly_scoped) expire unspent at reset -- there is no rollover -- so
# spending too SLOWLY is also a mistake, just one the pace/downtime machinery above has no reason
# to flag (it only ever escalates). `surplus` is the mirror signal: once past the midpoint of the
# window, project where this limit lands at reset if the current average rate holds, and flag it
# when that projection is comfortably under full. `session` is excluded on purpose -- there is no
# rollover to protect there either, but the session window resets every five hours regardless of
# use, so under-spending it costs nothing worth tracking; this signal exists for the multi-day
# pools where the wasted budget is real and the reset is far enough out to still act on it.
SURPLUS_MIN_ELAPSED_FRACTION = 0.5   # don't project off a small, noisy early slice of the week
SURPLUS_PROJECTED_FINAL_MAX = 75.0   # comfortably under full; a tighter trajectory isn't a surplus


def surplus_info(l):
    """{"projected_final_percent", "unspent_percent"} or None. Pool kinds only (not `session`, not
    an unknown kind -- both lack the rollover problem this signal protects against). Reuses
    window_pace_info's own elapsed_fraction/consumed arithmetic so this reading sits on exactly
    the same window math as `window_pace`, not a second, possibly-inconsistent formula.

    `projected_final_percent = min(100, percent / elapsed_fraction)`: where this limit lands at
    reset if the average rate so far holds, capped at 100 (it cannot land above what's spent).
    `unspent_percent = 100 - projected_final_percent`: how much of the budget is PROJECTED to
    still be unspent at reset if nothing changes -- the actionable number. Not what's unspent
    right now (`100 - percent`, already in the JSON, and not on its own informative: plenty of
    budget is unspent an hour into a fresh week, and that is not a surplus, it's normal).

    Never affects posture, any rung, or `posture_driver` -- this is purely a positive signal
    (spend more), and posture is exclusively an escalation signal (spend less); folding the two
    into one field would make a single number mean opposite things depending on its sign.
    """
    kind = l.get("kind")
    if kind not in ("weekly_all", "weekly_scoped"):
        return None
    pct = l.get("percent")
    reset_s = l.get("resets_in_seconds")
    length = WINDOW_LENGTHS.get(kind)
    if not is_num(pct) or not is_num(reset_s) or not length or reset_s <= 0 or reset_s > length:
        return None
    elapsed_fraction = (length - reset_s) / length
    if elapsed_fraction < SURPLUS_MIN_ELAPSED_FRACTION:
        return None  # also guards elapsed_fraction == 0 (a just-flipped window) against /0 below
    projected_final = min(100.0, pct / elapsed_fraction)
    if projected_final >= SURPLUS_PROJECTED_FINAL_MAX:
        return None
    return {
        "projected_final_percent": round(projected_final, 1),
        "unspent_percent": round(100.0 - projected_final, 1),
    }


def limit_posture(l):
    """(rung, reason) for one limit, from PACE and (v3) the downtime cap.

    Two pace readings, worst wins, because they see different failures. Burn pace catches a fresh
    burst near reset that the window average washes out. Window pace catches sustained
    overconsumption days out, where burn pace is null by construction (PACE_MAX_HORIZON_S). That
    worst-of-two-pace-readings rung is the PROVISIONAL rung; the downtime cap (see the "Downtime
    cap" section above) can then only LOWER it, never raise it, when exhausting the limit would
    cost little wall-clock downtime before the window resets anyway.

    Deliberately absent, all removed 2026-08-11:
      - the percent axis, per the rule at the top of this file. Terminal exhaustion is the narrow
        exception: displayed 100% is a fact that the limit has stopped, not a fullness band;
      - provider `severity`. It tracked fullness rather than sustainability and is discarded by
        parse_limits, so it cannot leak into posture or downstream display color;
      - the session-only near-reset relief. It existed to soften a high PERCENT shortly before
        reset; with percent gone it has nothing to relieve, and a kind-specific carve-out in a
        pace-only model is dead weight.
    """
    if is_exhausted(l):
        pct = l.get("percent")
        reset_s = l.get("resets_in_seconds")
        reason = "%.0f%% spent: limit exhausted" % pct
        if is_num(reset_s) and reset_s > 0:
            reason += ", resets in %s" % short_dur(reset_s)
        return "wind_down", reason

    p_burn, r_burn = pace_posture(l)
    p_win, r_win = window_pace_posture(l)
    p = _worst(p_burn, p_win)
    # Report whichever reading actually set the rung; window pace wins ties because it is the one
    # defined at every moment, so it is the more informative thing to print.
    reason = r_win if LEVELS.index(p_win) >= LEVELS.index(p_burn) else r_burn
    # v3 downtime cap: only ever lowers `p`, and only when it does gets a suffix explaining why --
    # see downtime_info's docstring and the "Downtime cap" section above for the D/margin algebra.
    _, D, cap = downtime_info(l)
    if cap is not None and LEVELS.index(cap) < LEVELS.index(p):
        # `reason` is always truthy here: it's r_win (window_pace_posture always returns a
        # non-empty reason string) unless burn strictly outranks window, in which case p_burn is
        # non-normal, which only happens when pace_posture ran the ratio-is-not-None branch --
        # which also always returns a non-empty reason. No "or use suffix alone" fallback needed.
        reason = "%s; capped at %s: exhaustion at current pace would cost only ~%s of downtime " \
                 "before reset" % (reason, cap, short_dur(D))
        p = cap
    return p, reason


def _pace_rank(l):
    """How close this limit is to escalating, for picking a driver among equals."""
    if is_exhausted(l):
        return float("inf")
    ratios = [r for r in (window_pace_info(l)[0], pace_info(l)[0]) if r is not None]
    return max(ratios) if ratios else -1.0


def overall_posture(limits):
    """Worst per-limit posture wins; return (posture, driving_limit_or_None, reason).

    A driver (and a reason) is returned even at `normal`: "why is it normal" is exactly the
    question nobody could answer on 2026-08-11, and it costs one line to say. Ties at the same
    rung go to whichever limit is closest to escalating, so the reason names the meter actually
    worth watching rather than whichever happened to be listed first.
    """
    worst, driver, why = "normal", None, None
    for l in limits:
        p, reason = limit_posture(l)
        better = LEVELS.index(p) > LEVELS.index(worst)
        tie = (driver is None) or (LEVELS.index(p) == LEVELS.index(worst)
                                   and _pace_rank(l) > _pace_rank(driver))
        if better or tie:
            worst, driver, why = p, l, reason
    return worst, driver, (("%s: %s" % (label(driver), why)) if driver and why else why)


# --------------------------------------------------------------------------- posture hysteresis
#
# The per-run posture above (post-cap, but otherwise a fresh computation with no memory of the
# previous run) can flap: a burn sample that wobbles across a band edge, or two axes trading off
# which one is worst, would otherwise change the reported posture every few minutes even though
# nothing about the account materially changed. v3 smooths that with asymmetric hysteresis on the
# TOP-LEVEL posture only (never on a per-limit rung, and never on the downtime cap): escalate
# instantly (a real problem should never be hidden behind a confirmation delay), de-escalate only
# after the computed rung has stayed lower for HYSTERESIS_CONFIRM_S -- a genuine improvement is
# worth a short wait to confirm, a genuine problem is not.
#
# State lives in the existing v1 cache under the additive `posture_state` key, so the cache schema
# itself does not change (see load_cache's schema-version gate). Persistence follows the same
# read-before-write, atomic-replace pattern the 429 path uses (see Test429ClobberPreservation):
# read once to decide, re-read immediately before writing so a concurrently-written last_429_at or
# history from another process is never clobbered -- only `posture_state` is mutated either way.
HYSTERESIS_MAX_AGE_S = 1800   # a run this far apart from the last one can't trust a stale hold
HYSTERESIS_CONFIRM_S = 120    # how long a de-escalation must persist before it's believed


def _valid_posture_state(s):
    """Defensive parse of the cached `posture_state` -- user/process-corruptible like every other
    cache field this script reads back. Returns a normalized dict, or None for anything not
    shaped as this version wrote it, which the caller treats identically to 'no stored state'."""
    if not isinstance(s, dict):
        return None
    posture = s.get("posture")
    at = s.get("at")
    if posture not in LEVELS or not is_num(at):
        return None
    driver_resets_at = s.get("driver_resets_at")
    if driver_resets_at is not None and not is_num(driver_resets_at):
        driver_resets_at = None
    pending = s.get("pending_downgrade")
    if pending is not None:
        if (not isinstance(pending, dict) or pending.get("posture") not in LEVELS
                or not is_num(pending.get("first_at"))):
            pending = None
    return {"posture": posture, "at": at, "driver_resets_at": driver_resets_at,
            "pending_downgrade": pending}


def hysteresis_posture(computed, driver, persist=True):
    """(posture, note) -- `computed` (this run's raw, post-cap overall_posture result) smoothed by
    the stored `posture_state`. `note` is a reason-string suffix to append to `posture_reason`
    when the returned posture differs from `computed` (holding a stale-but-unconfirmed rung),
    else None. `driver` is overall_posture's driving limit (or None), used to record the reset
    time of the window that's currently driving the held posture.

    All IO is best-effort: load_cache()/save_cache() already fail soft (an empty dict on read
    failure, a silent no-op on write failure -- see their own docstrings). An unreadable cache
    falls back to `computed` with no hold, same as "no stored state" -- read failure and read
    absence are indistinguishable to this function by construction. An unwritable cache is not
    the same case: this run's decision was already made from whatever the read returned, so a
    failed write afterward changes nothing about what THIS run reports; it only means the held
    state doesn't persist to disk, silently, and the NEXT run's read recovers on its own (finding
    the prior good state, or none) -- never an exception either way.
    """
    driver_resets_at = _reset_epoch(driver.get("resets_at")) if driver else None
    now = NOW

    def _persist(state):
        if not persist:
            return
        fresh = load_cache()
        if not isinstance(fresh, dict):
            fresh = {}
        fresh["posture_state"] = state
        save_cache(fresh)

    cache = load_cache()
    stored = _valid_posture_state(cache.get("posture_state")) if isinstance(cache, dict) else None

    # No stored state, unreadable/malformed state, or a stale hold (gap since the last run too
    # large to trust) -- adopt computed outright.
    if stored is None or (now - stored["at"]) > HYSTERESIS_MAX_AGE_S:
        _persist({"posture": computed, "at": now, "driver_resets_at": driver_resets_at,
                  "pending_downgrade": None})
        return computed, None

    # The window that drove the HELD posture has itself reset since -- whatever was true about it
    # no longer applies, so there is nothing left to hold.
    if stored["driver_resets_at"] is not None and now >= stored["driver_resets_at"]:
        _persist({"posture": computed, "at": now, "driver_resets_at": driver_resets_at,
                  "pending_downgrade": None})
        return computed, None

    stored_rank = LEVELS.index(stored["posture"])
    computed_rank = LEVELS.index(computed)

    if computed_rank >= stored_rank:
        # Escalation (>) adopts immediately -- never hidden behind a delay. A tie (==) is the
        # identical posture string either way (LEVELS indices are unique). Both clear any pending
        # downgrade: reaching or exceeding the held rung ends the confirmation window.
        _persist({"posture": computed, "at": now, "driver_resets_at": driver_resets_at,
                  "pending_downgrade": None})
        return computed, None

    # De-escalation candidate: hold the stored (higher) rung until HYSTERESIS_CONFIRM_S has
    # elapsed since the FIRST run that computed below it. Which specific lower rung each
    # subsequent run computes does not reset the clock -- only a run that recovers to >= the
    # stored rank (handled above) clears `pending_downgrade` early.
    pending = stored["pending_downgrade"]
    first_at = pending["first_at"] if pending else now
    if (now - first_at) >= HYSTERESIS_CONFIRM_S:
        _persist({"posture": computed, "at": now, "driver_resets_at": driver_resets_at,
                  "pending_downgrade": None})
        return computed, None
    _persist({"posture": stored["posture"], "at": now,
              "driver_resets_at": stored["driver_resets_at"],
              "pending_downgrade": {"posture": computed, "first_at": first_at}})
    note = "holding %s pending confirmation (computed %s just now)" % (stored["posture"], computed)
    return stored["posture"], note


# --------------------------------------------------------------------------- render

def label(l):
    if l["kind"] == "session":
        return "Session (5h)"
    if l["kind"] == "weekly_all":
        return "Weekly (all models)"
    if l["kind"] == "weekly_scoped":
        return f"Weekly ({l['scope_model'] or 'scoped'})"
    # Unknown/future kind: show whatever the endpoint called it, plus any scope.
    base = str(l["kind"] or "unknown limit")
    return f"{base} ({l['scope_model']})" if l.get("scope_model") else base


def render(limits, age_secs, stale, source, next_check_seconds=None, account=None):
    # Clamp before any use: a backwards clock jump gives a negative age, which must never print as
    # "fetched -5s ago" nor push compute_burn's data_at (NOW - age_secs) into the future, which
    # would inflate every baseline's elapsed and silently drop valid ones.
    age_secs = max(0, age_secs)
    compute_burn(limits, NOW - age_secs)
    # Annotate each limit with its pace reading (ratio + projected time-to-exhaustion) so it rides
    # along in the JSON and can be shown to the user. Depends on burn, so it runs after it.
    for l in limits:
        ratio, exhaust_seconds = pace_info(l)
        l["pace"] = ({"ratio": round(ratio, 2), "exhaust_seconds": int(exhaust_seconds)}
                     if ratio is not None else None)
        wratio, wheadroom, wexhaust = window_pace_info(l)
        l["window_pace"] = ({"ratio": round(wratio, 2),
                             "headroom": round(wheadroom, 2),
                             "exhaust_seconds": int(wexhaust) if wexhaust is not None else None}
                            if wratio is not None else None)
        # v3: downtime-cap transparency (the D/cap that already shaped this limit's rung inside
        # limit_posture, via overall_posture below) plus runway/imminent and surplus. Field order
        # per spec: appended after window_pace, in this build order.
        exhaust_eff, D, cap = downtime_info(l)
        l["downtime"] = {"seconds": int(round(D)), "cap": cap} if exhaust_eff is not None else None
        runway = int(round(exhaust_eff)) if exhaust_eff is not None else None
        l["runway_seconds"] = runway
        reset_s = l.get("resets_in_seconds")
        # Band the same ROUNDED value that gets emitted as `runway_seconds`, not the raw float --
        # otherwise e.g. a raw 1799.6s rounds to a displayed 1800 while `imminent` still bands the
        # unrounded 1799.6 (< 1800) as true, so the JSON would show runway_seconds: 1800 next to
        # imminent: true for a runway that reads as exactly the threshold. Self-consistency with
        # the field actually printed matters more here than a sub-second of banding precision.
        l["imminent"] = bool(runway is not None and runway < IMMINENT_RUNWAY_S
                             and is_num(reset_s) and runway < reset_s)
        l["surplus"] = surplus_info(l)
        limit_rung, _ = limit_posture(l)
        pace_judgeable = l["pace"] is not None or l["window_pace"] is not None
        l["posture"] = limit_rung
        l["pace_judgeable"] = pace_judgeable
        if not is_num(l.get("percent")):
            l["color"] = "gray"
        elif is_exhausted(l):
            l["color"] = POSTURE_COLORS["wind_down"]
        elif not pace_judgeable:
            l["color"] = "gray"
        else:
            l["color"] = POSTURE_COLORS[limit_rung]
    posture_computed, driver, posture_reason = overall_posture(limits)
    # v3: smooth the top-level posture with hysteresis (escalate instantly, de-escalate only after
    # confirmation -- see hysteresis_posture's docstring). Applies on every render() path: fresh
    # cache, network success, and every stale/backoff fallback, since they all funnel through here.
    posture, hyst_note = hysteresis_posture(posture_computed, driver, persist=not CACHE_ONLY)
    if hyst_note:
        posture_reason = ("%s; %s" % (posture_reason, hyst_note)) if posture_reason else hyst_note
    if not JSON_ONLY:
        age_str = (f"{int(age_secs // 60)}m {int(age_secs % 60)}s ago"
                   if age_secs >= 60 else f"{int(age_secs)}s ago")
        stale_note = " [STALE: showing last known data]" if stale else ""
        print(f"Claude usage (fetched {age_str}, source: {source}){stale_note}")
        for l in limits:
            pct = f"{round(l['percent'])}%" if is_num(l["percent"]) else "?"
            reset = rel_time(l.get("resets_at") or "")
            reset_str = f"  resets {reset} ({l['resets_at']})" if reset else ""
            flags = []
            if l.get("is_active"):
                flags.append("active")
            if l.get("resets_in_seconds") is not None and l["resets_in_seconds"] <= 0:
                flags.append("reset-elapsed")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            burn = l.get("burn")
            burn_str = (f"  +{burn['delta_percent']}% / {short_dur(burn['over_seconds'])}"
                        if burn else "")
            pace = l.get("pace")
            pace_str = (f"  pace {pace['ratio']}x (empties in {short_dur(pace['exhaust_seconds'])})"
                        if pace else "")
            # Show `extra` only when there is no percent to show -- i.e. this is a non-percent
            # meter (e.g. a usage-credit balance) that would otherwise render as a bare "?". For
            # ordinary percent limits `extra` stays in the JSON but out of the human line.
            extra = l.get("extra")
            extra_str = ""
            if extra and not is_num(l["percent"]):
                extra_str = "  {" + ", ".join(f"{k}={v}" for k, v in extra.items()) + "}"
            print(f"  {label(l):<24} {pct:>4}{reset_str}{flag_str}{burn_str}{pace_str}{extra_str}")
        if driver and is_num(driver.get("percent")):
            driver_str = f" (driven by {label(driver)} at {round(driver['percent'])}%)"
        elif driver:
            driver_str = f" (driven by {label(driver)})"
        else:
            driver_str = ""
        print(f"  posture: {posture}{driver_str}")
        if posture_reason:
            print(f"  why: {posture_reason}")
        for l in limits:
            if l.get("imminent"):
                print(f"  ⚠ {label(l)} exhausts in ~{short_dur(l['runway_seconds'])}, "
                      f"reset in ~{short_dur(l['resets_in_seconds'])} — checkpoint now")
            surplus = l.get("surplus")
            if surplus:
                print(f"  surplus: {label(l)} projected to land at "
                      f"{surplus['projected_final_percent']}% "
                      f"(~{surplus['unspent_percent']}% may go unspent)")
        print("---")
    print(json.dumps({
        "available": True,
        "stale": stale,
        "source": source,
        "age_seconds": int(age_secs),
        # Seconds until the NEXT allowed live-fetch attempt, when the caller
        # is showing stale data because of the 429 backoff specifically (None
        # for every other path, including other stale reasons like missing
        # credentials, where there is no scheduled retry to name). This was
        # previously only readable by parsing the "%ds left" the `source`
        # string embeds for humans -- a caller wanting "next check in Xm" had
        # to regex a prose string never meant to be parsed. This is that same
        # number as a real field.
        "next_check_seconds": next_check_seconds,
        "posture": posture,
        # This run's raw posture before hysteresis (v3) -- differs from `posture` only while a
        # de-escalation is held pending confirmation; see hysteresis_posture.
        "posture_computed": posture_computed,
        # Why posture is what it is, in one line. Percent still appears here and on every limit
        # because a human reads it; it just no longer decides anything.
        "posture_reason": posture_reason,
        "posture_driver": ({
            "kind": driver["kind"],
            "label": label(driver),
            "percent": driver["percent"],
        } if driver else None),
        # Resolved from the same OAuth token as these limits and cached atomically beside them.
        # Consumers must bind the UUIDs to their own account registry before displaying it.
        "account": account if isinstance(account, dict) else None,
        "limits": limits,
    }))
    sys.exit(0)


# --------------------------------------------------------------------------- main

def main():
    global HISTORY
    parse_args(sys.argv[1:])
    cache = load_cache()
    HISTORY = cache.get("history", [])
    if not isinstance(HISTORY, list):
        HISTORY = []
    cached_data = cache.get("data")
    cached_account = cache.get("account") if isinstance(cache.get("account"), dict) else None
    fetched_at = cache.get("fetched_at", 0)
    cache_age = NOW - fetched_at if fetched_at else None
    cached_limits = parse_limits(cached_data) if cached_data else None

    # This branch is deliberately before every credential lookup and provider request.
    # Keeping cache-only as an early terminal path makes its no-network property structural,
    # rather than dependent on a flag being threaded through credential/provider functions.
    if CACHE_ONLY:
        if not cached_limits or cache_age is None:
            fail("no cached usage data", "run a live usage check while this account is active")
        stale = cache_age >= TTL or reset_passed(cached_limits, fetched_at)
        render(cached_limits, cache_age, stale=stale, source="cache-only", account=cached_account)

    # 1. Fresh cache: serve it, no network call -- unless a limit has passed its reset, in which
    #    case the cached percent is for a window that no longer exists, so we fetch fresh.
    if (not FORCE_FRESH and cached_limits and cache_age is not None
            and cache_age < TTL and not reset_passed(cached_limits, fetched_at)):
        render(cached_limits, cache_age, stale=False, source="cache", account=cached_account)

    # 2. Recently rate-limited: don't retry yet, serve stale if we have it.
    #
    # The wait is the SERVER's `Retry-After` when it sent one, never our own
    # guess alone. This used to be a flat BACKOFF (900s), and that flat guess
    # created an inescapable lockout: the endpoint answered a 429 with
    # `Retry-After: 1772`, we retried at 900s, that early retry was refused --
    # and the refusal rewrote `last_429_at = NOW`, restarting our clock. Every
    # cycle retried too early and re-armed itself, so the meter could never
    # recover on its own. Observed doing exactly that for over two and a half
    # hours (2026-08-12).
    #
    # max() rather than the raw header: if the server asks for less than our
    # own floor we still wait the floor, which only costs staleness. The cap
    # stops a malformed or hostile header from parking the meter indefinitely.
    last_429 = cache.get("last_429_at", 0)
    retry_after = cache.get("retry_after_s")
    if isinstance(retry_after, (int, float)) and 0 < retry_after <= RETRY_AFTER_CAP:
        wait = max(BACKOFF, int(retry_after) + RETRY_AFTER_MARGIN)
    else:
        wait = BACKOFF
    if last_429 and (NOW - last_429) < wait:
        left = int(wait - (NOW - last_429))
        cause_label, cause_detail = _backoff_cause(cache)
        if cached_limits:
            render(cached_limits, cache_age or 0, stale=True,
                   source="cache (%s, %ds left)" % (cause_label, left),
                   next_check_seconds=left, account=cached_account)
        fail("%s, and no cached data" % cause_detail,
             f"retry after {left}s")

    # 3. Cache miss (or reset boundary crossed): fetch.
    token = get_access_token()
    if not token:
        if cached_limits:
            render(cached_limits, cache_age or 0, stale=True, source="cache (no credentials)",
                   account=cached_account)
        fail("no OAuth credentials found",
             "set CLAUDE_CODE_OAUTH_TOKEN, or ensure the macOS Keychain item "
             "'Claude Code-credentials' or <CLAUDE_CONFIG_DIR|~/.claude>/.credentials.json exists")

    status, data = fetch(token)

    if status == 200 and data is not None:
        limits = parse_limits(data)
        if limits:
            token_fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
            cached_account_fetched_at = cache.get("account_fetched_at")
            account_binding_matches = (
                cached_account is not None
                and cache.get("credential_fingerprint") == token_fingerprint)
            account_cache_fresh = (
                account_binding_matches
                and is_num(cached_account_fetched_at)
                and 0 <= NOW - cached_account_fetched_at < PROFILE_TTL)
            profile_failed_at = cache.get("profile_failed_at")
            profile_retry_after = cache.get("profile_retry_after_s")
            profile_wait = (
                max(BACKOFF, int(profile_retry_after) + RETRY_AFTER_MARGIN)
                if is_num(profile_retry_after) and 0 < profile_retry_after <= RETRY_AFTER_CAP
                else BACKOFF)
            profile_backed_off = (
                is_num(profile_failed_at)
                and 0 <= NOW - profile_failed_at < profile_wait)
            if account_cache_fresh:
                resolved_account = cached_account
                account_status = 200
            elif (token.startswith("sk-ant-") and len(token) >= 24
                  and not profile_backed_off):
                account_status, resolved_account = fetch_account(token)
            else:
                account_status, resolved_account = None, None
            if resolved_account is None and account_binding_matches:
                resolved_account = cached_account
            # Append this snapshot to history so future runs can compute burn rate. 'resets'
            # records each limit's reset time so burn can tell whether a reset boundary fell
            # between two samples (see compute_burn).
            snapshot = {
                "at": NOW,
                "limits": {limit_key(l): l["percent"]
                           for l in limits if is_num(l["percent"])},
                "resets": {limit_key(l): l.get("resets_at")
                           for l in limits if is_num(l["percent"])},
            }
            HISTORY = [h for h in HISTORY if isinstance(h, dict)
                       and is_num(h.get("at")) and (NOW - h["at"]) <= HISTORY_MAX_AGE]
            HISTORY.append(snapshot)
            HISTORY = HISTORY[-HISTORY_MAX:]
            # Read-modify-write, not a blind overwrite: render() -> hysteresis_posture() reads
            # this same cache file back immediately afterward to decide whether to hold a prior
            # posture, so a write here that dropped `posture_state` would erase the held state
            # before hysteresis ever saw it -- making hysteresis inert on every network refresh
            # (same re-read-before-write pattern as the 429 branch below and _persist above).
            fresh = load_cache()
            fresh["fetched_at"] = NOW
            fresh["data"] = data
            fresh["account"] = resolved_account
            fresh["credential_fingerprint"] = token_fingerprint
            if account_status == 200 and not account_cache_fresh and resolved_account is not None:
                fresh["account_fetched_at"] = NOW
                fresh["profile_failed_at"] = 0
                fresh["profile_retry_after_s"] = 0
                fresh["profile_status"] = None
            # A successful USAGE request always clears only the usage request's
            # backoff. Profile failures have their own keys above and must never
            # make fresh usage data look stale.
            fresh["last_429_at"] = 0
            # Clear the server's wait alongside the 429 marker: a successful
            # fetch means the window it described is over, and leaving it set
            # would make the NEXT 429 wait on a value the server sent for an
            # already-finished window.
            fresh["retry_after_s"] = 0
            fresh["history"] = HISTORY
            save_cache(fresh)
            render(limits, 0, stale=False, source="network", account=resolved_account)
        # 200 but unrecognizable shape: the private API may have changed.
        if cached_limits:
            render(cached_limits, cache_age or 0, stale=True,
                   source="cache (response shape changed)", account=cached_account)
        fail("endpoint returned 200 but the response shape was not recognized",
             "the private usage endpoint's response format may have changed")

    if status == 429:
        # Re-load from disk rather than mutating the `cache` dict loaded at the start of main():
        # another process may have completed a successful fetch (newer `data`/`history`) since we
        # loaded, and a naive read-modify-write here would clobber it.
        fresh = load_cache()
        fresh["last_429_at"] = NOW
        save_cache(fresh)
        if cached_limits:
            render(cached_limits, cache_age or 0, stale=True, source="cache (got 429)",
                   account=cached_account)
        fail("endpoint rate-limited (429) and no cached data",
             f"retry in ~{BACKOFF}s; the script will back off automatically")

    if status == 401:
        if cached_limits:
            render(cached_limits, cache_age or 0, stale=True, source="cache (token stale)",
                   account=cached_account)
        fail("OAuth token is stale (401)",
             "run any Claude Code command to refresh credentials, then retry")

    # Any other HTTP status (network errors with status=None excluded --
    # those get no backoff, since connectivity blips are not the endpoint
    # telling us anything). This used to arm no backoff at all: a 403 here
    # ("OAuth token does not meet scope requirement user:profile", observed
    # in production 2026-08-12/13) left `last_429_at` untouched, so the NEXT
    # poll tick (60s later, the server's normal interval) saw the SAME
    # already-elapsed backoff window and retried almost immediately --
    # repeating every 60s until a 429 finally arrived and armed the real
    # hour-long wait. Confirmed from the cache's own error history: a
    # recurring 403, 403 (+60s), 429 (+60s) pattern, once per backoff cycle,
    # for over 6 hours straight. Arming the same `last_429_at` here stops
    # that hammering: the wait becomes whichever is longer of the BACKOFF
    # floor or a still-on-record `retry_after_s` from an earlier 429 (a 403
    # carries no Retry-After header of its own, so it never sets one, only
    # reuses what's already there).
    if status is not None:
        fresh = load_cache()
        fresh["last_429_at"] = NOW
        save_cache(fresh)
    if cached_limits:
        render(cached_limits, cache_age or 0, stale=True,
               source=f"cache (fetch failed{': HTTP ' + str(status) if status else ''})",
               account=cached_account)
    fail(f"could not reach usage endpoint{' (HTTP ' + str(status) + ')' if status else ''}",
         "check network connectivity")


if __name__ == "__main__":
    main()
