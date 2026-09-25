#!/usr/bin/env python3
"""Posture table-test for check-usage.py (quiet-lynx, 2026-08-11; extended for v3).

The rule under test: posture reacts to PACE, never to percent, for every window
kind. Percent is a display fact. Before this, `limit_posture` took
_worst(percent_posture, pace_posture), so a weekly at 97% went wind_down while
pace read normal — which halted six healthy runners on 2026-08-11.

Both windows must get identical treatment; any row that passes for `session`
and fails for `weekly_all` at the same pace is a per-kind special case.

v3 adds one exception to "identical pace, identical kind -> identical rung",
stated precisely so it doesn't get mistaken for a regression: post-cap,
identical pace on the SAME kind can still land on different final rungs if
`resets_in_seconds` differs, because the downtime cap bands the wall-clock
COST of exhaustion (D = resets_in - exhaust_effective), not the pace ratio
itself. That is downtime-driven, not kind-driven or pace-driven, and is
covered by its own row below (two weekly_all limits, same ratio, different
resets_in, different final rungs). Everything else -- percent never driving
posture, severity never laundering percent back in, an unjudgeable pace never
panicking, every posture explaining itself -- is unchanged and still pinned
row by row.

Run against a candidate build with:
    USAGE_UNDER_TEST=/path/to/check-usage.py python3 test_usage_posture.py
"""
import importlib.util
import os
import sys

SCRIPT = os.environ.get(
    "USAGE_UNDER_TEST",
    os.path.expanduser("~/.claude/skills/claude-usage/scripts/check-usage.py"))

WEEK = 7 * 24 * 3600
FIVE_H = 5 * 3600


