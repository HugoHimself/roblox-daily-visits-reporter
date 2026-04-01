# Roblox Daily Visits Reporter

Posts yesterday's Roblox visit stats to a Slack channel every morning.

Each daily run fetches the current cumulative visit count from the Roblox public API and stores it as a snapshot. Visit counts are computed as the delta between consecutive snapshots (i.e. the 24-hour window between runs). Week-over-week % change compares the same weekday 7 days prior.

**Example Slack output:**
```
📊 Roblox Daily Visits — Tuesday, April 1

🟢 Active Games
  Hunted: 12,450 visits (+8.3% vs last Tue)
  Winx: 8,200 visits (-2.1% vs last Tue)

📈 Total: 20,650 visits (+4.2% vs last Tue)
```

---

## Setup

### 1. Clone and install dependencies

```bash
cd roblox-daily-visits-reporter
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create a Slack Bot

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch.
2. Under **OAuth & Permissions**, add the `chat:write` Bot Token Scope.
3. Install the app to your workspace.
4. Copy the **Bot User OAuth Token** (`xoxb-...`).
5. Invite the bot to `#chill-channel`: `/invite @your-bot-name`.

### 3. Set environment variables

```bash
export SLACK_BOT_TOKEN="xoxb-your-token-here"
```

Or create a `.env` file (never commit it) and load it before running:

```bash
# .env
SLACK_BOT_TOKEN=xoxb-your-token-here
```

---

## Running

**Dry run** — prints the message to the console, does not post to Slack:

```bash
python main.py --dry-run
```

**Live run** — fetches data and posts to Slack:

```bash
python main.py
```

> **Note:** The first run only stores a snapshot. You need at least two consecutive daily runs before visit deltas can be computed. WoW % comparisons require 8+ days of history.

---

## Scheduling with cron (10 AM CET)

Open your crontab:

```bash
crontab -e
```

Add this line (adjust paths to match your setup):

```cron
# Daily at 10:00 CET (UTC+1 standard, UTC+2 CEST)
# Adjust hour as needed for daylight saving time
0 9 * * * /path/to/roblox-daily-visits-reporter/.venv/bin/python /path/to/roblox-daily-visits-reporter/main.py >> /path/to/roblox-daily-visits-reporter/cron.log 2>&1
```

To ensure `SLACK_BOT_TOKEN` is available in the cron environment, either:

- Export it in the crontab itself: add `SLACK_BOT_TOKEN=xoxb-...` at the top of the crontab file.
- Or source a `.env` file in a wrapper shell script.

---

## Adding or deactivating games

Edit the `GAMES` dict at the top of `main.py`:

```python
GAMES = {
    "Active": {
        "Hunted": 136431686349723,
        "Winx": 76737571462455,
        "NewGame": 123456789,       # add here
    },
    "Non-Active": {
        "OldGame": 987654321,       # move here to keep history but exclude from totals
    },
}
```

Categories with no games are automatically hidden from the Slack message.

---

## Data storage

Snapshots are stored in `data/visits.json` — a plain JSON dict keyed by date:

```json
{
  "2026-03-31": {
    "136431686349723": 5000000,
    "76737571462455": 3000000
  },
  "2026-04-01": {
    "136431686349723": 5012450,
    "76737571462455": 3008200
  }
}
```

This file is gitignored by default. Back it up separately or remove it from `.gitignore` if you want to commit it.
