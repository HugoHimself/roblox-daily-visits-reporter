"""
Roblox Visits Dashboard — Flask web server.
Reads snapshot data from PostgreSQL (or local JSON fallback) and serves
an interactive Chart.js dashboard.
"""

import json
import os
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template

# Reuse game config and data layer from main.py
from main import GAMES, load_json, VISITS_FILE, CCU_FILE

app = Flask(__name__)


def compute_series(snapshots: dict) -> dict:
    """
    Given cumulative snapshots {date: {uid_str: int}},
    returns daily visit deltas as a dict ready for Chart.js.
    """
    dates = sorted(snapshots.keys())
    if len(dates) < 2:
        return {"dates": [], "games": {name: [] for name in GAMES}, "total": []}

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

    return {"dates": result_dates, "games": games_data, "total": total_data}


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
    latest_date = max(ccu_snapshots.keys())
    latest = ccu_snapshots[latest_date]
    return jsonify({name: latest.get(str(uid), 0) for name, uid in GAMES.items()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
