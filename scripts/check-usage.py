#!/usr/bin/env python3
"""check-usage: report claude.ai subscription usage (session, weekly, per-model, and any
future limit kinds such as usage-credit meters).

Cross-platform (macOS, Windows, Linux), Python standard library only. Normally invoked through
the small OS launchers next to it (check-usage.sh on Unix, check-usage.cmd on Windows), but it can
be run directly with Python 3.9+.

Data source: the private endpoint GET https://api.anthropic.com/api/oauth/usage (the same backend
behind claude.ai /settings/usage and Claude Code /usage), plus GET
https://api.anthropic.com/api/oauth/profile for account/organization metadata (fetched only when
needed and cached separately). Both endpoints are on api.anthropic.com; the OAuth token is sent to
Anthropic only. The usage endpoint is aggressively rate-limited, so this script caches responses
and only hits the network on a cache miss.

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
                        that cost derives
  - runway_seconds, imminent : the same earliest projection per limit, plus a flag for "about to
                        run dry -- checkpoint now, don't throttle"
  - surplus           : {projected_final_percent, unspent_percent} -- underspend detection for a
                        pool budget on track to expire unspent
  - posture           : accelerate | cruise | coast | brake | park, from pace, the downtime
                        cap, then hysteresis-smoothed run to run
  - posture_computed  : this run's raw posture before hysteresis
  - posture_reason    : one line saying why posture is what it is

Design note on brittleness: the endpoint's set of limit "kinds" is NOT assumed to be fixed. Model
line-ups and billing change often (for example a model moving from a scoped weekly limit to
metered usage credits). Every limit the endpoint returns is passed through, unknown kinds
included, and any meter that is not a 0-100 percent is surfaced under `extra` rather than dropped,
so a new kind of budget never silently reads as "all clear".

Credentials (checked in order): the CLAUDE_CODE_OAUTH_TOKEN env var; the macOS Keychain item
"Claude Code-credentials" (account CLAUDE_CODE_KEYCHAIN_ACCOUNT or the current OS user);
<config>/.credentials.json where <config> is CLAUDE_CONFIG_DIR or ~/.claude; then a running
`claude` process's environment (read via `ps`, same-user only, macOS/Linux). The token is used
in-process for the provider requests and is never printed or logged.

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
VERSION = "2.0.0"


def _warn(where, exc):
    """Write a one-line diagnostic to stderr for a swallowed exception, never to stdout (which
    would corrupt --json output) and never including any credential value."""
    try:
        sys.stderr.write("check-usage: %s: %s: %s\n" % (where, type(exc).__name__, exc))
    except Exception:
        pass


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
# POSTURE REACTS TO PACE, NEVER TO PERCENT. Percent stays in the JSON because it is the number a
# human reads, but it is a display fact and never a decision input. Exhaustion is the narrow
# exception: a displayed 100% is a terminal fact, not a fullness band.
#
# What that rule protects against, concretely: a weekly limit sitting at 97% with 2 hours left
# before reset. Two hours is ~1.2% of a week, so 3% remaining is AHEAD of pace and nothing is
# difference, because the same 97% is fine on day 6.9 and alarming on day 3 of a 7-day window.
# Only the clock distinguishes them, which is why the axis has to be a rate.
LEVELS = ["accelerate", "cruise", "coast", "brake", "park"]
POSTURE_COLORS = {"accelerate": "blue", "cruise": "green", "coast": "yellow", "brake": "orange", "park": "red"}
POSTURE_INSTRUCTIONS = {
    "accelerate": "Increase useful work: revisit valuable deferred work, add useful parallelism, or use stronger models where they add value. Never invent work to burn quota.",
    "cruise": "Maintain the current approach, aiming to use the available quota through reset.",
    "coast": "Ease off new commitments and speculative extras; preserve the rigor of work already running.",
    "brake": "Actively reduce new commitments and parallelism; use cheaper models only where the task and required gates permit.",
    "park": "Finish and save current work, then admit no new work until capacity is available. Persist and hand off held work before waiting.",
}
# Cruise targets 90-100% of the currently affordable rate. These are admission
# bands, independent of the existing quota-ring and display-multiplier colors.
ACCELERATE_RATIO = 0.9
PARK_RUNWAY_S = 300
EXPANSION_MIN_ELAPSED = 0.05
EXPANSION_MIN_UNSPENT = 2.0
EXHAUSTED_PERCENT = 99.5  # render() rounds this and higher to the displayed value "100%"


def is_exhausted(l):
    """True exactly when this limit's displayed rounded percentage is 100% or more."""
    return is_num(l.get("percent")) and l["percent"] >= EXHAUSTED_PERCENT

