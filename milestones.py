"""
Milestone detection + Slack alert messages.

Tracks three kinds of milestones across the hourly milestone run:
  1. Per-game visit thresholds  (1M, 5M, 10M, 25M, 50M, 100M, then every +50M)
  2. Portfolio total thresholds  (every +25M)
  3. New per-game all-time concurrent-player (CCU) records

State is persisted via main.load_json/save_json (PostgreSQL in prod, local
JSON otherwise) under the "milestones" key, so every milestone fires exactly
once. The FIRST run only establishes a silent baseline — no alerts — so a
fresh deploy never dumps dozens of "milestone" messages for thresholds that
were already passed long ago.

This module is deliberately clock-free (timestamps are passed in) so it can be
unit-tested deterministically.
"""

from __future__ import annotations

from datetime import datetime

# ---------------------------------------------------------------------------
# Threshold configuration — tweak freely
# ---------------------------------------------------------------------------

# Per-game visit ladder: early traction flags, then a rung every +10M.
#   1M, 5M, 10M, 20M, 30M, 40M, 50M, ...  (every 10 million)
# This catches "round ten-million" milestones like 120M, while still flagging
# the first 1M and 5M for newer games.
_VISIT_EARLY_RUNGS = [1_000_000, 5_000_000]
_VISIT_STEP = 10_000_000              # a rung every +10M from 10M upward
_VISIT_CEILING = 3_000_000_000        # generate rungs up to 3B

TOTAL_STEP = 25_000_000               # portfolio total: a rung every +25M
MIN_CCU_RECORD = 250                  # ignore CCU "records" below this many players
CCU_RECORD_COOLDOWN_HOURS = 20        # at most one CCU record per game / ~day

# Known historical all-time CCU peaks supplied by the team. The Roblox API can't
# report a game's past peak, so without these the engine would treat any climb
# above the first value it happened to observe as a "record". The stored peak is
# floored to at least this number, keyed by universe id.
KNOWN_CCU_PEAKS: dict[str, int] = {
    "7229780065": 9000,    # Hunted
    "8738763254": 19900,   # Sesame Street Neighborhood Adventures
    "9328305853": 16000,   # Winx
    "9710205604": 8500,    # Clean Crew
    "7486728492": 5000,    # Japanese Supermarket Simulator
    "7436965994": 4400,    # MMA Fighters
    "5988568657": 3600,    # Care Bears Caring Quest
    "9368056464": 3000,    # Glow Up by E.L.F Cosmetics
    "6906503978": 698,     # Art Leap by Belvedere Museum
}


def visit_rungs() -> list[int]:
    """The full per-game visit ladder, ascending."""
    rungs = list(_VISIT_EARLY_RUNGS)
    n = _VISIT_STEP
    while n <= _VISIT_CEILING:
        rungs.append(n)
        n += _VISIT_STEP
    return rungs


def highest_rung_at_or_below(value: int, rungs: list[int]) -> int | None:
    """Largest rung that `value` has reached, or None if below the first rung."""
    crossed = [r for r in rungs if value >= r]
    return crossed[-1] if crossed else None


def next_rung(rung: int | None, rungs: list[int]) -> int | None:
    """The rung immediately above `rung` (or the first rung if `rung` is None)."""
    above = [r for r in rungs if rung is None or r > rung]
    return above[0] if above else None


def total_milestone_at_or_below(total: int) -> int | None:
    """Largest +TOTAL_STEP multiple the portfolio total has reached."""
    if total < TOTAL_STEP:
        return None
    return (total // TOTAL_STEP) * TOTAL_STEP


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_short(n: int) -> str:
    """120000000 -> '120M', 1250000 -> '1.25M', 25000000 -> '25M'."""
    if n >= 1_000_000:
        m = n / 1_000_000
        s = f"{m:.2f}".rstrip("0").rstrip(".")
        return f"{s}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}K"
    return f"{n:,}"


def _hours_between(a_iso: str, b_iso: str) -> float:
    try:
        a = datetime.fromisoformat(a_iso)
        b = datetime.fromisoformat(b_iso)
        return abs((b - a).total_seconds()) / 3600.0
    except (ValueError, TypeError):
        return float("inf")


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------

def check(
    visits: dict[str, int],
    ccu: dict[str, int],
    names: dict[str, str],
    state: dict | None,
    now_iso: str,
) -> tuple[list[str], dict]:
    """
    Evaluate milestones against the latest live data.

    Args:
        visits:  {uid_str: cumulative_visits}
        ccu:     {uid_str: players_online_now}
        names:   {uid_str: game_name}
        state:   previously-saved milestone state (None on first ever run)
        now_iso: current UTC timestamp (ISO 8601) — for CCU-record cooldown

    Returns (alerts, new_state). On the first run, alerts is empty and state is
    seeded to the current values so nothing historical fires.
    """
    state = dict(state or {})
    g_state: dict[str, dict] = {k: dict(v) for k, v in state.get("games", {}).items()}
    first_run = not state.get("initialized")
    rungs = visit_rungs()
    alerts: list[str] = []

    for uid, name in names.items():
        gs = dict(g_state.get(uid, {}))

        # --- Per-game visit milestones ---
        if uid in visits:
            v = visits[uid]
            cur = highest_rung_at_or_below(v, rungs)
            prev = gs.get("visits_rung")
            if first_run or prev is None:
                gs["visits_rung"] = cur                       # silent baseline
            elif cur is not None and cur > prev:
                nxt = next_rung(cur, rungs)
                tail = f" Next stop: {fmt_short(nxt)}." if nxt else ""
                alerts.append(
                    f"🎉 *{name}* just crossed *{fmt_short(cur)} visits* "
                    f"(now {v:,}).{tail}"
                )
                gs["visits_rung"] = cur

        # --- Per-game all-time CCU records ---
        if uid in ccu:
            c = ccu[uid]
            # Floor the stored peak with any known historical peak, so a normal
            # number is never mistaken for an all-time record.
            peak = max(gs.get("ccu_peak", 0), KNOWN_CCU_PEAKS.get(uid, 0))
            if first_run or "ccu_peak" not in gs:
                gs["ccu_peak"] = max(peak, c)                 # silent baseline
            elif c > peak and c >= MIN_CCU_RECORD:
                last = gs.get("ccu_alert_iso")
                if not last or _hours_between(last, now_iso) >= CCU_RECORD_COOLDOWN_HOURS:
                    alerts.append(
                        f"🔥 *{name}* just hit an all-time high of "
                        f"*{c:,} players online at once* (previous peak {peak:,})."
                    )
                    gs["ccu_alert_iso"] = now_iso
                gs["ccu_peak"] = c

        g_state[uid] = gs

    # --- Portfolio total milestone ---
    total = sum(visits.values())
    cur_total = total_milestone_at_or_below(total)
    prev_total = state.get("total_rung")
    if first_run or prev_total is None:
        state["total_rung"] = cur_total                       # silent baseline
    elif cur_total is not None and cur_total > prev_total:
        alerts.append(
            f"🚀 *Portfolio milestone!* All tracked games combined just passed "
            f"*{fmt_short(cur_total)} visits* (now {total:,})."
        )
        state["total_rung"] = cur_total

    state["games"] = g_state
    state["initialized"] = True
    state["last_checked"] = now_iso
    return alerts, state
