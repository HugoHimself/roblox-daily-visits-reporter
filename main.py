#!/usr/bin/env python3
"""
Roblox Daily Visits Reporter

Fetches yesterday's visit delta and current CCU from Roblox, then posts a
summary to Slack. Games are auto-categorized as Active (≥35 CCU) or
Non-Active (<35 CCU) based on the live CCU snapshot taken at run time.
Run daily at 10 AM — each run stores cumulative visit and CCU snapshots;
visit deltas are computed between consecutive daily snapshots.
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

DATABASE_URL = os.environ.get("DATABASE_URL")

# Load .env file if present (no extra dependency needed)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Game config — add/remove games here, categories are auto-assigned by CCU
# ---------------------------------------------------------------------------
GAMES: dict[str, int] = {
    "Hunted": 7229780065,
    "Winx": 9328305853,
    "Glow Up": 9368056464,
    "Rabbids Takeover": 9054548108,
    "Japanese Supermarket Simulator": 7486728492,
    "Care Bears Caring Quest": 5988568657,
    "Dress Up BFF": 7737898405,
    "Care Bears Knockout": 9803070785,
    "Boat Racing": 8804313953,
    "Clean Crew": 9710205604,
    "MMA Fighters": 7436965994,
    "My Town": 9713686345,
    "Supermarket Simulator 2": 9550290526,
    "Ninja Training": 6981432181,
    "Sesame Street Neighborhood Adventures": 8738763254,
    "Chicken Jockey Training": 7552570368,
    "Wheelchair Training": 7475643372,
    "Art Leap by Belvedere Museum": 6906503978,
    "Wizard Training Simulator": 7448978668,
    "Princess Palace Tycoon": 7306273010,
    "My Brainrot Stand": 9674557095,
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROBLOX_API_URL = "https://games.roblox.com/v1/games"
SLACK_CHANNEL_ID = "C03CZ9EB538"
VISITS_FILE = Path(__file__).parent / "data" / "visits.json"
CCU_FILE = Path(__file__).parent / "data" / "ccu.json"

ACTIVE_CCU_THRESHOLD = 35

WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------------------
# Data persistence — PostgreSQL when DATABASE_URL is set, local files otherwise
# ---------------------------------------------------------------------------

def _db_conn():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def _db_ensure_table(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            key TEXT PRIMARY KEY,
            data JSONB NOT NULL
        )
    """)


def load_json(path: Path) -> dict:
    if DATABASE_URL:
        key = path.stem  # "visits" or "ccu"
        with _db_conn() as conn, conn.cursor() as cur:
            _db_ensure_table(cur)
            cur.execute("SELECT data FROM snapshots WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else {}
    if path.exists():
        with path.open() as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict) -> None:
    if DATABASE_URL:
        key = path.stem
        with _db_conn() as conn, conn.cursor() as cur:
            _db_ensure_table(cur)
            cur.execute("""
                INSERT INTO snapshots (key, data) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data
            """, (key, json.dumps(data)))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Roblox API
# ---------------------------------------------------------------------------

def fetch_game_data() -> tuple[dict[str, int], dict[str, int]]:
    """Returns (cumulative_visits, ccu) dicts keyed by str(universe_id)."""
    all_ids = list(GAMES.values())
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

    visits = {}
    ccu = {}
    for item in payload["data"]:
        uid_str = str(item["id"])
        visits[uid_str] = item["visits"]
        ccu[uid_str] = item["playing"]

    return visits, ccu


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


def format_millions(n: int) -> str:
    return f"{round(n / 1_000_000)}M"