# Admission uses consumption / the rate affordable until reset. Recent samples
# need a material change and a short enough forecast horizon. Window averages
# supply a fallback in the same units; headroom remains diagnostic only.
PACE_MIN_BURN_DELTA = 2.0
PACE_MAX_HORIZON_S = 21600
# Earlier checkpoint warning, independent of the five-minute Park reserve.
IMMINENT_RUNWAY_S = 1800
WINDOW_LENGTHS = {
    "session": 5 * 3600,
    "weekly_all": 7 * 24 * 3600,
    "weekly_scoped": 7 * 24 * 3600,
}

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
        # Preserve WHY the endpoint refused, rather than discarding the body and every header:
        # a 429 with a `Retry-After` header tells us exactly how long to wait, which is strictly
        # better information than falling back to our own fixed guess. Diagnostic capture itself
        # is wrapped so that a header/body-parsing failure here degrades to less diagnostic detail
        # rather than turning a handled HTTP error into an unhandled exception.
        detail = {"headers": {}, "body": None}
        try:
            detail["headers"] = {k.lower(): v for k, v in e.headers.items()}
        except Exception as exc:
            _warn("fetch: header capture", exc)
        try:
            raw = e.read().decode("utf-8", "replace")
            detail["body"] = raw[:2000] if raw else None
        except Exception as exc:
            _warn("fetch: body capture", exc)
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
        except Exception as exc:
            _warn("fetch_account: header capture", exc)
        try:
            raw = e.read().decode("utf-8", "replace")
            detail["body"] = raw[:2000] if raw else None
        except Exception as exc:
            _warn("fetch_account: body capture", exc)
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
    except Exception as exc:
        _warn("_record_http_error", exc)


def _backoff_cause(cache):
    """Describe what actually armed the backoff, rather than assuming a 429.

    `last_429_at` is armed on ANY HTTP status, not just 429 -- deliberately, so a recurring
    non-retryable error cannot hot-loop the endpoint. The cost of reusing that one field is that
    the wait is *reported* as a rate limit whatever actually caused it, which is why this function
    exists: it recovers the real cause (a 403 lacking the required OAuth scope, say) from the last
    recorded error so the reported reason names the actionable problem instead of always saying
    "rate limited".

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
    except Exception as exc:
        _warn("_backoff_cause: reading http_errors", exc)
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
    except (ValueError, AttributeError, TypeError):
        pass  # body is not the expected {"error": {"message": ...}} shape; message stays ""
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
                # Passed through as-is; the endpoint's own semantics for this field are not
                # publicly documented, so it is treated as informational, never load-bearing.
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
    the credentials file, then a running claude process's environment. Returns the token
    string or None. Never logs the value.

    `_process_env_token()` greps `pgrep -x claude` and takes whichever of up to 8 matching
    processes is found first -- on a machine running several concurrent sessions that is an
    arbitrary one, not necessarily the account the caller intends. It is kept last, as a
    fallback for when no stored credential is available, precisely because it can pick an
    unintended session's token."""
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok
    for raw in (_keychain_blob(), _file_blob()):
        if not raw:
            continue
        tok = _blob_access_token(raw)
        if tok:
            return tok
    return _process_env_token()


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
        return None, None  # too far out for a ~15-min burn to project across; window-average fallback governs
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

    Sample-free diagnostic readings: consumed/elapsed ratio, remaining headroom,
    and exhaustion at the whole-window average rate. Admission converts exhaustion
    to the same affordable-through-reset units as recent pace. Headroom remains a
    diagnostic field; it does not have a separate posture ladder.
    Unknown kinds, missing percentages, and unlocatable reset windows return nulls.
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
    """Admission direction around the affordable-through-reset rate, not quota fullness."""
    if ratio > 1.25:
        return "brake"
    if ratio > 1.0:
        return "coast"
    return "cruise"


