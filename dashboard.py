"""
Roblox Visits Dashboard — Flask web server.
Reads snapshot data from PostgreSQL (or local JSON fallback) and serves
an interactive Chart.js dashboard.
"""

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import milestones as ms
from main import (
    GAMES, ACTIVE_CCU_THRESHOLD, load_json, save_json, VISITS_FILE, CCU_FILE,
    get_latest_ccu_for_date, fetch_live_stats, fetch_votes,
    load_extra_games, EXTRA_FILE,
)

app = Flask(__name__)

MANUAL_FILE = Path(__file__).parent / "data" / "manual.json"

# Short-lived cache so rapid board refreshes don't hammer the Roblox API.
_live_cache: dict = {"ts": 0.0, "data": None}
LIVE_TTL_SECONDS = 60


def load_manual() -> dict:
    """Manual session-time data: Postgres/local store, seeded from the bundled
    data/manual.json the first time (when the prod DB is still empty)."""
    data = load_json(MANUAL_FILE)
    if data:
        return data
    if MANUAL_FILE.exists():
        with MANUAL_FILE.open() as f:
            return json.load(f)
    return {}


def get_live() -> dict:
    """Current visits/CCU/votes for all games (cached ~60s). Falls back to the
    latest stored snapshot if Roblox is unreachable, so the board always renders."""
    now = time.time()
    cached = _live_cache["data"]
    if cached and now - _live_cache["ts"] < LIVE_TTL_SECONDS:
        return cached

    live = True
    try:
        visits, ccu = fetch_live_stats()
    except Exception:
        live = False
        visit_snapshots = load_json(VISITS_FILE)
        ccu_snapshots = load_json(CCU_FILE)
        visits = visit_snapshots[max(visit_snapshots)] if visit_snapshots else {}
        ccu = (get_latest_ccu_for_date(ccu_snapshots, max(ccu_snapshots)[:10])
               if ccu_snapshots else {})

    try:
        votes = fetch_votes()
    except Exception:
        votes = {}

    data = {"visits": visits, "ccu": ccu, "votes": votes, "live": live, "fetched": now}
    _live_cache.update(ts=now, data=data)
    return data


def compute_series(snapshots: dict) -> dict:
    """
    Given cumulative snapshots {date: {uid_str: int}},
    returns daily visit deltas + 7-day rolling average as a dict ready for Chart.js.
    """
    dates = sorted(snapshots.keys())
    if len(dates) < 2:
        return {"dates": [], "games": {name: [] for name in GAMES}, "total": [], "rolling_avg": []}

    result_dates = []
    games_data: dict[str, list[int]] = {name: [] for name in GAMES}
    total_data: list[int] = []

    for i in range(1, len(dates)):
        prev = snapshots[dates[i - 1]]
        curr = snapshots[dates[i]]

        result_dates.append(dates[i])
        day_total = 0

        for name, uid in GAMES.items():
            uid_str = str(uid)
            delta = max(0, curr.get(uid_str, 0) - prev.get(uid_str, 0))
            games_data[name].append(delta)
            day_total += delta

        total_data.append(day_total)

    # 7-day trailing rolling average
    rolling_avg = []
    for i in range(len(total_data)):
        start = max(0, i - 6)
        rolling_avg.append(round(sum(total_data[start:i + 1]) / (i - start + 1)))

    return {"dates": result_dates, "games": games_data, "total": total_data, "rolling_avg": rolling_avg}


def get_avg_ccu_for_date(ccu_snapshots: dict, date_str: str) -> dict:
    """Average all CCU snapshots (possibly multiple per day) for a given date."""
    day_snaps = [v for k, v in ccu_snapshots.items() if k[:10] == date_str]
    if not day_snaps:
        return {}
    uid_strs = set().union(*(s.keys() for s in day_snaps))
    return {
        uid_str: round(sum(s.get(uid_str, 0) for s in day_snaps) / len(day_snaps))
        for uid_str in uid_strs
    }


def compute_dow_avg(snapshots: dict) -> dict:
    """Returns avg visits per day-of-week for total and each game individually."""
    dates = sorted(snapshots.keys())
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    empty = {d: 0 for d in day_names}

    if len(dates) < 2:
        return {"total": empty, "games": {name: empty.copy() for name in GAMES}}

    total_buckets: dict[int, list[int]] = {i: [] for i in range(7)}
    game_buckets: dict[str, dict[int, list[int]]] = {
        name: {i: [] for i in range(7)} for name in GAMES
    }

    for i in range(1, len(dates)):
        prev = snapshots[dates[i - 1]]
        curr = snapshots[dates[i]]
        dow = date.fromisoformat(dates[i]).weekday()
        day_total = 0
        for name, uid in GAMES.items():
            delta = max(0, curr.get(str(uid), 0) - prev.get(str(uid), 0))
            game_buckets[name][dow].append(delta)
            day_total += delta
        total_buckets[dow].append(day_total)

    def avg_dict(buckets: dict[int, list[int]]) -> dict[str, int]:
        return {day_names[i]: round(sum(v) / len(v)) if v else 0 for i, v in buckets.items()}

    return {
        "total": avg_dict(total_buckets),
        "games": {name: avg_dict(b) for name, b in game_buckets.items()},
    }


