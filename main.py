#!/usr/bin/env python3
"""
Roblox Daily Visits Reporter

Fetches yesterday's visit delta from Roblox and posts a summary to Slack.
Run daily at 10 AM — each run stores a cumulative snapshot; visits are
computed as the delta between today's and yesterday's snapshot.
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Game config
# Add games here. Move between "Active" and "Non-Active" as needed.
# ---------------------------------------------------------------------------
GAMES: dict[str, dict[str, int]] = {
    "Active": {
        "Hunted": 136431686349723,
        "Winx": 76737571462455,
    },
    "Non-Active": {
        # "GameName": universe_id,
    },
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROBLOX_API_URL = "https://games.roblox.com/v1/games"
SLACK_CHANNEL_ID = "C03CZ9EB538"
DATA_FILE = Path(__file__).parent / "data" / "visits.json"

CATEGORY_ICONS = {
    "Active": "🟢",
    "Non-Active": "🔴",
}

WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ---------------------------------------------------------------------------
# Data persistence
# ---------------------------------------------------------------------------

def load_snapshots() -> dict:
    if DATA_FILE.exists():
        with DATA_FILE.open() as f:
            return json.load(f)
    return {}


def save_snapshots(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Roblox API
# ---------------------------------------------------------------------------

def fetch_cumulative_visits() -> dict[str, int]:
    """Returns {str(universe_id): cumulative_visits} for all configured games."""
    all_ids = [uid for games in GAMES.values() for uid in games.values()]
    params = {"universeIds": ",".join(str(uid) for uid in all_ids)}

    try:
        resp = requests.get(ROBLOX_API_URL, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Roblox API request failed: {e}", file=sys.stderr)
        sys.exit(1)

    payload = resp.json()
    if "data" not in payload:
        print(f"[ERROR] Unexpected Roblox API response: {payload}", file=sys.stderr)
        sys.exit(1)

    return {str(item["id"]): item["visits"] for item in payload["data"]}


# ---------------------------------------------------------------------------
# Stats computation
# ---------------------------------------------------------------------------

def get_daily_visits(
    snapshots: dict, date_str: str, prev_date_str: str
) -> dict[str, int] | None:
    """
    Returns visits that accumulated between the prev_date snapshot and the
    date snapshot — i.e. the 24-hour window between the two daily runs.
    Returns None if either snapshot is missing.
    """
    if date_str not in snapshots or prev_date_str not in snapshots:
        return None

    result = {}
    for uid_str, cumulative in snapshots[date_str].items():
        if uid_str in snapshots[prev_date_str]:
            result[uid_str] = max(0, cumulative - snapshots[prev_date_str][uid_str])
    return result or None


def wow_pct(current: int, previous: int) -> float | None:
    if previous == 0:
        return None
    return (current - previous) / previous * 100


def format_pct(pct: float | None, weekday_short: str) -> str:
    if pct is None:
        return "N/A"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}% vs last {weekday_short}"


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def build_message(
    yesterday: date,
    yesterday_visits: dict[str, int],
    wow_visits: dict[str, int] | None,
) -> str:
    weekday_abbr = WEEKDAY_SHORT[yesterday.weekday()]
    date_label = yesterday.strftime("%A, %B ") + str(yesterday.day)

    lines = [f"📊 Roblox Daily Visits — {date_label}", ""]

    grand_today = 0
    grand_wow = 0

    for category, games in GAMES.items():
        if not games:
            continue

        icon = CATEGORY_ICONS.get(category, "⚪")
        lines.append(f"{icon} {category} Games")

        for name, uid in games.items():
            uid_str = str(uid)
            visits = yesterday_visits.get(uid_str, 0)
            grand_today += visits

            wow_base = wow_visits.get(uid_str) if wow_visits else None
            grand_wow += wow_base or 0

            pct = wow_pct(visits, wow_base) if wow_base is not None else None
            lines.append(f"  {name}: {visits:,} visits ({format_pct(pct, weekday_abbr)})")

        lines.append("")

    total_pct = wow_pct(grand_today, grand_wow) if wow_visits and grand_wow else None
    lines.append(f"📈 Total: {grand_today:,} visits ({format_pct(total_pct, weekday_abbr)})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def post_to_slack(message: str) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("[ERROR] SLACK_BOT_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"channel": SLACK_CHANNEL_ID, "text": message},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Slack API request failed: {e}", file=sys.stderr)
        sys.exit(1)

    result = resp.json()
    if not result.get("ok"):
        print(f"[ERROR] Slack API error: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Post yesterday's Roblox visit stats to Slack.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Slack message to stdout instead of posting it.",
    )
    args = parser.parse_args()

    today = date.today()
    yesterday = today - timedelta(days=1)

    today_str = today.isoformat()
    yesterday_str = yesterday.isoformat()
    # For WoW: same weekday last week = yesterday - 7 days
    # visits for that day = snap[today-7] - snap[today-8]
    wow_date_str = (today - timedelta(days=7)).isoformat()
    wow_prev_date_str = (today - timedelta(days=8)).isoformat()

    # 1. Load stored snapshots
    snapshots = load_snapshots()

    # 2. Fetch and persist today's cumulative snapshot
    if today_str not in snapshots:
        print(f"Fetching Roblox data ({today_str})...")
        snapshots[today_str] = fetch_cumulative_visits()
        save_snapshots(snapshots)
        print("Snapshot saved.")
    else:
        print(f"Snapshot for {today_str} already exists, skipping fetch.")

    # 3. Compute yesterday's visits (snap[today] - snap[yesterday])
    yesterday_visits = get_daily_visits(snapshots, today_str, yesterday_str)
    if yesterday_visits is None:
        print(
            f"[ERROR] Missing snapshot for {yesterday_str}. "
            "Need at least two consecutive daily runs to compute visit deltas.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 4. Compute WoW base visits (snap[today-7] - snap[today-8]); None if not enough history
    wow_visits = get_daily_visits(snapshots, wow_date_str, wow_prev_date_str)
    if wow_visits is None:
        print("Not enough historical data for WoW comparison — will show N/A.")

    # 5. Build and send (or print) the message
    message = build_message(yesterday, yesterday_visits, wow_visits)

    if args.dry_run:
        print("\n--- DRY RUN ---")
        print(message)
        print("--- END ---\n")
    else:
        post_to_slack(message)
        print("Message posted to Slack successfully.")


if __name__ == "__main__":
    main()