def pace_posture(l):
    ratio, exhaust = pace_info(l)
    if ratio is None:
        return "cruise", None
    return _band(ratio), "recent consumption %.2fx the rate affordable through reset" % ratio


def window_pace_posture(l):
    ratio, headroom, exhaust = window_pace_info(l)
    if ratio is None:
        kind = l.get("kind")
        why = ("no percent to judge" if not is_num(l.get("percent")) else
               "window length unknown for kind %r" % kind if kind not in WINDOW_LENGTHS else
               "window just reset" if l.get("resets_in_seconds") == WINDOW_LENGTHS.get(kind) else
               "reset outside window length" if is_num(l.get("resets_in_seconds")) and l["resets_in_seconds"] > WINDOW_LENGTHS.get(kind, 0) else
               "reset missing or already passed")
        return "cruise", "window pace not judged (%s)" % why
    affordable_ratio = 0.0 if ratio == 0 else l["resets_in_seconds"] / exhaust if exhaust and exhaust > 0 else float("inf")
    return _band(affordable_ratio), "window-average consumption %.2fx the rate affordable through reset (%.2fx original budget pace remains affordable)" % (affordable_ratio, headroom)


def _admission_pace(l):
    """Use one rate/affordable-rate unit; prefer reliable recent evidence, then window average."""
    ratio, exhaust = pace_info(l)
    if ratio is not None:
        return ratio, exhaust, "recent"
    window_ratio, _, exhaust = window_pace_info(l)
    if window_ratio is None:
        return None, None, None
    if window_ratio == 0:
        return 0.0, None, "window_average"
    if exhaust is None or exhaust <= 0:
        return None, exhaust, "window_average"
    return l["resets_in_seconds"] / exhaust, exhaust, "window_average"


# --- Downtime cap: weighting escalation by the COST of exhausting a limit -------------------
#
# Banding purely on rate (pace ratio / headroom) treats a limit's escalation as independent of
# what exhausting it actually costs. This layer weights escalation by that cost, which is the
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

# A short projected interruption can soften Brake to Coast. It cannot turn
# overspending into permission to expand or override the Park reserve.
DOWNTIME_CAP_BANDS = [(3600, "coast"), (86400, "brake")]


def _downtime_cap_band(D):
    """Cap rung for a downtime-cost figure in seconds. Ascending thresholds (D is a duration, read
    left to right as it grows) -- None when D lands beyond every band, meaning a day or more of
    downtime, exactly the case where the provisional rung should be trusted as-is."""
    for thresh, level in DOWNTIME_CAP_BANDS:
        if D < thresh:
            return level
    return None


