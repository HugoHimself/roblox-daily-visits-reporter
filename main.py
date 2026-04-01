#!/usr/bin/env python3
"""
Roblox Daily Visits Reporter

Fetches yesterday's visit delta and current CCU from Roblox, then posts a
summary to Slack. Games are auto-categorized as Active (≥35 CCU) or
Non-Active (<35 CCU) based on the live CCU snapshot taken at run time.
Run daily at 10 AM — each run stores cumulative visit and CCU snapshots;
visit deltas are computed between consecutive daily snapshots.
On Mondays, --weekly posts a full 7-day summary instead.
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

def get_visit_delta(
    snapshots: dict, date_str: str, prev_date_str: str
) -> dict[str, int] | None:
    """
    Returns visits that accumulated between prev_date and date snapshots.
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


def trend_arrow(pct: float | None) -> str:
    if pct is None:
        return ""
    if pct > 0:
        return "↑"
    if pct < 0:
        return "↓"
    return "→"


def format_millions(n: int) -> str:
    return f"{round(n / 1_000_000)}M"


def format_pct(pct: float | None, vs_label: str, wow_base: int | None = None) -> str:
    if pct is None:
        return "N/A"
    arrow = trend_arrow(pct)
    sign = "+" if pct >= 0 else ""
    base_str = f" | {format_millions(wow_base)}" if wow_base is not None else ""
    return f"{arrow} {sign}{pct:.1f}% vs {vs_label}{base_str}"


# ---------------------------------------------------------------------------
# CCU-based game categorization
# ---------------------------------------------------------------------------

def categorize_games(today_ccu: dict[str, int]) -> dict[str, list[tuple[str, int]]]:
    categories: dict[str, list[tuple[str, int]]] = {"Active": [], "Non-Active": []}
    for name, uid in GAMES.items():
        ccu = today_ccu.get(str(uid), 0)
        categories["Active" if ccu >= ACTIVE_CCU_THRESHOLD else "Non-Active"].append((name, uid))
    return categories


# ---------------------------------------------------------------------------
# Daily message
# ---------------------------------------------------------------------------

def build_daily_message(
    yesterday: date,
    yesterday_visits: dict[str, int],
    wow_visits: dict[str, int] | None,
    today_ccu: dict[str, int],
) -> str:
    vs_label = f"last {WEEKDAY_SHORT[yesterday.weekday()]}"
    date_label = yesterday.strftime("%A, %B ") + str(yesterday.day)

    lines = [f"📊 Roblox Daily Visits — {date_label}", ""]

    categories = categorize_games(today_ccu)
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
                f"  {name}: {visits_str} ({format_pct(pct, vs_label, wow_base)}) — {ccu:,} CCU"
            )

        lines.append("")

    total_pct = wow_pct(grand_today, grand_wow) if wow_visits and grand_wow else None
    total_wow_base = grand_wow if wow_visits and grand_wow else None
    lines.append(f"📈 Total: {grand_today:,} visits ({format_pct(total_pct, vs_label, total_wow_base)})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Weekly message
# ---------------------------------------------------------------------------

def build_weekly_message(
    today: date,
    visit_snapshots: dict,
    ccu_snapshots: dict,
) -> str | None:
    """
    Weekly total = snap[today] - snap[today-7].
    Prev week    = snap[today-7] - snap[today-14].
    Returns None if not enough history.
    """
    today_str = today.isoformat()
    week_ago_str = (today - timedelta(days=7)).isoformat()
    two_weeks_ago_str = (today - timedelta(days=14)).isoformat()

    if today_str not in visit_snapshots or week_ago_str not in visit_snapshots:
        print("Not enough history for weekly summary — need 7 days of snapshots.")
        return None

    week_snap = visit_snapshots[today_str]
    prev_snap = visit_snapshots[week_ago_str]
    older_snap = visit_snapshots.get(two_weeks_ago_str, {})

    week_start = today - timedelta(days=7)
    week_end = today - timedelta(days=1)
    date_range = f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d')}"

    lines = [f"📅 Weekly Summary — {date_range}", ""]

    today_ccu = ccu_snapshots.get(today_str, {})
    categories = categorize_games(today_ccu)
    icons = {"Active": "🟢", "Non-Active": "🔴"}

    grand_week = 0
    grand_prev = 0

    for category, game_list in categories.items():
        if not game_list:
            continue

        lines.append(f"{icons[category]} {category} Games")

        # Compute weekly visits per game for sorting
        def weekly_visits(uid: int) -> int:
            uid_str = str(uid)
            return max(0, week_snap.get(uid_str, 0) - prev_snap.get(uid_str, 0))

        game_list.sort(key=lambda x: weekly_visits(x[1]), reverse=True)

        for name, uid in game_list:
            uid_str = str(uid)
            this_week = max(0, week_snap.get(uid_str, 0) - prev_snap.get(uid_str, 0))
            prev_week = max(0, prev_snap.get(uid_str, 0) - older_snap.get(uid_str, 0)) if older_snap else None
            grand_week += this_week
            grand_prev += prev_week or 0

            pct = wow_pct(this_week, prev_week) if prev_week else None
            lines.append(
                f"  {name}: {this_week:,} visits ({format_pct(pct, 'prev week', prev_week)})"
            )

        lines.append("")

    total_pct = wow_pct(grand_week, grand_prev) if grand_prev else None
    total_prev = grand_prev if grand_prev else None
    lines.append(f"📈 Total: {grand_week:,} visits ({format_pct(total_pct, 'prev week', total_prev)})")

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
    parser = argparse.ArgumentParser(description="Post Roblox visit stats to Slack.")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of posting.")
    parser.add_argument("--weekly", action="store_true", help="Post the weekly summary.")
    args = parser.parse_args()

    today = date.today()
    today_str = today.isoformat()
    yesterday_str = (today - timedelta(days=1)).isoformat()
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

    today_ccu = ccu_snapshots.get(today_str, {})

    # --- Weekly mode ---
    if args.weekly:
        message = build_weekly_message(today, visit_snapshots, ccu_snapshots)
        if message is None:
            sys.exit(0)  # Not enough data yet — silent exit, not an error
    else:
        # --- Daily mode ---
        yesterday_visits = get_visit_delta(visit_snapshots, today_str, yesterday_str)
        if yesterday_visits is None:
            print("No yesterday snapshot — first run. Visits will show as N/A.")
            yesterday_visits = {}

        wow_visits = get_visit_delta(visit_snapshots, wow_date_str, wow_prev_date_str)
        if wow_visits is None:
            print("Not enough historical data for WoW comparison — will show N/A.")

        message = build_daily_message(today - timedelta(days=1), yesterday_visits, wow_visits, today_ccu)

    if args.dry_run:
        sys.stdout.buffer.write(b"\n--- DRY RUN ---\n")
        sys.stdout.buffer.write(message.encode("utf-8") + b"\n")
        sys.stdout.buffer.write(b"--- END ---\n\n")
    else:
        post_to_slack(message)
        print("Message posted to Slack successfully.")


if __name__ == "__main__":
    main()