@app.route("/")
def index():
    """Live portfolio board — the spreadsheet-style view."""
    return render_template("portfolio.html")


@app.route("/trends")
def trends():
    """Historical trends dashboard (daily deltas, WoW, day-of-week)."""
    return render_template("index.html")


def _game_row(gid, name, source, visits, ccu, rating, avg_min, available, editable, rungs):
    """Build a single portfolio row (shared by live + manual games)."""
    hours = round(visits * avg_min / 60) if (visits and avg_min) else None
    cur_rung = ms.highest_rung_at_or_below(visits or 0, rungs)
    nxt = ms.next_rung(cur_rung, rungs)
    progress = None
    if nxt is not None and visits is not None:
        base = cur_rung or 0
        progress = round((visits - base) / (nxt - base) * 100, 1)
    return {
        "id": gid, "uid": gid, "name": name, "source": source,
        "visits": visits, "ccu": ccu, "rating": rating,
        "avg_session_min": avg_min, "total_hours": hours,
        "available": available, "editable": editable,
        "next_milestone": nxt, "progress": progress,
    }


@app.route("/api/portfolio")
def api_portfolio():
    """Live + manual cumulative stats per game, portfolio totals, milestone progress."""
    live = get_live()
    manual = load_manual()
    extra = load_extra_games()
    rungs = ms.visit_rungs()

    games = []

    # --- Live games (auto from Roblox) ---
    for name, uid in GAMES.items():
        us = str(uid)
        v = live["visits"].get(us)
        vote = live["votes"].get(us)
        rating = None
        if vote and (vote["up"] + vote["down"]) > 0:
            rating = round(vote["up"] / (vote["up"] + vote["down"]) * 100, 1)
        avg_min = (manual.get(us) or {}).get("avg_session_min")
        games.append(_game_row(
            us, name, "live", v, live["ccu"].get(us, 0), rating, avg_min,
            available=v is not None, editable=["avg_session_min"], rungs=rungs,
        ))

    # --- Manual games (entered by hand, seeded from the sheet) ---
    for gid, g in extra.items():
        v = g.get("visits")
        games.append(_game_row(
            gid, g.get("name", gid), "manual", v, None, g.get("rating"),
            g.get("avg_session_min"), available=v is not None,
            editable=["visits", "rating", "avg_session_min"], rungs=rungs,
        ))

    # --- Totals across every game ---
    total_visits = sum(g["visits"] or 0 for g in games)
    total_ccu = sum(g["ccu"] or 0 for g in games)
    total_hours = sum(g["total_hours"] or 0 for g in games)
    for g in games:
        g["pct"] = round(g["visits"] / total_visits * 100, 1) if (g["visits"] and total_visits) else 0
    games.sort(key=lambda g: g["visits"] or 0, reverse=True)

    # Visits-weighted rating: a game's rating counts in proportion to its visits,
    # so Hunted (120M) weighs far more than Glow Up (9M). Simple mean kept for ref.
    rated = [(g["rating"], g["visits"]) for g in games if g["rating"] is not None and g["visits"]]
    weighted_rating = round(sum(r * v for r, v in rated) / sum(v for _, v in rated), 1) if rated else None
    simple_rating = round(sum(r for r, _ in rated) / len(rated), 1) if rated else None

    cur_total = ms.total_milestone_at_or_below(total_visits)
    next_total = (cur_total + ms.TOTAL_STEP) if cur_total is not None else ms.TOTAL_STEP
    total_progress = round((total_visits - (cur_total or 0)) / ms.TOTAL_STEP * 100, 1)

    return jsonify({
        "games": games,
        "totals": {
            "visits": total_visits, "ccu": total_ccu, "hours": total_hours,
            "rating": weighted_rating, "rating_simple": simple_rating,
            "next_milestone": next_total, "progress": total_progress,
            "count": len(games), "rated_count": len(rated),
        },
        "live": live["live"],
    })


