"""Unit tests for scripts/check-usage.py.

Stdlib unittest only (preserves the script's zero-dependency selling point). The script's
filename is hyphenated so it cannot be `import`ed normally; it is loaded per-test via
importlib.util.spec_from_file_location, always with CLAUDE_USAGE_CACHE / CLAUDE_CONFIG_DIR
pointed at a throwaway tempdir so nothing ever touches the real ~/.claude. Provider calls are
monkeypatched wherever a code path could reach them; the real endpoints are never contacted.

Because the module captures NOW = time.time() at import time, tests that fabricate history
timestamps derive them from the freshly loaded module's `mod.NOW`, never from a fresh
time.time() call.
"""

import contextlib
import hashlib
import importlib.util
import io
import itertools
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRIPT_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "scripts", "check-usage.py")
)

_module_counter = itertools.count()


def load_module(env=None):
    """Load a fresh instance of check-usage.py with the given environment overrides applied
    for the duration of the import (module-level code reads CLAUDE_USAGE_CACHE, CLAUDE_CONFIG_DIR,
    CLAUDE_USAGE_TTL, and CLAUDE_USAGE_429_BACKOFF at import time, so the env must be set before
    exec_module runs). Each call gets a unique module name so Python never returns a cached copy
    and each test gets independent module-level globals (JSON_ONLY, FORCE_FRESH, HISTORY, ...)."""
    env = env or {}
    saved = {}
    for k, v in env.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        name = f"check_usage_under_test_{next(_module_counter)}"
        spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


class CheckUsageTestCase(unittest.TestCase):
    """Base class: gives every test its own tempdir-scoped module instance so tests never share
    mutable module globals (HISTORY, JSON_ONLY, FORCE_FRESH) and never write outside a tempdir."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmpdir = self._td.name
        self.cache_path = os.path.join(self.tmpdir, ".usage-cache.json")
        self.mod = load_module({
            "CLAUDE_USAGE_CACHE": self.cache_path,
            "CLAUDE_CONFIG_DIR": self.tmpdir,
        })

    def run_with_exit(self, fn, *args, **kwargs):
        """Call fn(*args, **kwargs), which is expected to sys.exit(); return (exit_code, stdout)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                fn(*args, **kwargs)
        return cm.exception.code, buf.getvalue()


# --------------------------------------------------------------------------- parse_limits

class TestParseLimits(CheckUsageTestCase):
    def test_modern_limits_shape(self):
        data = {"limits": [
            {"kind": "session", "percent": 42, "severity": "normal", "is_active": True,
             "resets_at": "2099-01-01T00:00:00Z", "scope": None},
        ]}
        out = self.mod.parse_limits(data)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "session")
        self.assertEqual(out[0]["percent"], 42)
        self.assertNotIn("severity", out[0])
        self.assertTrue(out[0]["is_active"])

    def test_legacy_five_hour_seven_day_fallback(self):
        data = {
            "five_hour": {"utilization": 0.5, "resets_at": "2099-01-01T00:00:00Z"},
            "seven_day": {"utilization": 30, "resets_at": "2099-01-01T00:00:00Z"},
        }
        out = self.mod.parse_limits(data)
        kinds = {l["kind"]: l["percent"] for l in out}
        self.assertEqual(kinds["session"], 50.0)  # 0.5 -> scaled to 50%
        self.assertEqual(kinds["weekly_all"], 30)  # already > 1, left as-is

    def test_empty_and_garbage_input(self):
        self.assertIsNone(self.mod.parse_limits({}))
        self.assertIsNone(self.mod.parse_limits(None))
        self.assertIsNone(self.mod.parse_limits({"limits": "garbage"}))
        self.assertIsNone(self.mod.parse_limits({"limits": []}))
        self.assertIsNone(self.mod.parse_limits({"limits": [1, "x", None]}))

    def test_nan_infinity_percent_normalized_to_none(self):
        data = {"limits": [
            {"kind": "session", "percent": float("nan")},
            {"kind": "weekly_all", "percent": float("inf")},
            {"kind": "weekly_scoped", "percent": float("-inf")},
        ]}
        out = self.mod.parse_limits(data)
        self.assertTrue(all(l["percent"] is None for l in out))

    def test_unknown_kind_and_percentless_meter_kept(self):
        data = {"limits": [
            {"kind": "usage_credit", "percent": None},
            {"kind": "some_future_kind", "percent": 10},
        ]}
        out = self.mod.parse_limits(data)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["kind"], "usage_credit")
        self.assertIsNone(out[0]["percent"])
        self.assertEqual(out[1]["kind"], "some_future_kind")

    def test_extra_passthrough_including_nested_objects(self):
        data = {"limits": [
            {"kind": "usage_credit", "balance": {"remaining": 42, "currency": "usd"},
             "note": "beta"},
        ]}
        out = self.mod.parse_limits(data)
        extra = out[0]["extra"]
        self.assertEqual(extra["balance"], {"remaining": 42, "currency": "usd"})
        self.assertEqual(extra["note"], "beta")
        # Known keys must not leak into extra.
        self.assertNotIn("kind", extra)

    def test_extra_none_when_only_known_keys_present(self):
        data = {"limits": [{"kind": "session", "percent": 1}]}
        out = self.mod.parse_limits(data)
        self.assertIsNone(out[0]["extra"])


# --------------------------------------------------------------------------- posture

