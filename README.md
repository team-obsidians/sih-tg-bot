# SIH 2026 Software PS — Telegram Notifier Bot

Watches `https://sih.gov.in/sih2026PS` every **60 seconds** and pushes a
Telegram message whenever something changes among Software-category
problem statements:

- total submission count moved (and by how much)
- which specific PS gained submissions, and by how much
- any brand-new PS that appeared
- the current **least-submitted leaderboard** (top 10 lowest-competition PS)

You can also pull a snapshot on demand with `/stats`.

## 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

`playwright install chromium` downloads a headless browser build —
needed once, only to harvest a fresh session cookie from the portal.

## 2. Create your bot

1. Open Telegram, message **@BotFather**
2. `/newbot`, follow the prompts, copy the token it gives you
   (looks like `123456789:AAExampleToken...`)

## 3. Run it

```bash
export SIH_BOT_TOKEN="123456789:AAExampleToken..."
python sih_telegram_bot.py
```

Leave this running (a small VPS, a Raspberry Pi, `screen`/`tmux`, or a
`systemd` service all work — see below).

## 4. Subscribe

In Telegram, open a chat with your bot and send:

- `/start` — subscribe to change notifications
- `/stats` — get the current snapshot immediately
- `/stop` — unsubscribe
- `/help` — list commands

Multiple people can `/start` the same bot; everyone subscribed gets
notified.

## How it decides "changed"

Every check, it fetches the page and compares the new submission
counts against the last snapshot saved in `data/state.json`. The very
first run just saves a baseline and does **not** notify (there's
nothing to compare against yet) — you'll see the first real
notification on the second check onward, if anything moved.

## Notes on reliability

- The portal requires a session cookie. Rather than re-launching a
  browser on every 60-second check, the bot keeps one HTTP session
  alive and only re-harvests cookies via Playwright when the site
  actually rejects the session (expired cookie, redirect to login).
  In steady state, most checks are one plain HTTP request.
- If the portal ever starts blocking headless Chromium, set
  `headless=False` in `harvest_cookies(...)` inside the script (you'll
  need a machine with a display, or a virtual display like `xvfb`).
- All state lives in `./data/` as plain JSON — delete
  `data/state.json` if you ever want to force a fresh baseline instead
  of a "first run" comparison.

## Deploying to Render (free tier)

Render's free tier only keeps **Web Services** running (background workers
are a paid-only service type there), and free web services normally sleep
after 15 minutes with no inbound HTTP request. The files in this repo
already work around both of those:

- `sih_telegram_bot.py` starts a tiny health-check HTTP thread bound to
  Render's `$PORT` whenever `$PORT` is set — that's what makes Render treat
  this as a "live" web service.
- You'll point an external pinger at that health endpoint every ~10 minutes
  so Render never sees 15 idle minutes and never sleeps it (details below).
- `Dockerfile` uses the official Playwright image, which has Chromium and
  all its system libraries pre-installed — this avoids fighting Render's
  native Python buildpack over missing Chromium dependencies.

**One thing to accept going in:** Render's free tier has no persistent
disk. `./data/state.json` and `./data/subscribers.json` reset to empty
every time the container restarts (a redeploy, or a crash). In practice
this just means the bot silently re-establishes its baseline on the next
check after a restart, and re-subscribes are needed after a restart too —
acceptable for a personal notifier, but worth knowing rather than
discovering.

### Steps

1. **Push this folder to a GitHub repo** (Render deploys from Git).
2. In the [Render dashboard](https://dashboard.render.com), click
   **New > Web Service**, connect that repo.
3. Render should auto-detect the `Dockerfile` and set **Environment:
   Docker**. Instance type: **Free**.
4. Under **Environment Variables**, add:
   - `SIH_BOT_TOKEN` = your token from @BotFather
5. Click **Create Web Service**. First build takes a few minutes (pulling
   the Playwright base image). Once live, note the assigned URL —
   something like `https://sih-telegram-bot-xxxx.onrender.com`.
   (Alternatively: use **New > Blueprint** and point it at this repo —
   `render.yaml` is already set up so you only need to fill in the token.)
6. **Set up the keep-alive ping** so Render never sleeps this service:
   - Create a free account at [cron-job.org](https://cron-job.org) (or
     UptimeRobot, or any similar free uptime pinger).
   - Add a new job: URL = `https://<your-app>.onrender.com/`, interval =
     every 10 minutes.
   - That's it — every ping resets Render's 15-minute inactivity clock.
7. In Telegram, message your bot `/start` to subscribe.

### Staying inside the free hours

Render's free plan grants 750 instance-hours per workspace per month — a
30-day month is about 720 hours, so **one** service pinged 24/7 fits
comfortably, but a second always-on free service would push you over. Keep
this as your only free web service on the account if you want it running
continuously all month.

### If you'd rather not fight a free tier

Everything above is a genuine workaround — a health-check thread bolted
onto a bot that isn't a website, plus an external pinger to defeat a sleep
timer that exists on purpose. It works, but a small always-on VM (e.g.
Oracle Cloud's Always Free tier) avoids all of it and gives you a normal
filesystem that doesn't reset — just `pip install`, `playwright install
chromium`, and use the systemd setup below directly, no Docker or
health-check thread needed.

## Running continuously (optional — systemd example)

```ini
# /etc/systemd/system/sih-bot.service
[Unit]
Description=SIH 2026 PS Telegram Notifier
After=network.target

[Service]
WorkingDirectory=/path/to/sih-bot
Environment=SIH_BOT_TOKEN=123456789:AAExampleToken...
ExecStart=/usr/bin/python3 sih_telegram_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sih-bot
```

## Adjusting things

All at the top of `sih_telegram_bot.py`:

- `CHECK_INTERVAL_SECONDS` — polling interval (default `60`)
- `TOP_LEAST` — size of the least-submitted leaderboard (default `10`)
