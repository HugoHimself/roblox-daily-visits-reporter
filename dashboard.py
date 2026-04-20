"""
Roblox Visits Dashboard — Flask web server.
Reads snapshot data from PostgreSQL (or local JSON fallback) and serves
an interactive Chart.js dashboard.
"""

import os
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template

from main import GAMES, ACTIVE_CCU_THRESHOLD, load_json, VISITS_FILE, CCU_FILE, get_latest_ccu_for_date

app = Flask(__name__)


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
    return render_template("index.html")


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
