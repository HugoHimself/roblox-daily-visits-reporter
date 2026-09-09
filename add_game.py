#!/usr/bin/env python3
"""
Add one or more Roblox games to the tracked roster in a single command.

    python add_game.py 123318668892923
    python add_game.py https://www.roblox.com/games/73503837134305/Color-Catch=800
    python add_game.py 76192781446190 86247005360203=1200 --commit

Each argument is a Place ID (or a roblox.com game URL), optionally followed
by "=<peak>" to seed the game's known all-time CCU peak. For every game it:

  1. resolves the Place ID -> Universe ID via the Roblox places API
  2. verifies the universe returns live data (name / visits / CCU)
  3. inserts it into GAMES in main.py (skipped if already tracked)
  4. inserts the peak into KNOWN_CCU_PEAKS in milestones.py (if given)

--commit  also `git commit`s the change (and --push pushes HEAD to main).
--dry-run shows what would change without touching any file.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
MAIN_PY = ROOT / "main.py"
MILESTONES_PY = ROOT / "milestones.py"

UNIVERSE_URL = "https://apis.roblox.com/universes/v1/places/{place}/universe"
GAMES_URL = "https://games.roblox.com/v1/games?universeIds={ids}"


# ---------------------------------------------------------------------------
# Roblox lookups
# ---------------------------------------------------------------------------

def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "add_game.py"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def parse_arg(arg: str) -> tuple[int, int | None]:
    """'<place|url>[=peak]' -> (place_id, peak_or_None)."""
    peak: int | None = None
    if "=" in arg:
        arg, peak_s = arg.rsplit("=", 1)
        peak = int(peak_s.replace(",", "").replace("_", ""))
    m = re.search(r"/games/(\d+)", arg)
    place = int(m.group(1)) if m else int(arg.strip())
    return place, peak


def resolve_universe(place_id: int) -> int:
    return int(_get_json(UNIVERSE_URL.format(place=place_id))["universeId"])


def fetch_games(universe_ids: list[int]) -> dict[int, dict]:
    ids = ",".join(str(u) for u in universe_ids)
    data = _get_json(GAMES_URL.format(ids=ids)).get("data", [])
    return {int(g["id"]): g for g in data}


def clean_name(raw: str) -> str:
    """Strip emoji / decorative junk Roblox titles often carry, keep the words."""
    name = re.sub(r"[^\w\s'!?.&:()\-]", "", raw)   # drop emoji & symbols
    name = re.sub(r"\s+", " ", name).strip(" -:")
    return name.replace('"', '\\"')


# ---------------------------------------------------------------------------
# Source edits — insert a line before the closing "}" of a top-level dict
# ---------------------------------------------------------------------------

def _insert_into_dict(src: str, header_regex: str, new_line: str) -> str:
    lines = src.splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if re.match(header_regex, l))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip("\r\n") == "}")
    lines.insert(end, new_line)
    return "".join(lines)


def games_already_tracked(src: str) -> set[int]:
    block = re.search(r"^GAMES: dict\[str, int\] = \{(.*?)^\}", src, re.S | re.M).group(1)
    return {int(u) for u in re.findall(r":\s*(\d{6,})\s*,", block)}


def add_to_games(src: str, name: str, uid: int) -> str:
    return _insert_into_dict(src, r"^GAMES: dict\[str, int\] = \{", f'    "{name}": {uid},\n')


def add_to_peaks(src: str, name: str, uid: int, peak: int) -> str:
    key = f'"{uid}": {peak},'
    return _insert_into_dict(src, r"^KNOWN_CCU_PEAKS: dict\[str, int\] = \{",
                             f"    {key:<22} # {name}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("games", nargs="+", help="<placeId|url>[=ccu_peak] ...")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--commit", action="store_true", help="git commit the change")
    ap.add_argument("--push", action="store_true", help="git push HEAD:main after committing")
    args = ap.parse_args()

    main_src = MAIN_PY.read_text(encoding="utf-8")
    ms_src = MILESTONES_PY.read_text(encoding="utf-8")
    tracked = games_already_tracked(main_src)

    # 1. resolve everything first so a bad ID aborts before any edit
    wanted: list[tuple[int, int, int | None]] = []          # (place, universe, peak)
    for a in args.games:
        place, peak = parse_arg(a)
        try:
            uid = resolve_universe(place)
        except Exception as e:                               # noqa: BLE001
            print(f"[ERROR] place {place}: could not resolve universe ({e})", file=sys.stderr)
            return 1
        wanted.append((place, uid, peak))

    live = fetch_games([u for _, u, _ in wanted])
    added: list[tuple[str, int, int | None]] = []

    for place, uid, peak in wanted:
        g = live.get(uid)
        if not g:
            print(f"[ERROR] universe {uid} (place {place}) returned no live data — skipping", file=sys.stderr)
            continue
        name = clean_name(g["name"])
        print(f"{name}: place {place} -> universe {uid} | "
              f"visits={g['visits']:,} | playing={g['playing']}"
              + (f" | peak={peak:,}" if peak else " | peak=(none)"))
        if uid in tracked:
            print(f"   already tracked — skipping")
            continue
        if not args.dry_run:
            main_src = add_to_games(main_src, name, uid)
            if peak is not None:
                ms_src = add_to_peaks(ms_src, name, uid, peak)
        tracked.add(uid)
        added.append((name, uid, peak))

    if not added:
        print("Nothing to add.")
        return 0
    if args.dry_run:
        print(f"\n(dry run) would add {len(added)} game(s)")
        return 0

    MAIN_PY.write_text(main_src, encoding="utf-8")
    if any(p is not None for _, _, p in added):
        MILESTONES_PY.write_text(ms_src, encoding="utf-8")

    # sanity: the edited modules must still import
    subprocess.run([sys.executable, "-c", "import main, milestones"], check=True, cwd=ROOT)
    print(f"\nAdded {len(added)} game(s); main.py/milestones.py import cleanly.")

    if args.commit:
        names = ", ".join(n for n, _, _ in added)
        body = "\n".join(
            f"- {n} (universe {u}" + (f", peak {p:,})" if p is not None else ", no seeded peak)")
            for n, u, p in added
        )
        msg = f"feat: track {names}\n\n{body}\n"
        subprocess.run(["git", "add", "main.py", "milestones.py"], check=True, cwd=ROOT)
        subprocess.run(["git", "commit", "-q", "-m", msg], check=True, cwd=ROOT)
        print("committed:", subprocess.run(["git", "log", "--oneline", "-1"], capture_output=True,
                                           text=True, cwd=ROOT).stdout.strip())
        if args.push:
            subprocess.run(["git", "push", "origin", "HEAD:main"], check=True, cwd=ROOT)
            print("pushed to origin/main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