class TestLimitPosture(CheckUsageTestCase):
    def _limit(self, **overrides):
        base = {"kind": "session", "percent": 0, "severity": None, "resets_in_seconds": None}
        base.update(overrides)
        return base

    def test_high_percent_alone_does_not_escalate_when_on_pace(self):
        # Replaces test_percent_band_boundaries_session: the percent-band table (PERCENT_BANDS)
        # was deleted 2026-08-11 because it acted on fullness alone. This protects the new
        # guarantee: a high percent with the clock still on pace stays 'normal'. Exact incident
        # case -- weekly_all at 97% with 7999s (~2h13m) left in a 7-day window is AHEAD of pace
        # (2h is ~1.2% of a week), yet the old percent band said wind_down and halted a healthy
        # six-runner fleet. window ratio = 0.97 / ((604800-7999)/604800) = 0.983 < 1.0 -> normal.
        l = self._limit(kind="weekly_all", percent=97, resets_in_seconds=7999)
        self.assertEqual(self.mod.limit_posture(l)[0], "normal")

    def test_same_pace_same_posture_regardless_of_kind(self):
        # Replaces test_percent_band_boundaries_weekly_all: per-kind percent band tables (higher
        # thresholds for weekly_all) were deleted 2026-08-11 along with the rest of the percent
        # axis. Protects the new guarantee: no per-kind behavior survives -- session and
        # weekly_all at the identical pace (same percent consumed, same fraction of their
        # respective windows elapsed) land on the identical posture. Both at 80% consumed with
        # half their window elapsed -> ratio 1.6 -> frugal, for either kind.
        session = self._limit(kind="session", percent=80,
                              resets_in_seconds=self.mod.WINDOW_LENGTHS["session"] // 2)
        weekly = self._limit(kind="weekly_all", percent=80,
                             resets_in_seconds=self.mod.WINDOW_LENGTHS["weekly_all"] // 2)
        self.assertEqual(self.mod.limit_posture(session)[0], "frugal")
        self.assertEqual(self.mod.limit_posture(weekly)[0], "frugal")

    def test_provider_severity_is_discarded_and_does_not_change_posture(self):
        # The provider field is percent-derived, so it is deliberately absent from the public
        # contract as well as from the posture decision.
        normal = self._limit(percent=10, severity=None)
        elevated = self._limit(percent=10, severity="warning")
        posture_n, reason_n = self.mod.limit_posture(normal)
        posture_e, reason_e = self.mod.limit_posture(elevated)
        self.assertEqual(posture_n, "normal")
        self.assertEqual(posture_e, "normal")
        self.assertNotIn("severity", reason_e)
        parsed = self.mod.parse_limits({"limits": [{
            "kind": "session", "percent": 10, "severity": "warning",
            "resets_at": None,
        }]})
        self.assertNotIn("severity", parsed[0])

    def test_severity_does_not_change_posture_percentless_meter(self):
        # Replaces test_percentless_meter_with_elevated_severity_escalates: previously a
        # percentless meter with elevated severity escalated one rung via the (now-deleted)
        # severity bump. Protects the same new guarantee as above for the percentless case:
        # neither axis can judge a meter with no percent, so posture stays 'normal' regardless
        # of severity.
        l = self._limit(percent=None, severity="warning")
        self.assertEqual(self.mod.limit_posture(l)[0], "normal")

    def test_near_reset_high_percent_no_special_case_needed_pace_governs(self):
        # Replaces test_session_near_reset_relief: the session-only near-reset relief (percent
        # >= 90 relaxed one rung when resets_in_seconds < 900) was deleted 2026-08-11 -- it
        # existed to soften a high PERCENT shortly before reset, and with percent gone as a
        # decision input there is nothing left to relieve. Protects the new guarantee: pure pace
        # already gets this right with no kind-specific carve-out. 95% consumed with only 500s
        # left is ahead of pace for BOTH a 5h session (97% of the window elapsed) and a 7-day
        # weekly window (99.9% elapsed) -- ratio < 1.0 either way -> normal, not the 'frugal'
        # relief-band compromise the old carve-out produced, and no per-kind logic is needed to
        # get there.
        session = self._limit(kind="session", percent=95, resets_in_seconds=500)
        weekly = self._limit(kind="weekly_all", percent=95, resets_in_seconds=500)
        self.assertEqual(self.mod.limit_posture(session)[0], "normal")
        self.assertEqual(self.mod.limit_posture(weekly)[0], "normal")

    def test_unknown_kind_gets_no_window_pace(self):
        # Replaces test_unknown_kind_uses_default_bands: the default percent-band table used for
        # an unrecognized kind was deleted along with the rest of the percent axis. Protects the
        # new guarantee: an unknown kind has no WINDOW_LENGTHS entry, so window pace explicitly
        # declines to judge it (says so in the reason) rather than guessing a window length --
        # and, per the general no-percent-alone-escalation rule, a high percent with no burn
        # sample stays 'normal'.
        unknown = self._limit(kind="mystery_meter", percent=75)
        posture, reason = self.mod.limit_posture(unknown)
        self.assertEqual(posture, "normal")
        self.assertIn("window length unknown for kind 'mystery_meter'", reason)

    def test_low_percent_high_pace_escalates(self):
        # The screenshot case: only 20% used, but burning ~1.5x sustainable -> escalates off
        # 'normal' on pace alone. reset 15000s, burn +8% / 1000s -> exhaust in 10000s, ratio
        # 15000/10000 = 1.5 -> frugal provisional. v3: the downtime cap then looks at what
        # emptying this session actually costs -- margin-adjusted earliest exhaust (session,
        # optimistic /0.85) is ~11765s, leaving D ~= 3235s (~54m) before the 15000s reset, inside
        # the [900s,3600s) 'measured' cap band -- so the terminal rung is 'measured', one below
        # the pace-alone 'frugal'. Still well above 'normal', which is the property this test
        # protects: a low percent burning fast must not read as fine.
        l = self._limit(kind="session", percent=20, resets_in_seconds=15000,
                        burn={"delta_percent": 8.0, "over_seconds": 1000})
        self.assertEqual(self.mod.limit_posture(l)[0], "measured")


class TestOverallPosture(CheckUsageTestCase):
    def test_worst_wins(self):
        # resets_in_seconds=None used to be irrelevant because the (now-deleted) percent axis
        # judged purely on fullness. Under pace-only posture, a None reset means NEITHER pace
        # axis can be judged (window pace needs a real reset_s to locate the window), so all
        # three limits would silently read 'normal' and this test would stop testing worst-wins
        # at all. Given real reset times instead, chosen so each limit's window pace lands on a
        # distinct rung: session at 20% of its window elapsed with 10% consumed -> ratio 0.2 ->
        # normal; weekly_all at 10% of its window elapsed with 95% already consumed -> ratio 9.5
        # -> wind_down; weekly_scoped at 50% elapsed with 76% consumed -> ratio 1.52 -> frugal.
        # Worst among them is still wind_down, driven by the weekly_all limit.
        limits = [
            {"kind": "session", "percent": 10, "severity": None,
             "resets_in_seconds": int(self.mod.WINDOW_LENGTHS["session"] * 0.8)},
            {"kind": "weekly_all", "percent": 95, "severity": None,
             "resets_in_seconds": int(self.mod.WINDOW_LENGTHS["weekly_all"] * 0.9)},
            {"kind": "weekly_scoped", "percent": 76, "severity": None,
             "resets_in_seconds": int(self.mod.WINDOW_LENGTHS["weekly_scoped"] * 0.5)},
        ]
        posture, driver, reason = self.mod.overall_posture(limits)
        self.assertEqual(posture, "wind_down")
        self.assertIs(driver, limits[1])

    def test_all_normal(self):
        # overall_posture's docstring (2026-08-11) now guarantees a driver is returned even at
        # 'normal' -- "why is it normal" was exactly the question nobody could answer during the
        # incident, so the single limit is always named as the driver rather than driver being
        # None whenever nothing escalates.
        limits = [
            {"kind": "session", "percent": 1, "severity": None, "resets_in_seconds": None},
        ]
        posture, driver, reason = self.mod.overall_posture(limits)
        self.assertEqual(posture, "normal")
        self.assertIs(driver, limits[0])


# --------------------------------------------------------------------------- pace

class TestPaceInfo(CheckUsageTestCase):
    def _limit(self, **overrides):
        base = {"kind": "session", "percent": 20, "resets_in_seconds": 10000,
                "burn": {"delta_percent": 8.0, "over_seconds": 1000}}
        base.update(overrides)
        return base

    def test_ratio_and_exhaust_math(self):
        # remaining 80%, rate 8%/1000s = 0.008%/s -> exhaust 10000s; sustainable 80/10000 =
        # 0.008%/s -> ratio exactly 1.0 (lands on empty right at reset).
        ratio, exhaust = self.mod.pace_info(self._limit())
        self.assertAlmostEqual(ratio, 1.0, places=6)
        self.assertAlmostEqual(exhaust, 10000.0, places=6)

    def test_ratio_above_one_when_burning_too_fast(self):
        # double the burn -> exhaust in 5000s, ratio 10000/5000 = 2.0.
        ratio, exhaust = self.mod.pace_info(
            self._limit(burn={"delta_percent": 16.0, "over_seconds": 1000}))
        self.assertAlmostEqual(ratio, 2.0, places=6)
        self.assertAlmostEqual(exhaust, 5000.0, places=6)

    def test_ratio_identity_reset_over_exhaust(self):
        # ratio must equal seconds_to_reset / seconds_to_exhaustion for arbitrary inputs.
        l = self._limit(percent=37, resets_in_seconds=7200,
                        burn={"delta_percent": 9.0, "over_seconds": 640})
        ratio, exhaust = self.mod.pace_info(l)
        self.assertAlmostEqual(ratio, 7200 / exhaust, places=6)

    def test_small_burn_delta_ignored_as_noise(self):
        # below PACE_MIN_BURN_DELTA (2.0) the extrapolated rate is too noisy to steer on.
        ratio, exhaust = self.mod.pace_info(
            self._limit(burn={"delta_percent": 1.5, "over_seconds": 1000}))
        self.assertIsNone(ratio)
        self.assertIsNone(exhaust)

    def test_no_burn_baseline(self):
        self.assertEqual(self.mod.pace_info(self._limit(burn=None)), (None, None))

    def test_reset_in_past_or_unknown(self):
        self.assertEqual(self.mod.pace_info(self._limit(resets_in_seconds=0)), (None, None))
        self.assertEqual(self.mod.pace_info(self._limit(resets_in_seconds=-5)), (None, None))
        self.assertEqual(self.mod.pace_info(self._limit(resets_in_seconds=None)), (None, None))

    def test_already_full_yields_no_pace(self):
        self.assertEqual(self.mod.pace_info(self._limit(percent=100)), (None, None))

    def test_reset_beyond_horizon_yields_no_pace(self):
        # A weekly window days from reset: a ~15-min burst must not extrapolate across it.
        self.assertEqual(self.mod.pace_info(self._limit(resets_in_seconds=259200)), (None, None))

    def test_horizon_boundary(self):
        # At exactly the horizon pace is still judged; one second past it is not.
        at = self._limit(resets_in_seconds=self.mod.PACE_MAX_HORIZON_S,
                         burn={"delta_percent": 40.0, "over_seconds": 1000})
        self.assertIsNotNone(self.mod.pace_info(at)[0])
        past = self._limit(resets_in_seconds=self.mod.PACE_MAX_HORIZON_S + 1,
                           burn={"delta_percent": 40.0, "over_seconds": 1000})
        self.assertEqual(self.mod.pace_info(past), (None, None))

    def test_percentless_meter_yields_no_pace(self):
        self.assertEqual(self.mod.pace_info(self._limit(percent=None)), (None, None))

    def test_malformed_burn_fields_ignored(self):
        self.assertEqual(
            self.mod.pace_info(self._limit(burn={"delta_percent": "x", "over_seconds": 1000})),
            (None, None))
        self.assertEqual(
            self.mod.pace_info(self._limit(burn={"delta_percent": 8.0, "over_seconds": 0})),
            (None, None))


class TestPacePosture(CheckUsageTestCase):
    def _limit(self, ratio_target, percent=20, exhaust=None):
        """Build a limit whose pace_info yields approximately ratio_target. With remaining and a
        chosen reset, pick a burn rate so exhaust = reset / ratio_target."""
        remaining = 100 - percent
        reset_s = 10000
        exhaust_s = exhaust if exhaust is not None else reset_s / ratio_target
        rate = remaining / exhaust_s          # %/s needed to hit that exhaust
        return {"kind": "session", "percent": percent, "resets_in_seconds": reset_s,
                "burn": {"delta_percent": rate * 1000, "over_seconds": 1000}}

    def test_sustainable_pace_is_normal(self):
        self.assertEqual(self.mod.pace_posture(self._limit(0.5))[0], "normal")

    def test_band_rungs(self):
        self.assertEqual(self.mod.pace_posture(self._limit(1.1))[0], "measured")
        self.assertEqual(self.mod.pace_posture(self._limit(1.5))[0], "frugal")
        self.assertEqual(self.mod.pace_posture(self._limit(3.0))[0], "conserve")
        self.assertEqual(self.mod.pace_posture(self._limit(5.0))[0], "wind_down")

    def test_no_burn_is_normal(self):
        l = {"kind": "session", "percent": 20, "resets_in_seconds": 10000, "burn": None}
        self.assertEqual(self.mod.pace_posture(l)[0], "normal")

    def test_no_more_imminent_exhaustion_backstop_in_pace_posture(self):
        # Replaces test_imminent_exhaustion_floors_at_conserve: the v2 absolute-runway backstop
        # (floor at 'conserve' inside 30 minutes of projected exhaustion) was deleted from
        # pace_posture in v3. Same shape as the old test -- ratio ~1.1, exhaust 1500s (< the
        # renamed IMMINENT_RUNWAY_S=1800) -- but pace_posture on its own now reports plain
        # 'measured' off the ratio alone; the backstop's old job (never RAISING a rung, unlike the
        # old backstop) moved to `imminent` (see TestImminentAndDowntimeFields below) plus the
        # downtime cap in limit_posture, which can only lower a rung, not raise it the way this
        # backstop used to.
        l = {"kind": "session", "percent": 90, "resets_in_seconds": 1650,
             "burn": {"delta_percent": (10 / 1500) * 1000, "over_seconds": 1000}}
        ratio, exhaust = self.mod.pace_info(l)
        self.assertLess(ratio, 1.25)
        self.assertLessEqual(exhaust, self.mod.IMMINENT_RUNWAY_S)
        self.assertEqual(self.mod.pace_posture(l)[0], "measured")

    def test_weekly_within_horizon_reaches_middle_rung(self):
        # Regression for the binary-pace bug: a weekly limit near its reset must be able to land on
        # a middle rung, not jump straight to wind_down. 85%, 3h to reset, +2%/900s -> exhaust
        # ~6750s, ratio ~1.6 -> frugal.
        l = {"kind": "weekly_all", "percent": 85, "resets_in_seconds": 10800,
             "burn": {"delta_percent": 2.0, "over_seconds": 900}}
        self.assertEqual(self.mod.pace_posture(l)[0], "frugal")

    def test_weekly_beyond_horizon_pace_is_normal(self):
        # Same fast burn but reset is days out: a 15-min burst must not extrapolate -> pace normal,
        # so a heavy session no longer slams the weekly limit to wind_down.
        l = {"kind": "weekly_all", "percent": 40, "resets_in_seconds": 259200,
             "burn": {"delta_percent": 3.0, "over_seconds": 900}}
        self.assertEqual(self.mod.pace_posture(l)[0], "normal")

    def test_imminent_backstop_does_not_fire_when_sustainable(self):
        # Tiny exhaust window but reset is even sooner (ratio < 1) -> pace is normal, backstop off.
        l = {"kind": "session", "percent": 90, "resets_in_seconds": 500,
             "burn": {"delta_percent": (10 / 1000) * 1000, "over_seconds": 1000}}
        ratio, _ = self.mod.pace_info(l)
        self.assertLess(ratio, 1.0)
        self.assertEqual(self.mod.pace_posture(l)[0], "normal")


class TestPaceInJsonOutput(CheckUsageTestCase):
    def test_render_emits_pace_and_pace_driven_posture(self):
        mod = self.mod
        mod.HISTORY = [
            {"at": mod.NOW - 1000, "limits": {"session|": 12.0}, "resets": {"session|": "R"}},
        ]
        limits = [{
            "kind": "session", "percent": 20.0, "resets_at": "R", "resets_in_seconds": 15000,
            "severity": None, "scope_model": None, "is_active": False, "extra": None,
        }]
        code, out = self.run_with_exit(mod.render, limits, 0, False, "cache")
        payload = json.loads(out.strip().splitlines()[-1])
        pace = payload["limits"][0]["pace"]
        self.assertIsNotNone(pace)
        self.assertAlmostEqual(pace["ratio"], 1.5, places=2)   # 15000s reset / 10000s exhaust
        self.assertEqual(pace["exhaust_seconds"], 10000)
        # Low percent (20) but unsustainable pace -> escalates off 'normal'. `pace` itself is the
        # raw, uncapped reading (still 1.5x here); `posture` is v3's capped terminal rung -- see
        # test_low_percent_high_pace_escalates for the D/cap arithmetic (same limit shape here:
        # D ~= 3235s -> 'measured' cap, one below the pace-alone 'frugal').
        self.assertEqual(payload["posture_computed"], "measured")
        self.assertEqual(payload["posture"], "measured")

    def test_render_pace_null_without_burn_baseline(self):
        # Burn pace (needs a sample) is still null with no history baseline -- unchanged. Window
        # pace needs no sample, so it judges this limit anyway, and judges it fine: session window
        # 18000s with resets_in 15000 leaves 83.3% of the window still to run, and 80% of the
        # budget unspent, so headroom = 0.80 / 0.8333 = 0.96 -- nearly a full budget pace still
        # affordable from here -> 'normal'.
        #
        # Worth noting against the earlier draft of this change, which banded on consumed/elapsed
        # (ratio 1.2) and called this 'measured'. Banding on headroom instead is what stops the
        # governor reacting to a perfectly healthy 20% session, and is why the two pace axes do
        # not share a band table.
        mod = self.mod
        mod.HISTORY = []
        limits = [{
            "kind": "session", "percent": 20.0, "resets_at": "R", "resets_in_seconds": 15000,
            "severity": None, "scope_model": None, "is_active": False, "extra": None,
        }]
        code, out = self.run_with_exit(mod.render, limits, 0, False, "cache")
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertIsNone(payload["limits"][0]["pace"])
        self.assertEqual(payload["posture"], "normal")
        # Window pace judged it without any sample: headroom 0.96, well clear of the 0.75 rung.
        self.assertAlmostEqual(payload["limits"][0]["window_pace"]["headroom"], 0.96, places=2)
        self.assertIn("budget pace still affordable", payload["posture_reason"])


# --------------------------------------------------------------------------- compute_burn

class TestComputeBurn(CheckUsageTestCase):
    def test_prefers_most_recent_baseline_at_least_900s_old(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R"}]
        mod.HISTORY = [
            {"at": mod.NOW - 1000, "limits": {"session|": 40.0}, "resets": {"session|": "R"}},
            {"at": mod.NOW - 940, "limits": {"session|": 45.0}, "resets": {"session|": "R"}},
            {"at": mod.NOW - 100, "limits": {"session|": 58.0}, "resets": {"session|": "R"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        burn = limits[0]["burn"]
        self.assertIsNotNone(burn)
        self.assertEqual(burn["delta_percent"], 15.0)
        self.assertEqual(burn["over_seconds"], 940)

    def test_falls_back_to_most_recent_at_least_120s_old(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R"}]
        mod.HISTORY = [
            {"at": mod.NOW - 200, "limits": {"session|": 50.0}, "resets": {"session|": "R"}},
            {"at": mod.NOW - 150, "limits": {"session|": 55.0}, "resets": {"session|": "R"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        burn = limits[0]["burn"]
        self.assertIsNotNone(burn)
        self.assertEqual(burn["delta_percent"], 5.0)
        self.assertEqual(burn["over_seconds"], 150)

    def test_skips_the_same_snapshot_the_reading_came_from(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R"}]
        mod.HISTORY = [
            {"at": mod.NOW, "limits": {"session|": 60.0}, "resets": {"session|": "R"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])

    def test_same_window_guard_skips_mismatched_resets_at(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R2"}]
        mod.HISTORY = [
            {"at": mod.NOW - 1000, "limits": {"session|": 40.0}, "resets": {"session|": "R1"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])

    def test_subsecond_reset_jitter_is_still_same_window(self):
        # Regression: the endpoint jitters the sub-second part of resets_at within one window, so an
        # exact-match same-window guard rejected every real baseline and left burn (and pace) null.
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0,
                   "resets_at": "2026-07-14T06:09:59.760271+00:00"}]
        mod.HISTORY = [
            {"at": mod.NOW - 950, "limits": {"session|": 45.0},
             "resets": {"session|": "2026-07-14T06:09:59.935632+00:00"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        burn = limits[0]["burn"]
        self.assertIsNotNone(burn)
        self.assertEqual(burn["delta_percent"], 15.0)

    def test_genuine_window_flip_still_rejected(self):
        # A reset that moved ~5h is a real boundary between samples: reject even though the raw
        # delta is positive (10% -> reset -> 15% must not read as a tame +5%).
        mod = self.mod
        limits = [{"kind": "session", "percent": 15.0,
                   "resets_at": "2026-07-14T11:09:59.000000+00:00"}]
        mod.HISTORY = [
            {"at": mod.NOW - 950, "limits": {"session|": 10.0},
             "resets": {"session|": "2026-07-14T06:09:59.000000+00:00"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])

    def test_negative_delta_skipped(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 30.0, "resets_at": "R"}]
        mod.HISTORY = [
            {"at": mod.NOW - 1000, "limits": {"session|": 80.0}, "resets": {"session|": "R"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])

    def test_corrupt_history_entries_ignored(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R"}]
        mod.HISTORY = [
            None,
            "not a dict",
            {"limits": {"session|": 10.0}},                      # missing 'at'
            {"at": "bad", "limits": {"session|": 10.0}},          # non-numeric 'at'
            {"at": mod.NOW - 950, "limits": "not a dict"},        # limits not a dict
            {"at": mod.NOW - 950, "limits": {"session|": 45.0}, "resets": {"session|": "R"}},
        ]
        mod.compute_burn(limits, mod.NOW)
        burn = limits[0]["burn"]
        self.assertIsNotNone(burn)
        self.assertEqual(burn["delta_percent"], 15.0)

    def test_no_usable_baseline_burn_is_null(self):
        mod = self.mod
        limits = [{"kind": "session", "percent": 60.0, "resets_at": "R"}]
        mod.HISTORY = []
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])

    def test_percentless_limit_skipped_entirely(self):
        mod = self.mod
        limits = [{"kind": "usage_credit", "percent": None, "resets_at": None}]
        mod.HISTORY = [
            {"at": mod.NOW - 1000, "limits": {"usage_credit|": 40.0}, "resets": {}},
        ]
        mod.compute_burn(limits, mod.NOW)
        self.assertIsNone(limits[0]["burn"])


# --------------------------------------------------------------------------- reset_passed

class TestSameWindow(CheckUsageTestCase):
    def test_subsecond_jitter_is_same_window(self):
        self.assertTrue(self.mod.same_window(
            "2026-07-14T06:09:59.100000+00:00", "2026-07-14T06:09:59.900000+00:00"))

    def test_window_flip_is_not_same(self):
        self.assertFalse(self.mod.same_window(
            "2026-07-14T06:09:59+00:00", "2026-07-14T11:09:59+00:00"))

    def test_missing_side_treated_as_same_window(self):
        self.assertTrue(self.mod.same_window(None, "2026-07-14T06:09:59+00:00"))
        self.assertTrue(self.mod.same_window("2026-07-14T06:09:59+00:00", None))

    def test_unparseable_falls_back_to_exact_equality(self):
        self.assertTrue(self.mod.same_window("R", "R"))
        self.assertFalse(self.mod.same_window("R1", "R2"))

    def test_tolerance_boundary(self):
        base = "2026-07-14T06:00:00+00:00"
        self.assertTrue(self.mod.same_window(base, "2026-07-14T06:04:00+00:00"))    # 240s, within
        self.assertFalse(self.mod.same_window(base, "2026-07-14T06:10:00+00:00"))   # 600s, outside


class TestResetPassed(CheckUsageTestCase):
    def test_reset_after_fetched_at_is_true(self):
        mod = self.mod
        fetched_at = mod.NOW - 100
        # resets_in_seconds negative -> reset_epoch = NOW + s; make it land after fetched_at.
        limits = [{"resets_in_seconds": -10}]
        self.assertTrue(mod.reset_passed(limits, fetched_at))

    def test_reset_before_fetched_at_is_false(self):
        mod = self.mod
        fetched_at = mod.NOW  # fetched just now, after an already-idle reset
        limits = [{"resets_in_seconds": -10}]
        self.assertFalse(mod.reset_passed(limits, fetched_at))

    def test_future_reset_ignored(self):
        mod = self.mod
        limits = [{"resets_in_seconds": 500}]
        self.assertFalse(mod.reset_passed(limits, mod.NOW - 1000))

    def test_missing_resets_in_seconds_ignored(self):
        mod = self.mod
        limits = [{"resets_in_seconds": None}]
        self.assertFalse(mod.reset_passed(limits, mod.NOW - 1000))


# --------------------------------------------------------------------------- _int_env

class TestIntEnv(CheckUsageTestCase):
    def test_non_int_falls_back_to_default(self):
        os.environ["CU_TEST_VAR"] = "not-an-int"
        try:
            self.assertEqual(self.mod._int_env("CU_TEST_VAR", 5), 5)
        finally:
            del os.environ["CU_TEST_VAR"]

    def test_negative_falls_back_to_default(self):
        os.environ["CU_TEST_VAR"] = "-1"
        try:
            self.assertEqual(self.mod._int_env("CU_TEST_VAR", 5), 5)
        finally:
            del os.environ["CU_TEST_VAR"]

    def test_zero_is_allowed(self):
        os.environ["CU_TEST_VAR"] = "0"
        try:
            self.assertEqual(self.mod._int_env("CU_TEST_VAR", 5), 0)
        finally:
            del os.environ["CU_TEST_VAR"]

    def test_missing_env_uses_default(self):
        os.environ.pop("CU_TEST_VAR", None)
        self.assertEqual(self.mod._int_env("CU_TEST_VAR", 42), 42)


# --------------------------------------------------------------------------- credentials

class TestCredentialSelection(CheckUsageTestCase):
    def _blob(self, token, expires_at):
        return json.dumps({"claudeAiOauth": {
            "accessToken": token,
            "expiresAt": expires_at,
        }})

    def test_blob_access_token_accepts_unexpired_millisecond_expiration(self):
        raw = self._blob("keychain-token", (self.mod.NOW + 60) * 1000)
        self.assertEqual("keychain-token", self.mod._blob_access_token(raw))

    def test_blob_access_token_rejects_expired_millisecond_expiration(self):
        raw = self._blob("expired-token", (self.mod.NOW - 60) * 1000)
        self.assertIsNone(self.mod._blob_access_token(raw))

    def test_expired_keychain_falls_through_to_running_process(self):
        self.mod._keychain_blob = lambda: self._blob(
            "expired-keychain-token", (self.mod.NOW - 60) * 1000)
        self.mod._file_blob = lambda: None
        self.mod._sessionwatcher_token = lambda: None
        self.mod._process_env_token = lambda: "live-process-token"
        self.assertEqual("live-process-token", self.mod.get_access_token())


# --------------------------------------------------------------------------- parse_args

class TestParseArgs(CheckUsageTestCase):
    def test_unknown_flag_exits_2_with_json_line(self):
        code, out = self.run_with_exit(self.mod.parse_args, ["--bogus"])
        self.assertEqual(code, 2)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(payload["available"])
        self.assertIn("--bogus", payload["reason"])

    def test_json_flag_sets_json_only_but_not_force_fresh(self):
        self.mod.parse_args(["--json"])
        self.assertTrue(self.mod.JSON_ONLY)
        self.assertFalse(self.mod.FORCE_FRESH)

    def test_fresh_flag_sets_force_fresh_but_not_json_only(self):
        self.mod.parse_args(["--fresh"])
        self.assertTrue(self.mod.FORCE_FRESH)
        self.assertFalse(self.mod.JSON_ONLY)

    def test_cache_only_flag_is_independent(self):
        self.mod.parse_args(["--cache-only"])
        self.assertTrue(self.mod.CACHE_ONLY)
        self.assertFalse(self.mod.FORCE_FRESH)
        self.assertFalse(self.mod.JSON_ONLY)

    def test_help_long_exits_0_prints_usage_no_json(self):
        code, out = self.run_with_exit(self.mod.parse_args, ["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(out, self.mod.USAGE_TEXT)
        self.assertNotIn('"available"', out)

    def test_help_short_exits_0_prints_usage_no_json(self):
        code, out = self.run_with_exit(self.mod.parse_args, ["-h"])
        self.assertEqual(code, 0)
        self.assertEqual(out, self.mod.USAGE_TEXT)
        self.assertNotIn('"available"', out)

    def test_help_touches_no_disk_or_network(self):
        # No cache file should exist before or after --help.
        self.assertFalse(os.path.exists(self.cache_path))
        self.run_with_exit(self.mod.parse_args, ["--help"])
        self.assertFalse(os.path.exists(self.cache_path))

    def test_version_exits_0_prints_version_no_json(self):
        code, out = self.run_with_exit(self.mod.parse_args, ["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out, f"check-usage {self.mod.VERSION}\n")
        self.assertNotIn('"available"', out)


# --------------------------------------------------------------------------- time helpers

class TestTimeHelpers(CheckUsageTestCase):
    def test_seconds_until_z_suffix(self):
        target = datetime.now(timezone.utc) + timedelta(seconds=100)
        iso = target.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        secs = self.mod.seconds_until(iso)
        self.assertIsNotNone(secs)
        self.assertAlmostEqual(secs, 100, delta=5)

    def test_seconds_until_offset_suffix(self):
        target = datetime.now(timezone.utc) + timedelta(seconds=200)
        iso = target.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
        secs = self.mod.seconds_until(iso)
        self.assertIsNotNone(secs)
        self.assertAlmostEqual(secs, 200, delta=5)

    def test_seconds_until_garbage_returns_none(self):
        self.assertIsNone(self.mod.seconds_until("not-a-date"))
        self.assertIsNone(self.mod.seconds_until(None))

    def test_rel_time_garbage_returns_empty_string(self):
        self.assertEqual(self.mod.rel_time("not-a-date"), "")
        self.assertEqual(self.mod.rel_time(None), "")

    def test_short_dur_minutes_and_hours(self):
        self.assertEqual(self.mod.short_dur(45 * 60), "45m")
        self.assertEqual(self.mod.short_dur(3 * 3600 + 5 * 60), "3h05m")


# --------------------------------------------------------------------------- age clamp

class TestAgeClamp(CheckUsageTestCase):
    def test_negative_age_renders_as_zero(self):
        code, out = self.run_with_exit(self.mod.render, [], -5, False, "cache")
        self.assertEqual(code, 0)
        self.assertIn("0s ago", out)
        json_line = out.strip().splitlines()[-1]
        payload = json.loads(json_line)
        self.assertEqual(payload["age_seconds"], 0)


class TestAccountMetadata(CheckUsageTestCase):
    def test_profile_metadata_is_normalized_from_the_same_oauth_account(self):
        class Response:
            status = 200
            url = self.mod.PROFILE_ENDPOINT
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self):
                return json.dumps({
                    "account": {"email": "billy@example.com", "uuid": "account-billy"},
                    "organization": {
                        "uuid": "org-billy",
                        "organization_type": "claude_max",
                        "rate_limit_tier": "default_claude_max_5x",
                    },
                }).encode()

        self.mod.urllib.request.urlopen = lambda request, timeout: Response()
        self.assertEqual(self.mod.fetch_account("sk-ant-oat-test-token-value"), (200, {
            "email": "billy@example.com",
            "account_uuid": "account-billy",
            "organization_uuid": "org-billy",
            "subscription_type": "max",
            "rate_limit_tier": "default_claude_max_5x",
        }))

    def test_render_emits_account_metadata_at_top_level(self):
        account = {"email": "billy@example.com", "account_uuid": "account-billy",
                   "organization_uuid": "org-billy", "subscription_type": "max",
                   "rate_limit_tier": "default_claude_max_5x"}
        code, out = self.run_with_exit(self.mod.render, [], 0, False, "cache", None, account)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["account"], account)

    def test_profile_http_error_is_recorded_with_retry_after(self):
        error = self.mod.urllib.error.HTTPError(
            self.mod.PROFILE_ENDPOINT, 429, "rate limited",
            {"Retry-After": "120"}, io.BytesIO(b'{"error":"slow down"}'))

        def reject(_request, timeout):
            raise error

        self.mod.urllib.request.urlopen = reject
        self.assertEqual(self.mod.fetch_account("sk-ant-oat-test-token-value"), (429, None))
        cache = self.mod.load_cache()
        self.assertEqual(cache["http_errors"][-1]["status"], 429)
        self.assertEqual(cache["http_errors"][-1]["endpoint"], "profile")
        self.assertEqual(cache["profile_retry_after_s"], 120)
        self.assertNotIn("retry_after_s", cache)

    def test_profile_failure_never_arms_usage_backoff(self):
        token = "sk-ant-oat-test-token-value-long-enough"
        reset = datetime.fromtimestamp(self.mod.NOW + 3600, timezone.utc).isoformat()
        self.mod.save_cache({"history": [], "retry_after_s": 1772})
        self.mod.get_access_token = lambda: token
        self.mod.fetch = lambda value: (200, {"limits": [{
            "kind": "session", "percent": 26, "resets_at": reset,
        }]})
        self.mod.fetch_account = lambda value: (403, None)
        old_argv = sys.argv[:]
        sys.argv = ["check-usage.py", "--json", "--fresh"]
        try:
            code, out = self.run_with_exit(self.mod.main)
        finally:
            sys.argv = old_argv
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out.strip().splitlines()[-1])["stale"])
        cache = self.mod.load_cache()
        self.assertEqual(cache["last_429_at"], 0)
        self.assertEqual(cache["retry_after_s"], 0)

    def test_profile_backoff_skips_only_profile_request(self):
        token = "sk-ant-oat-test-token-value-long-enough"
        reset = datetime.fromtimestamp(self.mod.NOW + 3600, timezone.utc).isoformat()
        self.mod.save_cache({
            "history": [],
            "profile_failed_at": self.mod.NOW - 60,
            "profile_status": 403,
        })
        self.mod.get_access_token = lambda: token
        usage_calls = []
        self.mod.fetch = lambda value: (
            usage_calls.append(value) or (200, {"limits": [{
                "kind": "session", "percent": 26, "resets_at": reset,
            }]}))
        self.mod.fetch_account = lambda value: self.fail("profile backoff was ignored")
        old_argv = sys.argv[:]
        sys.argv = ["check-usage.py", "--json", "--fresh"]
        try:
            code, out = self.run_with_exit(self.mod.main)
        finally:
            sys.argv = old_argv
        self.assertEqual(code, 0)
        self.assertEqual(usage_calls, [token])
        self.assertFalse(json.loads(out.strip().splitlines()[-1])["stale"])

    def test_fresh_usage_reuses_account_bound_to_unchanged_token(self):
        token = "sk-ant-oat-test-token-value-long-enough"
        fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
        reset = datetime.fromtimestamp(self.mod.NOW + 3600, timezone.utc).isoformat()
        account = {"email": "billy@example.com", "account_uuid": "account-billy",
                   "organization_uuid": "org-billy", "subscription_type": "max",
                   "rate_limit_tier": "default_claude_max_5x"}
        self.mod.save_cache({
            "fetched_at": self.mod.NOW - self.mod.TTL - 1,
            "data": {"limits": [{"kind": "session", "percent": 25,
                                    "resets_at": reset}]},
            "account": account,
            "credential_fingerprint": fingerprint,
            "account_fetched_at": self.mod.NOW - 60,
            "history": [],
        })
        self.mod.get_access_token = lambda: token
        self.mod.fetch = lambda value: (200, {"limits": [{"kind": "session", "percent": 26,
                                                            "resets_at": reset}]})
        self.mod.fetch_account = lambda value: self.fail("unchanged account was re-requested")
        old_argv = sys.argv[:]
        sys.argv = ["check-usage.py", "--json", "--fresh"]
        try:
            code, out = self.run_with_exit(self.mod.main)
        finally:
            sys.argv = old_argv
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["account"], account)


# --------------------------------------------------------------------------- 429 path

class Test429ClobberPreservation(CheckUsageTestCase):
    def test_429_path_preserves_concurrently_written_newer_cache(self):
        mod = self.mod

        old_cache = {
            "v": 1,
            "fetched_at": mod.NOW - 10000,  # older than TTL, forces main() past the cache-fresh path
            "data": {"limits": [{"kind": "session", "percent": 10}]},
            "last_429_at": 0,
            "history": [{"at": mod.NOW - 5000, "limits": {"session|": 10}, "resets": {}}],
        }
        new_cache = {
            "v": 1,
            "fetched_at": mod.NOW - 50,
            "data": {"limits": [{"kind": "session", "percent": 99}]},
            "last_429_at": 0,
            "history": [{"at": mod.NOW - 40, "limits": {"session|": 99}, "resets": {}}],
        }

        # main() calls load_cache() twice on this path: once at the top, once inside the 429
        # branch (by design, to avoid clobbering a concurrent writer). Simulate another process
        # completing a successful fetch in between by returning old_cache on the first call and
        # new_cache (the "concurrently written" newer state) on the second -- and, since v3's
        # hysteresis_posture() also reads/writes the cache from inside render() (its own
        # read-for-decision and read-before-write, following this exact same pattern), every call
        # from the third onward delegates to the REAL loader so it sees what's actually on disk,
        # including the 429 write this test's own second call is about to simulate via
        # save_cache(). A fake that kept returning the static `new_cache` literal forever would
        # make hysteresis's own read-before-write clobber the just-written last_429_at back to 0
        # -- a bug in the test double, not in production (which always reads real disk state).
        real_load_cache = mod.load_cache
        calls = {"n": 0}

        def fake_load_cache():
            calls["n"] += 1
            if calls["n"] == 1:
                return dict(old_cache)
            if calls["n"] == 2:
                return dict(new_cache)
            return real_load_cache()

        mod.load_cache = fake_load_cache
        mod.get_access_token = lambda: "dummy-token"
        mod.fetch = lambda token: (429, None)

        # main() parses sys.argv itself; the real argv here is whatever invoked this test run
        # (e.g. "-m unittest discover tests"), so pin it to a benign value for the duration.
        old_argv = sys.argv[:]
        sys.argv = ["check-usage.py"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(SystemExit):
                    mod.main()
        finally:
            sys.argv = old_argv

        # 2 from main() (as before) + 2 from hysteresis_posture()'s own read-for-decision and
        # read-before-write (see hysteresis_posture's docstring) -- exactly one render() call
        # happens on this path (the stale-cache render after the 429 branch).
        self.assertEqual(calls["n"], 4)
        with open(self.cache_path, encoding="utf-8") as f:
            written = json.load(f)

        # The newer data/history from the "concurrent" write must survive; last_429_at was
        # overwritten by this run's 429 branch, and hysteresis's later, separate write must not
        # have clobbered any of that -- only added `posture_state`.
        self.assertEqual(written["data"], new_cache["data"])
        self.assertEqual(written["history"], new_cache["history"])
        self.assertEqual(written["fetched_at"], new_cache["fetched_at"])
        self.assertEqual(written["last_429_at"], mod.NOW)
        self.assertIn("posture_state", written)


class TestSuccessPathPreservesPostureState(CheckUsageTestCase):
    """F1: the 200-success write in main() used to blindly overwrite the cache with a fresh
    {fetched_at, data, last_429_at, history} dict, dropping any stored `posture_state` before
    render() -> hysteresis_posture() ever got to read it back. That made hysteresis inert on
    every network refresh: a run that fetched fresh data past the TTL always saw "no stored
    state" and adopted the computed rung outright, silently discarding a held de-escalation.
    """

    def test_200_success_preserves_stored_posture_state_for_hysteresis(self):
        mod = self.mod
        # Seed a prior hold: posture "conserve" was being reported, recently enough (and with
        # no driver_resets_at) that hysteresis must still be honoring it. No `data`/`fetched_at`
        # so main() takes the network-fetch path rather than serving straight from cache.
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump({
                "v": 1,
                "posture_state": {
                    "posture": "conserve",
                    "at": mod.NOW - 100,
                    "driver_resets_at": None,
                    "pending_downgrade": None,
                },
            }, f)

        mod.get_access_token = lambda: "dummy-token"
        # A calm session limit: both pace axes read "normal" and the downtime cap does nothing
        # (exhaustion projects well past reset), so this run's raw posture_computed is "normal" --
        # far below the held "conserve".
        resets_at = (datetime.now(timezone.utc) + timedelta(seconds=10000)).isoformat()
        data = {"limits": [{
            "kind": "session", "percent": 10.0, "severity": "normal", "is_active": True,
            "resets_at": resets_at,
        }]}
        mod.fetch = lambda token: (200, data)

        old_argv = sys.argv[:]
        sys.argv = ["check-usage.py", "--json"]
        try:
            code, out = self.run_with_exit(mod.main)
        finally:
            sys.argv = old_argv

        self.assertEqual(code, 0)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["posture_computed"], "normal")
        # The held "conserve" must survive this fetch-refresh: hysteresis_posture() has to be
        # able to read it back from the cache the success path just wrote, not find it wiped.
        self.assertEqual(payload["posture"], "conserve")
        self.assertIn(
            "holding conserve pending confirmation (computed normal just now)",
            payload["posture_reason"])

        with open(self.cache_path, encoding="utf-8") as f:
            written = json.load(f)
        # "and vice versa" (spec Sec H): the posture_state write hysteresis makes right after
        # must not clobber the history/last_429_at this same run's success path wrote.
        self.assertEqual(written["last_429_at"], 0)
        self.assertTrue(written["history"])
        self.assertIn("posture_state", written)
        self.assertEqual(written["posture_state"]["posture"], "conserve")
        self.assertEqual(written["posture_state"]["pending_downgrade"]["posture"], "normal")


# --------------------------------------------------------------------------- cache versioning

class TestCacheVersioning(CheckUsageTestCase):
    def test_unversioned_cache_treated_as_empty(self):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": self.mod.NOW, "data": {"limits": []}}, f)
        self.assertEqual(self.mod.load_cache(), {})

    def test_v1_cache_is_honored(self):
        cache = {"v": 1, "fetched_at": self.mod.NOW, "data": {"limits": []}}
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        self.assertEqual(self.mod.load_cache(), cache)

    def test_wrong_version_treated_as_empty(self):
        for bad_v in (0, 2):
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump({"v": bad_v, "fetched_at": self.mod.NOW, "data": {"limits": []}}, f)
            self.assertEqual(self.mod.load_cache(), {})

    def test_saved_cache_carries_v1_on_disk(self):
        self.mod.save_cache({"fetched_at": self.mod.NOW, "data": {"limits": []}})
        with open(self.cache_path, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["v"], 1)

    def test_save_cache_does_not_mutate_caller_dict(self):
        original = {"fetched_at": self.mod.NOW, "data": {"limits": []}}
        self.mod.save_cache(original)
        self.assertNotIn("v", original)

    def test_history_round_trips_through_versioned_cache(self):
        history = [{"at": self.mod.NOW - 1000, "limits": {"session|": 40.0}, "resets": {}}]
        self.mod.save_cache({
            "fetched_at": self.mod.NOW,
            "data": {"limits": []},
            "history": history,
        })
        loaded = self.mod.load_cache()
        self.assertEqual(loaded["v"], 1)
        self.assertEqual(loaded["history"], history)


# --------------------------------------------------------------------------- v3: downtime cap

class TestDowntimeCapBands(CheckUsageTestCase):
    def test_cap_band_edges(self):
        # DOWNTIME_CAP_BANDS = [(900, normal), (3600, measured), (21600, frugal), (86400, conserve)]
        cases = [
            (899, "normal"), (900, "measured"),
            (3599, "measured"), (3600, "frugal"),
            (21599, "frugal"), (21600, "conserve"),
            (86399, "conserve"), (86400, None),
        ]
        for D, expected in cases:
            with self.subTest(D=D):
                self.assertEqual(self.mod._downtime_cap_band(D), expected)


class TestDowntimeInfo(CheckUsageTestCase):
    def test_session_margin_is_optimistic_rate_multiplier(self):
        # margin scales the RATE (exhaust_adjusted = exhaust_raw / margin), verified against the
        # raw pace_info/window_pace_info readings directly rather than re-deriving them.
        l = {"kind": "session", "percent": 50, "resets_in_seconds": 3600,
             "burn": {"delta_percent": 15.15, "over_seconds": 1000}}
        _, burn_exhaust = self.mod.pace_info(l)
        _, _, window_exhaust = self.mod.window_pace_info(l)
        eff, D, cap = self.mod.downtime_info(l)
        expected = min(burn_exhaust, window_exhaust) / self.mod.MARGIN_SESSION_RATE
        self.assertAlmostEqual(eff, expected, places=6)

    def test_pool_kind_margin_is_pessimistic_rate_multiplier(self):
        l = {"kind": "weekly_all", "percent": 50, "resets_in_seconds": 3600,
             "burn": {"delta_percent": 15.15, "over_seconds": 1000}}
        _, burn_exhaust = self.mod.pace_info(l)
        _, _, window_exhaust = self.mod.window_pace_info(l)
        eff, D, cap = self.mod.downtime_info(l)
        expected = min(burn_exhaust, window_exhaust) / self.mod.MARGIN_POOL_RATE
        self.assertAlmostEqual(eff, expected, places=6)

    def test_unknown_kind_gets_pool_margin_not_session_margin(self):
        # No WINDOW_LENGTHS entry for an unfamiliar kind, so only the (kind-agnostic) burn axis
        # contributes; the margin applied to it must be MARGIN_POOL_RATE, never
        # MARGIN_SESSION_RATE, since nothing is known about the kind's shape.
        l = {"kind": "mystery_meter", "percent": 50, "resets_in_seconds": 3600,
             "burn": {"delta_percent": 15.15, "over_seconds": 1000}}
        _, burn_exhaust = self.mod.pace_info(l)
        eff, D, cap = self.mod.downtime_info(l)
        self.assertAlmostEqual(eff, burn_exhaust / self.mod.MARGIN_POOL_RATE, places=6)

    def test_optimistic_session_margin_pushes_exhaust_later_than_pool(self):
        # Same raw pace/exhaust math, different kind: session's 0.85 divides by LESS than one
        # (later exhaust, less downtime cost); the pool's 1.15 divides by MORE than one (earlier
        # exhaust, more downtime cost).
        l_session = {"kind": "session", "percent": 50, "resets_in_seconds": 3600,
                     "burn": {"delta_percent": 15.15, "over_seconds": 1000}}
        l_pool = {"kind": "weekly_all", "percent": 50, "resets_in_seconds": 3600,
                  "burn": {"delta_percent": 15.15, "over_seconds": 1000}}
        eff_session, D_session, _ = self.mod.downtime_info(l_session)
        eff_pool, D_pool, _ = self.mod.downtime_info(l_pool)
        self.assertGreater(eff_session, eff_pool)
        self.assertLess(D_session, D_pool)

    def test_d_zero_session_margin_pushes_exhaust_past_reset(self):
        # Single axis, not two: window headroom is 2.5 ('normal'), well below 'measured', so it
        # never wins the worst-of-two-pace-readings provisional -- that's burn alone (ratio ~1.09
        # -> 'measured') -- and its own exhaust (14400s) is nowhere near the earliest-of-two-axes
        # race the cap runs either (burn's 3300s wins that too). The real mechanism is the
        # OPTIMISTIC session margin: raw pace_info identity guarantees exhaust < resets_in
        # whenever ratio >= 1.0 (ratio == resets_in / exhaust), so D = resets_in - exhaust can't
        # be pushed to 0 by ratio alone on a single axis -- but the margin divides exhaust by 0.85
        # (i.e. multiplies it by ~1.176) AFTER that identity holds, so any ratio in [1.0, 1/0.85)
        # -- here ~1.09 -- still lands its margin-adjusted exhaust (~3883s) past the 3600s reset,
        # clamping D to 0 and capping the 'measured' provisional down to 'normal'. This falsifies
        # the spec's "cannot happen on one axis" premise (claude-usage-v3-spec.md Sec A) for
        # `session`: that premise holds for the raw ratio identity, but not once the margin is
        # layered on top of it, and only session's margin (< 1) can push exhaust the wrong way
        # relative to the identity that motivated the premise -- the pool margin (> 1) only ever
        # pulls exhaust earlier, reinforcing D > 0 rather than erasing it.
        l = {"kind": "session", "percent": 50, "resets_in_seconds": 3600,
             "burn": {"delta_percent": 15.15, "over_seconds": 1000}}
        eff, D, cap = self.mod.downtime_info(l)
        self.assertGreater(eff, 3600)
        self.assertEqual(D, 0.0)
        self.assertEqual(cap, "normal")
        self.assertEqual(self.mod.limit_posture(l)[0], "normal")

    def test_d_zero_direct_on_cap_function(self):
        self.assertEqual(self.mod._downtime_cap_band(0), "normal")

    def test_no_projection_on_either_axis_yields_no_cap(self):
        # No burn baseline and an unknown kind (no window length): neither axis has an exhaustion
        # projection, so there's nothing to cap against.
        l = {"kind": "mystery_meter", "percent": 50, "resets_in_seconds": 3600, "burn": None}
        self.assertEqual(self.mod.downtime_info(l), (None, None, None))

    def test_non_positive_or_missing_resets_in_yields_no_cap(self):
        for reset_s in (None, 0, -5):
            with self.subTest(reset_s=reset_s):
                l = {"kind": "session", "percent": 50, "resets_in_seconds": reset_s,
                     "burn": {"delta_percent": 15.15, "over_seconds": 1000}}
                self.assertEqual(self.mod.downtime_info(l), (None, None, None))

    def test_cap_never_raises_a_normal_provisional(self):
        # Sustainable pace (ratio well under 1.0) -> provisional 'normal' on both axes. D happens
        # to clamp to 0 here too (tiny reset window), so cap is 'normal' -- equal rank, not lower,
        # so no suffix and no rung change. There is nothing for the cap to raise 'normal' TO.
        l = {"kind": "session", "percent": 10, "resets_in_seconds": 500,
             "burn": {"delta_percent": 10.0, "over_seconds": 1000}}
        ratio, _ = self.mod.pace_info(l)
        self.assertLess(ratio, 1.0)
        posture, reason = self.mod.limit_posture(l)
        self.assertEqual(posture, "normal")
        self.assertNotIn("capped at", reason or "")

    def test_reason_gains_suffix_when_cap_actually_lowers_the_rung(self):
        l = {"kind": "session", "percent": 20, "resets_in_seconds": 15000,
             "burn": {"delta_percent": 8.0, "over_seconds": 1000}}
        posture, reason = self.mod.limit_posture(l)
        self.assertEqual(posture, "measured")
        self.assertIn("capped at measured", reason)
        self.assertIn("downtime before reset", reason)

    def test_reason_has_no_suffix_when_cap_does_not_lower_the_rung(self):
        # Front-loaded sprint (40% by day 1): D is huge (~4.7 days), well beyond every cap band,
        # so downtime_info's cap is None and the provisional 'measured' rung stands untouched.
        l = {"kind": "weekly_all", "percent": 40, "resets_in_seconds": 518400, "burn": None}
        posture, reason = self.mod.limit_posture(l)
        self.assertEqual(posture, "measured")
        _, _, cap = self.mod.downtime_info(l)
        self.assertIsNone(cap)
        self.assertNotIn("capped at", reason)

    def test_unknown_kind_with_burn_gets_provisional_rung_and_pessimistic_cap(self):
        # Closes the v2 gap: an unfamiliar kind gets no window pace (no WINDOW_LENGTHS entry), but
        # burn pace is kind-agnostic, so it still gets a provisional rung off pace alone, and the
        # v3 downtime cap (pessimistic pool margin) still governs it.
        l = {"kind": "mystery_meter", "percent": 30, "resets_in_seconds": 5000,
             "burn": {"delta_percent": 40.0, "over_seconds": 1000}}
        posture, reason = self.mod.limit_posture(l)
        self.assertNotEqual(posture, "normal")
        self.assertIn("capped at", reason)
        eff, D, cap = self.mod.downtime_info(l)
        _, burn_exhaust = self.mod.pace_info(l)
        self.assertAlmostEqual(eff, burn_exhaust / self.mod.MARGIN_POOL_RATE, places=6)


class TestRunwayAndImminentFields(CheckUsageTestCase):
    def test_null_projection_gives_null_runway_and_false_imminent(self):
        mod = self.mod
        l = {"kind": "usage_credit", "percent": None, "resets_at": None,
             "resets_in_seconds": None, "severity": None, "scope_model": None,
             "is_active": False, "extra": {"balance": 10}}
        code, out = self.run_with_exit(mod.render, [l], 0, False, "cache")
        payload = json.loads(out.strip().splitlines()[-1])
        lim = payload["limits"][0]
        self.assertIsNone(lim["runway_seconds"])
        self.assertFalse(lim["imminent"])
        self.assertIsNone(lim["downtime"])

    def test_imminent_true_when_runway_short_and_before_reset(self):
        mod = self.mod
        # Baseline via HISTORY so compute_burn derives the burn (render() overwrites any
        # pre-populated 'burn' key on the limit dict itself).
        mod.HISTORY = [{"at": mod.NOW - 1000, "limits": {"session|": 80.0}, "resets": {}}]
        l = {"kind": "session", "percent": 90.0, "resets_at": "R", "resets_in_seconds": 1650,
             "severity": None, "scope_model": None, "is_active": False, "extra": None}
        code, out = self.run_with_exit(mod.render, [l], 0, False, "cache")
        payload = json.loads(out.strip().splitlines()[-1])
        lim = payload["limits"][0]
        self.assertIsNotNone(lim["runway_seconds"])
        self.assertLess(lim["runway_seconds"], mod.IMMINENT_RUNWAY_S)
        self.assertTrue(lim["imminent"])
        self.assertIsNotNone(lim["downtime"])
        self.assertIn("seconds", lim["downtime"])
        self.assertIn("cap", lim["downtime"])

    def test_imminent_false_when_runway_present_but_beyond_threshold(self):
        mod = self.mod
        mod.HISTORY = []
        l = {"kind": "weekly_all", "percent": 40, "resets_at": "R",
             "resets_in_seconds": 518400, "severity": None, "scope_model": None,
             "is_active": False, "extra": None}
        code, out = self.run_with_exit(mod.render, [l], 0, False, "cache")
        payload = json.loads(out.strip().splitlines()[-1])
        lim = payload["limits"][0]
        self.assertIsNotNone(lim["runway_seconds"])
        self.assertGreaterEqual(lim["runway_seconds"], mod.IMMINENT_RUNWAY_S)
        self.assertFalse(lim["imminent"])

    def test_imminent_boundary_matches_rounded_runway_seconds(self):
        # F9: `imminent` must band the same ROUNDED figure emitted as `runway_seconds`, not the
        # raw float underneath it -- otherwise a raw 1799.6s (< 1800 threshold) prints
        # runway_seconds: 1800 (rounds up) right next to imminent: true, which contradicts its own
        # displayed number. Force exhaust_eff via a stub so the boundary is exact and deterministic
        # rather than depending on burn/percent algebra landing on the edge by coincidence.
        mod = self.mod
        l = {"kind": "session", "percent": 50.0, "resets_at": "R", "resets_in_seconds": 5000,
             "severity": None, "scope_model": None, "is_active": False, "extra": None}

        mod.downtime_info = lambda limit: (1799.4, 100.0, "measured")
        code, out = self.run_with_exit(mod.render, [dict(l)], 0, False, "cache")
        lim = json.loads(out.strip().splitlines()[-1])["limits"][0]
        self.assertEqual(lim["runway_seconds"], 1799)
        self.assertTrue(lim["imminent"])

        mod.downtime_info = lambda limit: (1799.6, 100.0, "measured")
        code, out = self.run_with_exit(mod.render, [dict(l)], 0, False, "cache")
        lim = json.loads(out.strip().splitlines()[-1])["limits"][0]
        self.assertEqual(lim["runway_seconds"], 1800)
        self.assertFalse(lim["imminent"])

    def test_human_output_prints_imminent_warning_line(self):
        mod = self.mod
        mod.HISTORY = [{"at": mod.NOW - 1000, "limits": {"session|": 80.0}, "resets": {}}]
        l = {"kind": "session", "percent": 90.0, "resets_at": "R", "resets_in_seconds": 1650,
             "severity": None, "scope_model": None, "is_active": False, "extra": None}
        code, out = self.run_with_exit(mod.render, [l], 0, False, "cache")
        self.assertIn("checkpoint now", out)

    def test_human_output_prints_surplus_line(self):
        mod = self.mod
        mod.HISTORY = []
        length = mod.WINDOW_LENGTHS["weekly_all"]
        l = {"kind": "weekly_all", "percent": 30.0, "resets_at": "R",
             "resets_in_seconds": length // 2, "severity": None, "scope_model": None,
             "is_active": False, "extra": None}
        code, out = self.run_with_exit(mod.render, [l], 0, False, "cache")
        self.assertIn("surplus:", out)
        self.assertIn("may go unspent", out)


class TestV3FieldsPresentOnEveryRenderPath(CheckUsageTestCase):
    def test_new_fields_present_regardless_of_stale_or_source(self):
        mod = self.mod
        for stale, source in [(False, "network"), (False, "cache"),
                              (True, "cache (429 backoff)"), (True, "cache (fetch failed)")]:
            with self.subTest(stale=stale, source=source):
                mod.HISTORY = []
                limits = [{"kind": "weekly_all", "percent": 40.0, "resets_at": "R",
                          "resets_in_seconds": 518400, "severity": None, "scope_model": None,
                          "is_active": False, "extra": None}]
                code, out = self.run_with_exit(mod.render, limits, 0, stale, source)
                payload = json.loads(out.strip().splitlines()[-1])
                self.assertEqual(payload["stale"], stale)
                self.assertEqual(payload["source"], source)
                self.assertIn("posture_computed", payload)
                lim = payload["limits"][0]
                for key in ("downtime", "runway_seconds", "imminent", "surplus",
                            "posture", "pace_judgeable", "color"):
                    self.assertIn(key, lim)


class TestPerLimitColors(CheckUsageTestCase):
    def _render_limit(self, limit, stale=False):
        self.mod.HISTORY = []
        code, out = self.run_with_exit(self.mod.render, [limit], 0, stale, "cache-only")
        self.assertEqual(code, 0)
        return json.loads(out.strip().splitlines()[-1])["limits"][0]

    def test_all_five_posture_colors(self):
        length = self.mod.WINDOW_LENGTHS["weekly_all"]
        cases = [
            (10, int(length * 0.5), "normal", "green"),
            (40, int(length * 6 / 7), "measured", "blue"),
            (76, int(length * 0.5), "frugal", "yellow"),
            (90, int(length * 23 / 24), "conserve", "orange"),
            (100, int(length * 0.5), "wind_down", "red"),
        ]
        for percent, resets_in, posture, color in cases:
            with self.subTest(posture=posture):
                limit = {"kind": "weekly_all", "percent": percent, "resets_at": "R",
                         "resets_in_seconds": resets_in, "severity": None,
                         "scope_model": None, "is_active": False, "extra": None}
                rendered = self._render_limit(limit)
                self.assertEqual(rendered["posture"], posture)
                self.assertEqual(rendered["color"], color)

    def test_unavailable_and_unjudgeable_stale_are_gray(self):
        unavailable = {"kind": "usage_credit", "percent": None, "resets_at": None,
                       "resets_in_seconds": None, "severity": None, "scope_model": None,
                       "is_active": False, "extra": None}
        self.assertEqual(self._render_limit(unavailable)["color"], "gray")
        stale = dict(unavailable, kind="mystery", percent=50)
        self.assertEqual(self._render_limit(stale, stale=True)["color"], "gray")

    def test_limits_are_classified_independently(self):
        length = self.mod.WINDOW_LENGTHS["weekly_all"]
        limits = [
            {"kind": "session", "percent": 10, "resets_at": "R",
             "resets_in_seconds": 9000, "severity": None, "scope_model": None,
             "is_active": False, "extra": None},
            {"kind": "weekly_all", "percent": 100, "resets_at": "R",
             "resets_in_seconds": length // 2, "severity": None, "scope_model": None,
             "is_active": False, "extra": None},
        ]
        self.mod.HISTORY = []
        _, out = self.run_with_exit(self.mod.render, limits, 0, False, "cache-only")
        rendered = json.loads(out.strip().splitlines()[-1])["limits"]
        self.assertEqual([limit["color"] for limit in rendered], ["green", "red"])


class TestExhaustedAndUnjudgeableLimits(CheckUsageTestCase):
    def _payload(self, limit, stale=False):
        self.mod.HISTORY = []
        code, out = self.run_with_exit(
            self.mod.render, [limit], 0, stale, "cache-only")
        self.assertEqual(code, 0)
        return json.loads(out.strip().splitlines()[-1])

    @staticmethod
    def _limit(kind="session", percent=100, resets_in=300, resets_at="R"):
        return {"kind": kind, "percent": percent, "resets_at": resets_at,
                "resets_in_seconds": resets_in, "severity": None,
                "scope_model": None, "is_active": False, "extra": None}

    def test_exhausted_limit_is_always_wind_down_and_red(self):
        for kind, percent, resets_in in (
                ("session", 100, 300),
                ("session", 100, 4 * 3600),
                ("session", 99.6, 300),
                ("session", 100, None),
                ("weekly_all", 100, 2 * 86400),
                ("mystery", 100, 300)):
            with self.subTest(kind=kind, percent=percent, resets_in=resets_in):
                payload = self._payload(self._limit(kind, percent, resets_in))
                limit = payload["limits"][0]
                self.assertEqual(limit["posture"], "wind_down")
                self.assertEqual(limit["color"], "red")
                self.assertIn("limit exhausted", payload["posture_reason"])

    def test_exhaustion_rounding_boundary_does_not_capture_99_4(self):
        limit = self._limit(percent=99.4, resets_in=300)
        self.assertFalse(self.mod.is_exhausted(limit))
        self.assertNotEqual(self.mod.limit_posture(limit)[1], "99% spent: limit exhausted")

    def test_unknown_fresh_unjudgeable_limit_is_gray(self):
        payload = self._payload(self._limit("mystery", 60, 300))
        limit = payload["limits"][0]
        self.assertFalse(limit["pace_judgeable"])
        self.assertEqual(limit["color"], "gray")

    def test_reset_outside_known_window_is_gray_with_truthful_reason(self):
        length = self.mod.WINDOW_LENGTHS["weekly_all"]
        payload = self._payload(self._limit("weekly_all", 60, length + 1))
        limit = payload["limits"][0]
        self.assertFalse(limit["pace_judgeable"])
        self.assertEqual(limit["color"], "gray")
        self.assertIn("reset outside window length", payload["posture_reason"])
        self.assertNotIn("unknown kind", payload["posture_reason"])

    def test_missing_percent_has_truthful_unjudgeable_reason(self):
        payload = self._payload(self._limit("session", None, 300))
        self.assertIn("no percent to judge", payload["posture_reason"])

    def test_just_reset_window_has_truthful_unjudgeable_reason(self):
        length = self.mod.WINDOW_LENGTHS["session"]
        payload = self._payload(self._limit("session", 0, length))
        self.assertIn("window just reset", payload["posture_reason"])

    def test_stale_expired_window_at_100_is_still_red(self):
        payload = self._payload(self._limit(percent=100, resets_in=-500), stale=True)
        self.assertEqual(payload["limits"][0]["color"], "red")

    def test_exhausted_limit_drives_healthy_limit(self):
        exhausted = self._limit("session", 100, 300)
        healthy = self._limit("weekly_all", 10, self.mod.WINDOW_LENGTHS["weekly_all"] // 2)
        posture, driver, reason = self.mod.overall_posture([healthy, exhausted])
        self.assertEqual(posture, "wind_down")
        self.assertIs(driver, exhausted)
        self.assertIn("limit exhausted", reason)

    def test_exhausted_limit_wins_tie_with_pace_driven_wind_down(self):
        exhausted = self._limit("session", 100, 300)
        paced = self._limit("weekly_all", 99, self.mod.WINDOW_LENGTHS["weekly_all"] // 2)
        posture, driver, reason = self.mod.overall_posture([paced, exhausted])
        self.assertEqual(posture, "wind_down")
        self.assertIs(driver, exhausted)
        self.assertIn("limit exhausted", reason)

    def test_hysteresis_releases_exhausted_driver_after_its_real_reset(self):
        reset = datetime.fromtimestamp(self.mod.NOW - 5, timezone.utc).isoformat()
        first = self._payload(self._limit(percent=100, resets_in=-5, resets_at=reset))
        self.assertEqual(first["posture"], "wind_down")
        second = self._payload(self._limit(percent=10, resets_in=9000, resets_at=reset))
        self.assertEqual(second["posture"], "normal")


class TestCacheOnlyMode(CheckUsageTestCase):
    def _run_main(self):
        old_argv = sys.argv[:]
        sys.argv = ["check-usage.py", "--json", "--cache-only"]
        try:
            return self.run_with_exit(self.mod.main)
        finally:
            sys.argv = old_argv

    def test_cache_only_is_mechanically_network_and_credential_free(self):
        reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.mod.save_cache({
            "fetched_at": self.mod.NOW - 60,
            "data": {"limits": [{"kind": "session", "percent": 25,
                                  "resets_at": reset}]},
            "history": [],
        })
        with open(self.cache_path, "rb") as handle:
            before = handle.read()
        self.mod.get_access_token = lambda: self.fail("credentials were loaded")
        self.mod.fetch = lambda token: self.fail("network was called")
        code, out = self._run_main()
        self.assertEqual(code, 0)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(payload["available"])
        self.assertEqual(payload["source"], "cache-only")
        with open(self.cache_path, "rb") as handle:
            self.assertEqual(before, handle.read())

    def test_cache_only_missing_or_malformed_cache_is_unavailable_without_network(self):
        self.mod.get_access_token = lambda: self.fail("credentials were loaded")
        self.mod.fetch = lambda token: self.fail("network was called")
        for content in (None, "not json", json.dumps({"v": 1, "data": {"bad": True}})):
            with self.subTest(content=content):
                if content is None:
                    if os.path.exists(self.cache_path):
                        os.unlink(self.cache_path)
                else:
                    with open(self.cache_path, "w", encoding="utf-8") as handle:
                        handle.write(content)
                code, out = self._run_main()
                self.assertEqual(code, 2)
                self.assertFalse(json.loads(out.strip().splitlines()[-1])["available"])

    def test_cache_only_expired_window_is_stale_and_gray(self):
        reset = datetime.fromtimestamp(self.mod.NOW - 500, timezone.utc).isoformat()
        self.mod.save_cache({
            "fetched_at": self.mod.NOW - 1000,
            "data": {"limits": [{"kind": "session", "percent": 75,
                                  "resets_at": reset}]},
            "history": [],
        })
        code, out = self._run_main()
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertTrue(payload["stale"])
        self.assertEqual("gray", payload["limits"][0]["color"])

    def test_cache_only_old_but_still_judgeable_data_is_explicitly_stale(self):
        reset = datetime.fromtimestamp(self.mod.NOW + 3600, timezone.utc).isoformat()
        self.mod.save_cache({
            "fetched_at": self.mod.NOW - self.mod.TTL - 1,
            "data": {"limits": [{"kind": "session", "percent": 25,
                                  "resets_at": reset}]},
            "history": [],
        })
        code, out = self._run_main()
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertTrue(payload["stale"])
        self.assertTrue(payload["limits"][0]["pace_judgeable"])


class TestAuthorizationErrorFallbacks(CheckUsageTestCase):
    def _run_status(self, status):
        reset = datetime.fromtimestamp(self.mod.NOW + 3600, timezone.utc).isoformat()
        self.mod.save_cache({
            "fetched_at": self.mod.NOW - self.mod.TTL - 1,
            "data": {"limits": [{"kind": "session", "percent": 25,
                                  "resets_at": reset}]},
            "history": [],
        })
        self.mod.get_access_token = lambda: "dummy"
        self.mod.fetch = lambda token: (status, None)
        old_argv = sys.argv[:]
        sys.argv = ["check-usage.py", "--json"]
        try:
            return self.run_with_exit(self.mod.main)
        finally:
            sys.argv = old_argv

    def test_401_serves_stale_cache(self):
        code, out = self._run_status(401)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertTrue(payload["stale"])
        self.assertIn("token stale", payload["source"])

    def test_403_serves_stale_cache_and_arms_backoff(self):
        code, out = self._run_status(403)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertTrue(payload["stale"])
        self.assertIn("HTTP 403", payload["source"])
        self.assertEqual(self.mod.NOW, self.mod.load_cache()["last_429_at"])


# --------------------------------------------------------------------------- v3: surplus

class TestSurplusInfo(CheckUsageTestCase):
    def _limit(self, kind, percent, resets_in):
        return {"kind": kind, "percent": percent, "resets_in_seconds": resets_in}

    def test_session_excluded(self):
        self.assertIsNone(self.mod.surplus_info(self._limit("session", 10, 9000)))

    def test_unknown_kind_excluded(self):
        self.assertIsNone(self.mod.surplus_info(self._limit("mystery_meter", 10, 9000)))

    def test_weekly_scoped_included(self):
        length = self.mod.WINDOW_LENGTHS["weekly_scoped"]
        self.assertIsNotNone(self.mod.surplus_info(self._limit("weekly_scoped", 30, length // 2)))

    def test_below_min_elapsed_fraction_excluded(self):
        length = self.mod.WINDOW_LENGTHS["weekly_all"]
        resets_in = int(length * 0.51)  # elapsed_fraction ~0.49, just under the 0.5 floor
        self.assertIsNone(self.mod.surplus_info(self._limit("weekly_all", 10, resets_in)))

    def test_missing_percent_or_bad_reset_bounds_excluded(self):
        length = self.mod.WINDOW_LENGTHS["weekly_all"]
        self.assertIsNone(self.mod.surplus_info(
            {"kind": "weekly_all", "percent": None, "resets_in_seconds": length // 2}))
        # resets_in_seconds beyond the window length: a clock-skew/window-redefinition
        # contradiction, same guard window_pace_info uses.
        self.assertIsNone(self.mod.surplus_info(
            {"kind": "weekly_all", "percent": 10, "resets_in_seconds": length + 100}))
        self.assertIsNone(self.mod.surplus_info(
            {"kind": "weekly_all", "percent": 10, "resets_in_seconds": 0}))

    def test_at_min_elapsed_fraction_boundary_included(self):
        length = self.mod.WINDOW_LENGTHS["weekly_all"]
        resets_in = length // 2  # elapsed_fraction exactly 0.5
        self.assertIsNotNone(self.mod.surplus_info(self._limit("weekly_all", 20, resets_in)))

    def test_projected_final_at_75_excluded(self):
        length = self.mod.WINDOW_LENGTHS["weekly_all"]
        resets_in = length // 2
        # 37.5% spent at 50% elapsed -> projected_final exactly 75.0 -> excluded (< 75.0, not <=).
        self.assertIsNone(self.mod.surplus_info(self._limit("weekly_all", 37.5, resets_in)))

    def test_projected_final_just_under_75_included(self):
        length = self.mod.WINDOW_LENGTHS["weekly_all"]
        resets_in = length // 2
        result = self.mod.surplus_info(self._limit("weekly_all", 37.0, resets_in))
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["projected_final_percent"], 74.0, places=1)
        self.assertAlmostEqual(result["unspent_percent"], 26.0, places=1)

    def test_cap_at_100_never_crashes_and_stays_excluded(self):
        # percent/elapsed_fraction can exceed 100 (e.g. near-full budget with little time left);
        # min(100, ...) must clamp it safely rather than emit a >100 value or divide oddly. Any
        # raw value above 100 is also above the 75.0 surplus ceiling, so this always reads
        # 'not a surplus' -- verifying it does so safely, not incorrectly.
        length = self.mod.WINDOW_LENGTHS["weekly_all"]
        result = self.mod.surplus_info(self._limit("weekly_all", 95, int(length * 0.6)))
        self.assertIsNone(result)

    def test_never_moves_posture(self):
        length = self.mod.WINDOW_LENGTHS["weekly_all"]
        l = self._limit("weekly_all", 37.0, length // 2)
        self.assertIsNotNone(self.mod.surplus_info(l))
        posture, _ = self.mod.limit_posture(l)
        self.assertEqual(posture, "normal")  # under-spend never escalates


# --------------------------------------------------------------------------- v3: posture hysteresis

class TestHysteresisPosture(CheckUsageTestCase):
    def _seed_state(self, **overrides):
        state = {"posture": "frugal", "at": self.mod.NOW - 10,
                 "driver_resets_at": None, "pending_downgrade": None}
        state.update(overrides)
        self.mod.save_cache({"fetched_at": self.mod.NOW, "data": {"limits": []},
                             "posture_state": state})
        return state

    def test_no_stored_state_adopts_computed(self):
        posture, note = self.mod.hysteresis_posture("frugal", None)
        self.assertEqual(posture, "frugal")
        self.assertIsNone(note)

    def test_escalation_adopts_immediately(self):
        self._seed_state(posture="measured", at=self.mod.NOW - 10)
        posture, note = self.mod.hysteresis_posture("wind_down", None)
        self.assertEqual(posture, "wind_down")
        self.assertIsNone(note)

    def test_tie_keeps_computed_and_clears_pending(self):
        self._seed_state(posture="frugal", at=self.mod.NOW - 10,
                         pending_downgrade={"posture": "measured", "first_at": self.mod.NOW - 500})
        posture, note = self.mod.hysteresis_posture("frugal", None)
        self.assertEqual(posture, "frugal")
        self.assertIsNone(note)
        with open(self.cache_path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertIsNone(written["posture_state"]["pending_downgrade"])

    def test_first_sighting_of_downgrade_holds_stored_and_records_pending(self):
        self._seed_state(posture="conserve", at=self.mod.NOW - 10, pending_downgrade=None)
        posture, note = self.mod.hysteresis_posture("measured", None)
        self.assertEqual(posture, "conserve")
        self.assertIn("holding conserve", note)
        self.assertIn("computed measured", note)
        with open(self.cache_path, encoding="utf-8") as f:
            written = json.load(f)
        pending = written["posture_state"]["pending_downgrade"]
        self.assertIsNotNone(pending)
        self.assertEqual(pending["posture"], "measured")

    def test_downgrade_confirmed_after_120s_adopts_computed(self):
        first_at = self.mod.NOW - 130  # >= HYSTERESIS_CONFIRM_S (120) ago
        self._seed_state(posture="conserve", at=self.mod.NOW - 10,
                         pending_downgrade={"posture": "frugal", "first_at": first_at})
        # Confirmation only requires "still below stored rank", not a match to the originally
        # pending rung -- 'measured' (also below 'conserve') at >=120s since first_at confirms.
        posture, note = self.mod.hysteresis_posture("measured", None)
        self.assertEqual(posture, "measured")
        self.assertIsNone(note)

    def test_downgrade_not_yet_confirmed_still_holds(self):
        first_at = self.mod.NOW - 50  # < 120s ago
        self._seed_state(posture="conserve", at=self.mod.NOW - 10,
                         pending_downgrade={"posture": "frugal", "first_at": first_at})
        posture, note = self.mod.hysteresis_posture("frugal", None)
        self.assertEqual(posture, "conserve")
        self.assertIsNotNone(note)
        with open(self.cache_path, encoding="utf-8") as f:
            written = json.load(f)
        # first_at must not reset on a second below-stored run before confirmation.
        self.assertEqual(written["posture_state"]["pending_downgrade"]["first_at"], first_at)

    def test_intervening_reescalation_clears_pending(self):
        first_at = self.mod.NOW - 50
        self._seed_state(posture="conserve", at=self.mod.NOW - 10,
                         pending_downgrade={"posture": "frugal", "first_at": first_at})
        # Recovers to >= stored rank -- clears pending even though not yet confirmed.
        posture, note = self.mod.hysteresis_posture("conserve", None)
        self.assertEqual(posture, "conserve")
        self.assertIsNone(note)
        with open(self.cache_path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertIsNone(written["posture_state"]["pending_downgrade"])
        # A later below-stored run must start a FRESH first_at, not resurrect the cleared one.
        posture2, note2 = self.mod.hysteresis_posture("frugal", None)
        self.assertEqual(posture2, "conserve")
        with open(self.cache_path, encoding="utf-8") as f:
            written2 = json.load(f)
        self.assertNotEqual(written2["posture_state"]["pending_downgrade"]["first_at"], first_at)

    def test_max_age_bypass_adopts_computed(self):
        self._seed_state(posture="wind_down", at=self.mod.NOW - 2000)  # > HYSTERESIS_MAX_AGE_S
        posture, note = self.mod.hysteresis_posture("normal", None)
        self.assertEqual(posture, "normal")
        self.assertIsNone(note)

    def test_driver_reset_bypass_adopts_computed(self):
        self._seed_state(posture="wind_down", at=self.mod.NOW - 10,
                         driver_resets_at=self.mod.NOW - 5)  # already reset
        posture, note = self.mod.hysteresis_posture("normal", None)
        self.assertEqual(posture, "normal")
        self.assertIsNone(note)

    def test_unreadable_state_falls_back_to_computed(self):
        with open(self.cache_path, "w", encoding="utf-8") as f:
            f.write("not json")
        posture, note = self.mod.hysteresis_posture("frugal", None)
        self.assertEqual(posture, "frugal")
        self.assertIsNone(note)

    def test_malformed_posture_state_falls_back_to_computed(self):
        self.mod.save_cache({"fetched_at": self.mod.NOW, "data": {"limits": []},
                             "posture_state": {"posture": "not-a-real-rung", "at": self.mod.NOW}})
        posture, note = self.mod.hysteresis_posture("frugal", None)
        self.assertEqual(posture, "frugal")
        self.assertIsNone(note)

    def test_load_failure_falls_back_to_computed(self):
        # load_cache() is already best-effort (returns {} on any read failure); simulate that
        # contract directly rather than corrupting disk state a second way.
        self.mod.load_cache = lambda: {}
        posture, note = self.mod.hysteresis_posture("frugal", None)
        self.assertEqual(posture, "frugal")
        self.assertIsNone(note)

    def test_driver_resets_at_recorded_from_driver(self):
        driver = {"resets_at": "2099-01-01T00:00:00+00:00"}
        self.mod.hysteresis_posture("frugal", driver)
        with open(self.cache_path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertIsNotNone(written["posture_state"]["driver_resets_at"])

    def test_malformed_driver_resets_at_and_pending_downgrade_ignored(self):
        # _valid_posture_state's per-field defensive parse: a malformed driver_resets_at or
        # pending_downgrade must not invalidate the whole state (posture/at are still fine), it
        # just normalizes that one field to None/absent, same discipline as every other
        # user-corruptible cache field this script reads back (see compute_burn's HISTORY guards).
        result = self.mod._valid_posture_state({
            "posture": "frugal", "at": self.mod.NOW, "driver_resets_at": "not-a-number",
            "pending_downgrade": {"posture": "not-a-real-rung", "first_at": self.mod.NOW},
        })
        self.assertIsNotNone(result)
        self.assertEqual(result["posture"], "frugal")
        self.assertIsNone(result["driver_resets_at"])
        self.assertIsNone(result["pending_downgrade"])

    def test_persist_handles_non_dict_load_cache_return(self):
        # load_cache() always returns a dict by contract, but hysteresis_posture's internal
        # _persist() defends against a non-dict return anyway (belt-and-suspenders, matching the
        # rest of this file's "every cache read is treated as corruptible" discipline). Exercise
        # it directly by monkeypatching load_cache the same way other tests in this class do.
        self.mod.load_cache = lambda: None
        posture, note = self.mod.hysteresis_posture("frugal", None)
        self.assertEqual(posture, "frugal")
        self.assertIsNone(note)
        with open(self.cache_path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written["posture_state"]["posture"], "frugal")


class TestHysteresisIntegrationWithRender(CheckUsageTestCase):
    def test_posture_computed_emitted_and_reason_suffix_while_holding(self):
        mod = self.mod
        mod.save_cache({"fetched_at": mod.NOW, "data": {"limits": []},
                        "posture_state": {"posture": "conserve", "at": mod.NOW - 10,
                                          "driver_resets_at": None,
                                          "pending_downgrade": {"posture": "normal",
                                                                "first_at": mod.NOW - 10}}})
        # provisional/computed 'normal': plenty of headroom, half the window elapsed.
        limits = [{"kind": "session", "percent": 10.0, "resets_at": "R",
                  "resets_in_seconds": 9000, "severity": None, "scope_model": None,
                  "is_active": False, "extra": None}]
        code, out = self.run_with_exit(mod.render, limits, 0, False, "cache")
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["posture"], "conserve")             # held
        self.assertEqual(payload["posture_computed"], "normal")      # this run's raw reading
        self.assertIn("holding conserve", payload["posture_reason"])
        self.assertIn("computed normal", payload["posture_reason"])


class TestPostureStateClobberPreservation(CheckUsageTestCase):
    def test_posture_state_write_preserves_concurrent_429_and_history(self):
        # Symmetric to Test429ClobberPreservation: a write to `posture_state` must not clobber a
        # concurrently-written `last_429_at`/`history`, and vice versa -- both follow the same
        # read-before-write, atomic-replace pattern.
        mod = self.mod
        mod.save_cache({"fetched_at": mod.NOW - 100, "data": {"limits": []},
                        "last_429_at": 0, "history": []})
        real_load_cache = mod.load_cache
        calls = {"n": 0}

        def fake_load_cache():
            calls["n"] += 1
            if calls["n"] == 2:
                # A concurrent process completes a 429 write between hysteresis_posture's own
                # read-for-decision (call 1) and its read-before-write (call 2's return, used to
                # decide; the actual write happens after this call returns).
                concurrent = real_load_cache()
                concurrent["last_429_at"] = mod.NOW
                concurrent["history"] = [{"at": mod.NOW - 10, "limits": {"session|": 5},
                                          "resets": {}}]
                mod.save_cache(concurrent)
            return real_load_cache()

        mod.load_cache = fake_load_cache
        posture, note = mod.hysteresis_posture("frugal", None)
        self.assertEqual(posture, "frugal")
        with open(self.cache_path, encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written["last_429_at"], mod.NOW)
        self.assertEqual(len(written["history"]), 1)
        self.assertIn("posture_state", written)
        self.assertEqual(written["posture_state"]["posture"], "frugal")


# --------------------------------------------------------------------------- import side effects

class TestImportSideEffects(CheckUsageTestCase):
    def test_import_does_not_inspect_argv_or_exit(self):
        # A bogus sys.argv (as unittest's own argv often is) must not affect module import.
        old_argv = sys.argv[:]
        sys.argv = ["check-usage.py", "--totally-bogus-flag"]
        try:
            mod = load_module({
                "CLAUDE_USAGE_CACHE": os.path.join(self.tmpdir, "other-cache.json"),
                "CLAUDE_CONFIG_DIR": self.tmpdir,
            })
            self.assertFalse(mod.JSON_ONLY)
            self.assertFalse(mod.FORCE_FRESH)
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
