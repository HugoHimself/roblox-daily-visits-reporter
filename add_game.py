#!/usr/bin/env python3
"""
Add Roblox games to the tracked roster (data/games.json) in one command.

    python add_game.py 123318668892923
    python add_game.py https://www.roblox.com/games/73503837134305/Color-Catch=800
    python add_game.py 76192781446190 86247005360203=1200 --commit --push

Each argument is a Place ID (or a roblox.com game URL), optionally followed
by "=<peak>" to seed the game's known all-time CCU peak. For every game it:

  1. resolves the Place ID -> Universe ID via the Roblox places API
  2. verifies the universe returns live data (name / visits / CCU)
  3. appends it to data/games.json - or, if it's already tracked and a
     "=<peak>" was given, updates that game's ccu_peak

--dry-run shows the plan without touching the file.
--commit  git-commits data/games.json; --push then pushes HEAD to main.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

import games_config

ROOT = Path(__file__).parent
UNIVERSE_URL = "https://apis.roblox.com/universes/v1/places/{place}/universe"
GAMES_URL = "https://games.roblox.com/v1/games?universeIds={ids}"


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "add_game.py"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def parse_arg(arg: str) -> tuple[int, int | None]:
    """'<place|url>[=peak]' -> (place_id, peak_or_None). Accepts 1.7K / 1,700."""
    peak: int | None = None
    if "=" in arg:
        arg, peak_s = arg.rsplit("=", 1)
        peak_s = peak_s.strip().replace(",", "").replace("_", "")
        peak = int(float(peak_s[:-1]) * 1000) if peak_s[-1] in "kK" else int(peak_s)
    m = re.search(r"/games/(\d+)", arg)
    place = int(m.group(1)) if m else int(arg.strip())
    return place, peak


def resolve_universe(place_id: int) -> int:
    return int(_get_json(UNIVERSE_URL.format(place=place_id))["universeId"])


def fetch_games(universe_ids: list[int]) -> dict[int, dict]:
    ids = ",".join(str(u) for u in universe_ids)
    return {int(g["id"]): g for g in _get_json(GAMES_URL.format(ids=ids)).get("data", [])}


def clean_name(raw: str) -> str:
    """Strip emoji / decorative junk Roblox titles often carry, keep the words."""
    name = re.sub(r"[^\w\s'!?.&:()\-]", "", raw)
    return re.sub(r"\s+", " ", name).strip(" -:")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("games", nargs="+", help="<placeId|url>[=ccu_peak] ...")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--commit", action="store_true", help="git commit data/games.json")
    ap.add_argument("--push", action="store_true", help="git push HEAD:main after committing")
    args = ap.parse_args()

    roster = games_config.load()
    by_uid = {int(g["universe_id"]): g for g in roster}

    # resolve everything first so one bad id aborts before any edit
    wanted: list[tuple[int, int, int | None]] = []
    for a in args.games:
        place, peak = parse_arg(a)
        try:
            uid = resolve_universe(place)
        except Exception as e:                       # noqa: BLE001
            print(f"[ERROR] place {place}: could not resolve universe ({e})", file=sys.stderr)
            return 1
        wanted.append((place, uid, peak))

    live = fetch_games([u for _, u, _ in wanted])
    changes: list[str] = []

    for place, uid, peak in wanted:
        g = live.get(uid)
        if not g:
            print(f"[ERROR] universe {uid} (place {place}) returned no live data - skipping", file=sys.stderr)
            continue
        name = clean_name(g["name"])
        print(f"{name}: place {place} -> universe {uid} | visits={g['visits']:,} | playing={g['playing']}"
              + (f" | peak={peak:,}" if peak else " | peak=(none)"))

        if uid in by_uid:
            entry = by_uid[uid]
            if peak is None:
                print("   already tracked - nothing to do")
            elif entry.get("ccu_peak") == peak:
                print(f"   already tracked with peak {peak:,} - nothing to do")
            else:
                print(f"   already tracked - updating peak {entry.get('ccu_peak') or 0:,} -> {peak:,}")
                entry["ccu_peak"] = peak
                changes.append(f"{entry['name']}: peak -> {peak:,}")
            continue

        if peak is None:
            print("   note: no ccu_peak seeded - the engine will baseline at the first CCU it sees")
        entry = {"name": name, "universe_id": uid, "ccu_peak": peak}
        roster.append(entry)
        by_uid[uid] = entry
        changes.append(f"add {name} (universe {uid}" + (f", peak {peak:,})" if peak else ", no peak)"))

    if not changes:
        print("Nothing to change.")
        return 0
    if args.dry_run:
        print(f"\n(dry run) {len(changes)} change(s):\n  " + "\n  ".join(changes))
        return 0

    games_config.save(roster)
    subprocess.run([sys.executable, "-c", "import main, milestones"], check=True, cwd=ROOT)
    print(f"\nWrote data/games.json ({len(roster)} games); main/milestones import cleanly.")

    if args.commit:
        added = [c[4:].split(" (")[0] for c in changes if c.startswith("add ")]
        title = "feat: track " + ", ".join(added) if added else "chore: update CCU peaks"
        msg = title + "\n\n" + "\n".join(f"- {c}" for c in changes) + "\n"
        subprocess.run(["git", "add", "data/games.json"], check=True, cwd=ROOT)
        subprocess.run(["git", "commit", "-q", "-m", msg], check=True, cwd=ROOT)
        print("committed:", subprocess.run(["git", "log", "--oneline", "-1"], capture_output=True,
                                           text=True, cwd=ROOT).stdout.strip())
        if args.push:
            subprocess.run(["git", "push", "origin", "HEAD:main"], check=True, cwd=ROOT)
            print("pushed to origin/main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