def format_pct(pct: float | None, weekday_short: str, wow_base: int | None = None) -> str:
    if pct is None:
        return "N/A"
    sign = "+" if pct >= 0 else ""
    base_str = f" | {format_millions(wow_base)}" if wow_base is not None else ""
    return f"{sign}{pct:.1f}% vs last {weekday_short}{base_str}"


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def build_message(
    yesterday: date,
    yesterday_visits: dict[str, int],
    wow_visits: dict[str, int] | None,
    today_ccu: dict[str, int],
) -> str:
    weekday_abbr = WEEKDAY_SHORT[yesterday.weekday()]
    date_label = yesterday.strftime("%A, %B ") + str(yesterday.day)

    lines = [f"📊 Roblox Daily Visits — {date_label}", ""]

    # Bucket games by current CCU
    categories: dict[str, list[tuple[str, int]]] = {"Active": [], "Non-Active": []}
    for name, uid in GAMES.items():
        uid_str = str(uid)
        ccu = today_ccu.get(uid_str, 0)
        bucket = "Active" if ccu >= ACTIVE_CCU_THRESHOLD else "Non-Active"
        categories[bucket].append((name, uid))

    icons = {"Active": "🟢", "Non-Active": "🔴"}

    grand_today = 0
    grand_wow = 0

    for category, game_list in categories.items():
        if not game_list:
            continue

        lines.append(f"{icons[category]} {category} Games")

        game_list.sort(key=lambda x: yesterday_visits.get(str(x[1]), 0), reverse=True)

        for name, uid in game_list:
            uid_str = str(uid)
            visits = yesterday_visits.get(uid_str)
            ccu = today_ccu.get(uid_str, 0)
            if visits is not None:
                grand_today += visits

            wow_base = wow_visits.get(uid_str) if wow_visits else None
            grand_wow += wow_base or 0

            pct = wow_pct(visits, wow_base) if (visits is not None and wow_base is not None) else None
            visits_str = f"{visits:,} visits" if visits is not None else "N/A visits"
            lines.append(
                f"  {name}: {visits_str} ({format_pct(pct, weekday_abbr, wow_base)}) — {ccu:,} CCU"
            )

        lines.append("")

    total_pct = wow_pct(grand_today, grand_wow) if wow_visits and grand_wow else None
    total_wow_base = grand_wow if wow_visits and grand_wow else None
    lines.append(f"📈 Total: {grand_today:,} visits ({format_pct(total_pct, weekday_abbr, total_wow_base)})")

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
    wow_date_str = (today - timedelta(days=7)).isoformat()
    wow_prev_date_str = (today - timedelta(days=8)).isoformat()

    # 1. Load stored snapshots
    visit_snapshots = load_json(VISITS_FILE)
    ccu_snapshots = load_json(CCU_FILE)

    # 2. Fetch and persist today's snapshots (visits + CCU)
    if today_str not in visit_snapshots:
        print(f"Fetching Roblox data ({today_str})...")
        visits_today, ccu_today = fetch_game_data()
        visit_snapshots[today_str] = visits_today
        ccu_snapshots[today_str] = ccu_today
        save_json(VISITS_FILE, visit_snapshots)
        save_json(CCU_FILE, ccu_snapshots)
        print("Snapshots saved.")
    else:
        print(f"Snapshot for {today_str} already exists, skipping fetch.")

    # 3. Compute yesterday's visits (snap[today] - snap[yesterday])
    yesterday_visits = get_daily_visits(visit_snapshots, today_str, yesterday_str)
    if yesterday_visits is None:
        print("No yesterday snapshot — first run. Visits will show as N/A.")
        yesterday_visits = {}

    # 4. Compute WoW base visits; None if not enough history
    wow_visits = get_daily_visits(visit_snapshots, wow_date_str, wow_prev_date_str)
    if wow_visits is None:
        print("Not enough historical data for WoW comparison — will show N/A.")

    # 5. Use today's CCU for live categorization
    today_ccu = ccu_snapshots.get(today_str, {})

    # 6. Build and send (or print) the message
    message = build_message(yesterday, yesterday_visits, wow_visits, today_ccu)

    if args.dry_run:
        sys.stdout.buffer.write(b"\n--- DRY RUN ---\n")
        sys.stdout.buffer.write(message.encode("utf-8") + b"\n")
        sys.stdout.buffer.write(b"--- END ---\n\n")
    else:
        post_to_slack(message)
        print("Message posted to Slack successfully.")


if __name__ == "__main__":
    main()