@app.route("/api/edit", methods=["POST"])
def api_edit():
    """Edit a manual field. Live games: avg_session_min only. Manual games:
    visits, rating, avg_session_min. Body: {id, field, value}."""
    payload = request.get_json(force=True, silent=True) or {}
    gid = str(payload.get("id", "")).strip()
    field = str(payload.get("field", "")).strip()
    raw = payload.get("value")
    if not gid:
        return jsonify({"error": "id required"}), 400

    live_ids = {str(u) for u in GAMES.values()}
    is_live = gid in live_ids
    allowed = {"avg_session_min"} if is_live else {"visits", "rating", "avg_session_min"}
    if field not in allowed:
        return jsonify({"error": f"field '{field}' is not editable for this game"}), 400

    # Empty clears the value; otherwise parse as a number.
    if raw in (None, "", False):
        value = None
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid number"}), 400
        if field == "visits":
            value = int(value)

    if is_live:
        manual = load_manual()
        entry = dict(manual.get(gid, {}))
        if value is None:
            entry.pop(field, None)
        else:
            entry[field] = value
        manual[gid] = entry
        save_json(MANUAL_FILE, manual)
    else:
        extra = load_extra_games()
        if gid not in extra:
            return jsonify({"error": "unknown game"}), 404
        extra[gid][field] = value
        save_json(EXTRA_FILE, extra)

    _live_cache["ts"] = 0.0  # bust cache so totals recompute on next load
    return jsonify({"ok": True, "id": gid, "field": field, "value": value})


@app.route("/api/visits")
def api_visits():
    snapshots = load_json(VISITS_FILE)
    series = compute_series(snapshots)
    return jsonify(series)


@app.route("/api/ccu")
def api_ccu():
    """Latest CCU snapshot for each game."""
    ccu_snapshots = load_json(CCU_FILE)
    if not ccu_snapshots:
        return jsonify({})
    today_str = date.today().isoformat()
    latest = get_latest_ccu_for_date(ccu_snapshots, today_str) or ccu_snapshots[max(ccu_snapshots.keys())]
    return jsonify({name: latest.get(str(uid), 0) for name, uid in GAMES.items()})


@app.route("/api/stats")
def api_stats():
    """Per-game table stats (yesterday, WoW%, 7d avg, CCU) + DOW averages + last-updated date."""
    visit_snapshots = load_json(VISITS_FILE)
    ccu_snapshots = load_json(CCU_FILE)

    dates = sorted(visit_snapshots.keys())
    last_updated = dates[-1] if dates else None

    # Avg CCU for yesterday (from all intra-day snapshots)
    yesterday_str = (date.fromisoformat(last_updated) - timedelta(days=1)).isoformat() if last_updated else None
    avg_ccu = get_avg_ccu_for_date(ccu_snapshots, yesterday_str) if yesterday_str else {}
    # Fall back to latest available snapshot if no yesterday data yet
    if not avg_ccu and ccu_snapshots:
        avg_ccu = get_latest_ccu_for_date(ccu_snapshots, last_updated) if last_updated else {}

    games_stats = []
    for name, uid in GAMES.items():
        uid_str = str(uid)
        ccu = avg_ccu.get(uid_str, 0)
        active = ccu >= ACTIVE_CCU_THRESHOLD

        # Yesterday's visits
        yesterday = None
        if len(dates) >= 2:
            yesterday = max(0,
                visit_snapshots[dates[-1]].get(uid_str, 0) -
                visit_snapshots[dates[-2]].get(uid_str, 0)
            )

        # WoW %: yesterday vs same weekday 7 days ago
        wow_pct = None
        if last_updated and yesterday is not None:
            today_d = date.fromisoformat(last_updated)
            wow_date = (today_d - timedelta(days=7)).isoformat()
            wow_prev = (today_d - timedelta(days=8)).isoformat()
            if wow_date in visit_snapshots and wow_prev in visit_snapshots:
                wow_delta = max(0,
                    visit_snapshots[wow_date].get(uid_str, 0) -
                    visit_snapshots[wow_prev].get(uid_str, 0)
                )
                if wow_delta > 0:
                    wow_pct = round((yesterday - wow_delta) / wow_delta * 100, 1)

        # 7-day average (up to last 7 daily deltas)
        avg_7d = None
        if len(dates) >= 2:
            recent = dates[-8:] if len(dates) >= 8 else dates
            deltas = [
                max(0,
                    visit_snapshots[recent[i]].get(uid_str, 0) -
                    visit_snapshots[recent[i - 1]].get(uid_str, 0))
                for i in range(1, len(recent))
            ]
            if deltas:
                avg_7d = round(sum(deltas) / len(deltas))

        games_stats.append({
            "name": name,
            "yesterday": yesterday,
            "wow_pct": wow_pct,
            "avg_7d": avg_7d,
            "ccu": ccu,
            "active": active,
        })

    games_stats.sort(key=lambda x: x["yesterday"] or 0, reverse=True)

    return jsonify({
        "last_updated": last_updated,
        "games": games_stats,
        "dow_avg": compute_dow_avg(visit_snapshots),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