def downtime_info(l):
    """(exhaust_effective, D, cap) for one limit -- the downtime layer, computed independently
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
    """Ride the limit: increase useful admission when underspending, reduce it before overshoot.

    Existing work keeps its rigor. Park reserves checkpoint time; only terminal exhaustion uses
    raw fullness. Unknown meters cannot authorize expansion. Downtime can soften Brake to Coast,
    which still reduces admission: it can no longer erase an overshoot warning entirely.
    """
    if is_exhausted(l):
        return "park", "%.0f%% spent: limit exhausted; finish and save, admit no new work" % l["percent"]
    ratio, exhaust, basis = _admission_pace(l)
    if ratio is None:
        return window_pace_posture(l)
    reset = l.get("resets_in_seconds")
    margin = MARGIN_SESSION_RATE if l.get("kind") == "session" else MARGIN_POOL_RATE
    runway = exhaust / margin if exhaust is not None else None
    if runway is not None and runway <= PARK_RUNWAY_S and runway < reset:
        return "park", "only ~%s adjusted runway before the limit, reset in %s; finish and save current work" % (short_dur(runway), short_dur(reset))
    posture = _band(ratio)
    reason = "%s consumption %.2fx the rate affordable through reset" % ("recent" if basis == "recent" else "window-average", ratio)
    if ratio < ACCELERATE_RATIO - 1e-9:
        # A fresh window is not evidence of waste. Expansion from a window average needs
        # enough elapsed time and a material projected shortfall; a measured recent rate
        # can directly show spare capacity without pretending to know future workload.
        length = WINDOW_LENGTHS.get(l.get("kind"))
        elapsed = (length - reset) / length if length and is_num(reset) else None
        window_ratio = window_pace_info(l)[0]
        projected_unused = max(0.0, 100 * (1 - window_ratio)) if window_ratio is not None else None
        ready = basis == "recent" or (elapsed is not None and elapsed >= EXPANSION_MIN_ELAPSED
                                      and projected_unused is not None and projected_unused >= EXPANSION_MIN_UNSPENT)
        if ready:
            posture = "accelerate"
            reason += "; increase useful work toward the affordable rate"
            if projected_unused is not None:
                reason += " (~%.1f%% projected unused at reset at the window-average rate)" % projected_unused
        else:
            reason += "; cruise while the new window establishes a spending history"
    elif posture == "cruise":
        reason += "; maintain useful work toward reset"
    else:
        _, downtime, cap = downtime_info(l)
        if cap is not None and LEVELS.index(cap) < LEVELS.index(posture):
            posture = cap
            reason += "; capped at %s: projected interruption only ~%s, still reduce new commitments" % (cap, short_dur(downtime))
    return posture, reason


def _pace_rank(l):
    if is_exhausted(l):
        return float("inf")
    ratio, _, _ = _admission_pace(l)
    return ratio if ratio is not None else -1.0


def overall_posture(limits):
    """The most restrictive applicable limit governs; expansion requires every limit to allow it."""
    worst, driver, why = "accelerate", None, None
    for l in limits:
        posture, reason = limit_posture(l)
        higher = LEVELS.index(posture) > LEVELS.index(worst)
        tie = driver is None or (posture == worst and _pace_rank(l) > _pace_rank(driver))
        if higher or tie:
            worst, driver, why = posture, l, reason
    if driver is None:
        return "cruise", None, "no limits available to judge; do not expand on missing data"
    return worst, driver, "%s: %s" % (label(driver), why)


# --------------------------------------------------------------------------- posture hysteresis
#
# The per-run posture above (post-cap, but otherwise a fresh computation with no memory of the
# previous run) can flap: a burn sample that wobbles across a band edge, or two axes trading off
# which one is worst, would otherwise change the reported posture every few minutes even though
# nothing about the account materially changed. Asymmetric hysteresis smooths that on the
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


def display_pace_info(limit):
    """One unit for the outside multiplier: rate / rate affordable through reset.

    Prefer a reliable recent sample. Otherwise convert the window-average exhaustion
    projection into the SAME units, rather than substituting consumed/elapsed.
    This display-only reading never enters posture or its forecasts.
    """
    ratio, _ = pace_info(limit)
    basis = "recent"
    if ratio is None:
        window_ratio, _, exhaust = window_pace_info(limit)
        if window_ratio is None or is_exhausted(limit):
            return None
        basis = "window_average"
        if window_ratio == 0:
            ratio = 0.0
        elif not is_num(exhaust) or exhaust <= 0:
            return None
        else:
            ratio = limit["resets_in_seconds"] / exhaust
    if not is_num(ratio):
        return None
    ratio = round(max(0.0, ratio), 2)
    color = ("blue" if ratio < 0.9 else "green" if ratio <= 1.1
             else "yellow" if ratio <= 2 else "orange")
    return {"ratio": ratio, "basis": basis, "color": color}


def schedule_info(limit):
    """Display position against an even spending schedule; never a throttling input.

    Compare percentage points, so equal visible gaps have equal colors in every window.
    Five points either side of the target count as on pace; more than fifteen ahead is orange.
    A quota displayed as 100% is always red, matching the existing exhaustion boundary.
    """
    percent = limit.get("percent")
    remaining = limit.get("resets_in_seconds")
    duration = WINDOW_LENGTHS.get(limit.get("kind"))
    if (not is_num(percent) or not is_num(remaining) or not duration
            or remaining < 0 or remaining > duration):
        return None
    expected = round(100 * (1 - remaining / duration), 2)
    deviation = round(min(100, max(0, percent)) - expected, 2)
    color = ("red" if is_exhausted(limit) else "blue" if deviation < -5 else "green" if deviation <= 5
             else "yellow" if deviation <= 15 else "orange")
    return {"expected_percent": expected, "deviation_percent": deviation, "color": color}


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
        # Downtime-cap transparency: the D/cap that already shaped this limit's rung inside
        # limit_posture (via overall_posture below), plus runway/imminent and surplus, appended
        # after window_pace in this build order.
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
            l["posture_color"] = "gray"
        elif is_exhausted(l):
            l["posture_color"] = POSTURE_COLORS["park"]
        elif not pace_judgeable:
            l["posture_color"] = "gray"
        else:
            l["posture_color"] = POSTURE_COLORS[limit_rung]
        l["display_pace"] = display_pace_info(l)
        l["schedule"] = schedule_info(l)
        l["color"] = ("red" if is_exhausted(l) else
                      l["schedule"]["color"] if l["schedule"] is not None else "gray")
    posture_computed, driver, posture_reason = overall_posture(limits)
    # Smooth the top-level posture with hysteresis (escalate instantly, de-escalate only after
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
        # credentials, where there is no scheduled retry to name). Provided as
        # a real numeric field so a caller wanting "next check in Xm" never
        # has to regex the prose `source` string, which is meant for humans.
        "next_check_seconds": next_check_seconds,
        "posture": posture,
        "posture_instruction": POSTURE_INSTRUCTIONS[posture],
        "posture_policy": "ride_limit_v1",
        # This run's raw posture before hysteresis -- differs from `posture` only while a
        # de-escalation is held pending confirmation; see hysteresis_posture.
        "posture_computed": posture_computed,
        # Why posture is what it is, in one line. Percent still appears here and on every limit
        # because a human reads it; it is a display fact only and never decides posture.
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
    # The wait is the SERVER's `Retry-After` when it sent one, never our own guess alone. A flat
    # guess (e.g. a fixed 900s) creates an inescapable lockout whenever the server's real window is
    # longer: an early retry gets refused, and the refusal rewrites `last_429_at = NOW`, restarting
    # the wait clock -- every cycle retries too early and re-arms itself, so the meter never
    # recovers on its own. Honoring the server's own `Retry-After` avoids that failure mode.
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
    # telling us anything). A non-retryable error such as a 403 ("OAuth token
    # does not meet scope requirement user:profile") still needs a backoff
    # armed here: leaving `last_429_at` untouched would let the next poll
    # tick see the same already-elapsed backoff window and retry almost
    # immediately, repeating every poll interval until a genuine 429 finally
    # arrived and armed the real wait. Arming the same `last_429_at` here
    # stops that hammering: the wait becomes whichever is longer of the
    # BACKOFF floor or a still-on-record `retry_after_s` from an earlier 429
    # (a 403 carries no Retry-After header of its own, so it never sets one,
    # only reuses what's already there).
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
