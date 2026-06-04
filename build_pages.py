#!/usr/bin/env python3
"""Generate docs/portfolio.json for the static GitHub Pages dashboard.

GitHub Pages can't run the Flask backend, and Roblox blocks browser (CORS)
calls — so a scheduled Action runs this script, which computes the exact same
payload as the /api/portfolio route (live Roblox visits/players/ratings +
manual games + visits-weighted rating) and writes it to docs/portfolio.json.
The static page (docs/index.html) reads that file, same-origin, no CORS.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from dashboard import compute_portfolio

OUT = Path(__file__).parent / "docs" / "portfolio.json"


def main() -> None:
    data = compute_portfolio()
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    t = data["totals"]
    print(f"Wrote {OUT} — {t['count']} games, total {t['visits']:,}, "
          f"weighted rating {t['rating']}%.")


if __name__ == "__main__":
    main()