def load():
    """Import the script without running main()."""
    spec = importlib.util.spec_from_file_location("usage_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.argv = ["check-usage.py", "--json"]
    spec.loader.exec_module(m)
    return m


def limit(kind, percent, resets_in, delta=None, over=1680, severity="normal"):
    """One normalized limit dict, shaped exactly as parse_limits emits."""
    l = {
        "kind": kind,
        "percent": percent,
        "severity": severity,
        "is_active": False,
        "resets_at": None,
        "resets_in_seconds": resets_in,
        "scope_model": None,
        "scope": None,
        "extra": {},
    }
    if delta is not None:
        l["burn"] = {"delta_percent": delta, "over_seconds": over}
    return l


# (name, limit, expected_posture, why_this_row_exists)
CASES = [
    # --- the 2026-08-11 regression: healthy account, fleet halted -----------
    ("TODAY weekly: 97% but 2h to reset, pacing fine",
     limit("weekly_all", 97, 7999, delta=1.0), "normal",
     "The live case that stopped six runners. 3% left with 1.2% of the week "
     "to run is AHEAD of pace. Percent said wind_down; pace says fine."),
    ("TODAY session: 45%, 53m to reset, gentle burn",
     limit("session", 45, 3199, delta=6.0), "normal",
     "Same snapshot, ratio 0.21. Was already correct; must stay correct."),

    # --- the case percent-based logic misses entirely -----------------------
    # ratio = (60/600) / (70/7200) = 10.3 -> wind_down band on pace alone. v3: the
    # downtime cap then looks at what emptying this window actually COSTS -- only
    # 7200s (2h) remains before the weekly window resets anyway, so the
    # margin-adjusted earliest exhaust (~609s) leaves D ~= 6591s (~1h49m), which
    # is under the 21600s frugal cap -- capping wind_down DOWN to frugal. Still
    # unambiguously escalated from a 30%-full 'normal' percent reading would give,
    # which is the point of this row; v3 just makes the terminal rung reflect the
    # wall-clock cost of being wrong rather than the raw ratio alone.
    ("LOW percent, doomed pace, weekly",
     limit("weekly_all", 30, 7200, delta=60.0, over=600), "frugal",
     "30% is nowhere near any percent band, but this empties long before "
     "reset. Percent-based logic is blind here; pace escalates it, and the "
     "downtime cap then rightsizes the rung to the ~1h49m this actually costs."),
    ("LOW percent, doomed pace, session",
     limit("session", 30, 7200, delta=60.0, over=600), "frugal",
     "Identical pace, different kind, same final rung -- not because the "
     "cap is kind-blind (it applies a different margin per kind: 0.85 here "
     "vs 1.15 above) but because both land in the same [3600s,21600s) cap "
     "band regardless (~1h46m vs ~1h49m of downtime). See the two "
     "same-kind rows below for a case where resets_in, not kind, is what "
     "actually moves the final rung."),
    # ratio = (25/900) / (70/3600) = 1.43 -> frugal on pace alone. v3: only 3600s
    # left before this weekly window's own reset, so D ~= 1409s (~23m), inside
    # the 900-3600s 'measured' cap band -- capped down one rung from frugal.
    ("MILDLY over pace stays mid-ladder, weekly",
     limit("weekly_all", 30, 3600, delta=25.0, over=900), "measured",
     "Ratio 1.43 would read frugal on pace alone; the ladder must have "
     "working middle rungs, not just normal and wind_down. The downtime "
     "cap then softens it one more rung because only ~23m of downtime is "
     "actually on the line this close to the weekly window's own reset."),
    # Same kind, same pace ratio (1.43) as the row directly above, but far more
    # time left before reset (20000s vs 3600s) -- exhaust scaled to match
    # (delta=4.5/over=900 -> exhaust 14000s, same ratio). D ~= 7826s (~2h10m)
    # lands in the frugal cap band instead of measured, so provisional and cap
    # agree at frugal: no suffix, no downgrade. Proves the cap is DOWNTIME-
    # driven, not kind-driven -- two weekly_all limits, identical pace, differ
    # only in resets_in, and land on different final rungs (frugal here,
    # measured above) purely because the wall each one is heading for costs a
    # different amount of wait.
    ("SAME kind and pace as above, further from reset -> different final rung",
     limit("weekly_all", 30, 20000, delta=4.5, over=900), "frugal",
     "Downtime-driven, not kind-driven: identical ratio 1.43 to the row "
     "above, identical kind, only resets_in differs (20000s vs 3600s), and "
     "that alone is what keeps this one uncapped at frugal instead of "
     "sliding to measured."),

    # --- high percent is judged by the CLOCK, not by being high -----------
    # The point of the rule is not that a high percent can never escalate. It is that percent
    # alone cannot decide: 97% at 99% elapsed is fine (row 1 above), while 99% at 82% elapsed is
    # genuinely over budget. Same axis, opposite verdicts, decided by the clock.
    ("AT budget exactly, weekly: 99% spent, 99% elapsed",
     limit("weekly_all", 99, 7999, delta=0.0), "normal",
     "Headroom 0.76: 1% of budget for 1.3% of the window. Dead on budget pace, and budget "
     "pace is by definition survivable, so nothing escalates. The percent band used to call "
     "this number a halt."),
    ("OVER budget, session: 99% spent, 82% elapsed",
     limit("session", 99, 3199, delta=0.0), "measured",
     "Headroom 0.06: 1% of budget left to cover 18% of the window -- wind_down on pace alone. "
     "Same 99% as the row above, opposite verdict, decided entirely by the clock; that half of "
     "the point still holds. v3 CHANGES the terminal rung: window exhaust ~150s, optimistic "
     "session margin (/0.85) pushes it to ~176s, leaving D ~= 3023s (~50m) of actual downtime "
     "before the 53-minute session reset -- inside the [900s,3600s) 'measured' cap band. Capped "
     "down from wind_down to measured, and `imminent` (not tested by this table, see "
     "test_check_usage.py) is true: the right move is finish-and-checkpoint, not throttle, "
     "since a session window is use-it-or-lose-it and this budget was about to expire anyway."),

    # --- the hole the old code left wide open -----------------------------
    ("FULLY exhausted midway through the window",
     limit("weekly_all", 100, 302400, delta=0.0), "wind_down",
     "pace_info used to return None at remaining<=0 with the comment 'the percent axis owns "
     "this case' -- an axis that no longer exists, so this would have read normal at 100% "
     "consumed. Headroom is 0.00: there is nothing left to spend and half a week to go."),

    # --- sprint behaviour, and the early window ---------------------------
    ("Front-loaded sprint: 40% by day 1 of the week",
     limit("weekly_all", 40, 518400, delta=0.0), "measured",
     "Headroom 0.70 -- 60% of budget for 86% of the week, so 0.7x budget pace from here. The "
     "gentlest possible response to a deliberate heavy day, and it tightens on its own if the "
     "sprint persists (70% by day 2 -> 0.42 frugal). John's specific concern; pinned so a "
     "change to the bands is a deliberate act."),
    ("Early window needs no cap: headroom is well behaved",
     limit("weekly_all", 2, 601200, delta=0.0), "normal",
     "1h into the week at 2%: headroom 0.99. The consumed/elapsed ratio reads 3.4 here purely "
     "because the denominator is tiny, which is why the earlier draft needed an early-window "
     "cap bolted on. Headroom approaches 1-consumed early instead of exploding, so the cap "
     "was deleted rather than tuned."),
    ("Early window still catches a genuine blowout",
     limit("weekly_all", 90, 586800, delta=0.0), "conserve",
     "90% gone in the week's first 5 hours. The deleted early-window cap would have clamped "
     "this to measured -- softening the one early reading that genuinely matters."),

    # --- severity must not launder percent back in ------------------------
    ("HIGH percent with provider severity=critical",
     limit("weekly_all", 97, 7999, delta=1.0, severity="critical"), "normal",
     "Provider severity tracks percent (97->critical, 45->normal). If the "
     "severity bump survives, the removed axis is back through the door."),

    # --- undefined pace is reported, never panicked on --------------------
    ("No burn baseline yet",
     limit("weekly_all", 97, 7999), "normal",
     "Cold start. Unjudgeable pace must not stop the fleet — that is the "
     "same class of reason being fixed. It must SAY it could not judge."),

    # --- F3: the two spec worked examples that had no pinned test ---------
    # headroom = (1 - 0.97) / (1 - 259200/604800) = 0.03 / 0.571428 = 0.0525 -> wind_down
    # (< 0.10 band). No burn (delta 0.0), so the cap layer sees only the window axis:
    # win_exhaust = (1 - 0.97) * (259200/0.97) = 8016.4s; pool margin 1.15 -> 6970.8s adjusted;
    # D = 345600 - 6970.8 = 338629.2s, well past the 86400s conserve-cap ceiling -> no cap.
    # Provisional and final both wind_down.
    ("Weekly 97% spent, 4 days to reset (day 3 of 7)",
     limit("weekly_all", 97, 345600, delta=0.0), "wind_down",
     "headroom 0.0525 -> wind_down on pace alone; D ~= 338629s (>= 86400s) is far beyond every "
     "cap band, so the downtime cap leaves the provisional rung untouched -- unlike the day-3 "
     "reading elsewhere in this table with hours (not days) left, this one gets no relief."),
    # ratio = reset_s/exhaust_seconds = 15360/10440 = 1.4713 -> frugal on the burn axis (>= 1.25).
    # remaining=90, over=1160, delta=10.0 -> actual_rate=10/1160, exhaust=90/(10/1160)=10440
    # exactly. window axis (percent 10, session length 18000s) reads headroom 1.05 -> normal, so
    # it does not raise the provisional rung, and its own (much later) exhaust does not win the
    # earliest-of-two-axes race either. margin-adjusted exhaust = 10440/0.85 = 12282.35s;
    # D = 15360 - 12282.35 = 3077.65s, inside the [900s, 3600s) 'measured' cap band -> capped
    # down from frugal to measured. Matches the corrected session-overpace example in SKILL.md.
    ("Session burn exhaust 10440s (~2h54m), resets_in 15360s (~4h16m)",
     limit("session", 10, 15360, delta=10.0, over=1160), "measured",
     "Provisional frugal at ratio ~1.47 on the burn axis; the downtime cap then finds only "
     "~51m of actual downtime on the table (D ~= 3078s) and softens it one rung to measured."),
]


def main():
    m = load()
    print("script under test: %s\n" % SCRIPT)
    print("%-46s %-9s %-9s %s" % ("case", "expect", "got", "reason emitted"))
    print("-" * 130)
    failures = []
    for name, l, expect, why in CASES:
        reason = ""
        try:
            got, reason = m.limit_posture(l)
        except Exception as exc:
            got = "ERROR:%s" % exc
        ok = got == expect
        # Every verdict must explain itself: "why is it normal" is the question nobody could
        # answer on 2026-08-11, and an unexplained normal is indistinguishable from a blind one.
        if ok and not reason:
            ok, reason = False, "(NO REASON EMITTED)"
        print("%-46s %-9s %-9s %s" % (name[:46], expect, got, (reason or "")[:60]))
        if not ok:
            failures.append((name, expect, got, why))

    print()
    if failures:
        for name, expect, got, why in failures:
            print("FAIL: %s\n  expected %s, got %s\n  %s\n" % (name, expect, got, why))
        print("%d/%d FAILED" % (len(failures), len(CASES)))
        return 1
    print("all %d cases pass" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
