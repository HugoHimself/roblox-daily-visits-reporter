"""
Single source of truth for the tracked-game roster: data/games.json.

Each entry: {"name": str, "universe_id": int, "ccu_peak": int | null}

  name        display name used in Slack posts and the dashboard
  universe_id Roblox *universe* id (not the place id in the game URL)
  ccu_peak    known all-time concurrent-player peak, used to floor the
              milestone engine so a normal day is never called a "record"

Add games with `python add_game.py <placeId|url>[=ccu_peak]` rather than
editing this file by hand; the script resolves and verifies the ids.
"""

from __future__ import annotations

import json
from pathlib import Path

GAMES_FILE = Path(__file__).parent / "data" / "games.json"


def load(path: Path = GAMES_FILE) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        roster = json.load(f)
    seen: set[int] = set()
    for g in roster:
        uid = int(g["universe_id"])
        if uid in seen:
            raise ValueError(f"data/games.json: duplicate universe_id {uid}")
        seen.add(uid)
        if not g.get("name"):
            raise ValueError(f"data/games.json: universe {uid} has no name")
    return roster


def save(roster: list[dict], path: Path = GAMES_FILE) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(roster, indent=2, ensure_ascii=False) + "\n")


def games_dict(roster: list[dict] | None = None) -> dict[str, int]:
    """{name: universe_id} — the shape the rest of the code base expects."""
    return {g["name"]: int(g["universe_id"]) for g in (roster or load())}


def known_ccu_peaks(roster: list[dict] | None = None) -> dict[str, int]:
    """{str(universe_id): peak} for every game with a seeded peak."""
    return {str(g["universe_id"]): int(g["ccu_peak"])
            for g in (roster or load()) if g.get("ccu_peak")}
