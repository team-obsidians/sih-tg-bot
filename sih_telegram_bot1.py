"""
SIH 2026 Software PS — Telegram Notifier Bot
================================================================
Ports the scraping/parsing logic of the original terminal script into
a long-running Telegram bot that:

  1. Polls https://sih.gov.in/sih2026PS every CHECK_INTERVAL_SECONDS
     (default 60s) for the Software-category problem statements.
  2. Compares the new snapshot against the last-saved one and, if
     anything changed, pushes a Telegram notification to every
     subscribed chat containing:
       - how much total submissions moved
       - which PS gained submissions (and by how much)
       - any brand-new PS that appeared
       - the current "least-submitted" leaderboard (best opportunities)
  3. Exposes /start, /stop, /stats, /help commands so users can
     subscribe/unsubscribe and pull an on-demand snapshot any time.

Cookie handling
----------------
The original script re-launches a headless Chromium via Playwright on
every single run just to harvest a fresh `laravel_session` cookie.
Doing that every 60 seconds would be slow and heavy, so here a
`Scraper` wraps one long-lived `requests.Session` and only re-harvests
cookies (via Playwright) when the current session actually gets
rejected (redirected to login / non-200). In steady state, most
1-minute checks are a single lightweight HTTP GET.

Setup
-----
  1. pip install -r requirements.txt
  2. playwright install chromium
  3. Create a bot with @BotFather on Telegram, copy its token
  4. export SIH_BOT_TOKEN="123456789:AA...your-token..."
  5. python sih_telegram_bot.py
  6. In Telegram, open a chat with your bot and send /start

State is kept in ./data/state.json (last snapshot) and
./data/subscribers.json (chat ids to notify) so restarts don't
re-send a giant "everything changed" notification.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import os
import re
import sys
import threading
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ══════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════
BASE_URL = "https://sih.gov.in"
PS_PAGE = f"{BASE_URL}/sih2026PS"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Safari/537.36"
)

CHECK_INTERVAL_SECONDS = 60
TOP_LEAST = 10  # "least 10 PS submissions" leaderboard size

BOT_TOKEN = os.environ.get("SIH_BOT_TOKEN", "")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("sih_bot")


# ══════════════════════════════════════════════
# HEALTH-CHECK SERVER
# ══════════════════════════════════════════════
# Render (and similar PaaS free tiers) only keep a "Web Service" alive, and
# they detect that by watching for something listening on $PORT. This bot
# has nothing to do with HTTP, but a tiny background thread serving 200 OK
# satisfies that check. Point an external uptime pinger (see README) at this
# endpoint every ~10 minutes to also stop the free tier from sleeping after
# 15 minutes of no inbound requests.
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — required method name
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # silence per-request access logging


def start_health_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health-server").start()
    logger.info("Health-check server listening on 0.0.0.0:%d", port)


# ══════════════════════════════════════════════
# PERSISTENCE HELPERS
# ══════════════════════════════════════════════
def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Corrupt JSON at %s — resetting.", path)
    return default


def save_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def load_subscribers() -> set:
    return set(load_json(SUBSCRIBERS_FILE, []))


def save_subscribers(subs: set) -> None:
    save_json(SUBSCRIBERS_FILE, sorted(subs))


def load_state() -> Dict[str, Dict[str, Any]]:
    return load_json(STATE_FILE, {})


def save_state(state: Dict[str, Dict[str, Any]]) -> None:
    save_json(STATE_FILE, state)


# ══════════════════════════════════════════════
# SCRAPER / PARSER  (ported from the original script)
# ══════════════════════════════════════════════
_MODAL_MARKER = re.compile(
    r"\s*[×✕✖xX]\s*Problem\s+Statement\s+Details",
    re.IGNORECASE,
)


def _clean_title(raw: str) -> str:
    if not raw:
        return raw
    parts = _MODAL_MARKER.split(raw, maxsplit=1)
    return parts[0].strip()


def _first_int(text: Any) -> Optional[int]:
    if text is None:
        return None
    m = re.search(r"\d+", str(text).replace(",", ""))
    return int(m.group()) if m else None


def _detect_columns(headers: List[str]) -> Dict[str, int]:
    lowered = [h.lower() for h in headers]

    def find(*keys: str, default: int) -> int:
        for i, h in enumerate(lowered):
            if any(k in h for k in keys):
                return i
        return default

    return {
        "ps_id": find("ps number", "ps no", "ps id",
                      "problem statement id", "problem id", default=4),
        "title": find("problem statement title", "title", default=2),
        "category": find("category", "type", default=3),
        "theme": find("theme", "domain", default=6),
        "organization": find("organization", "ministry", "department", default=1),
        "submissions": find("submitted idea", "submission",
                            "idea count", "proposal", default=5),
    }


def parse_html(html_text: str) -> List[Dict[str, Any]]:
    """Parses the PS table and returns only Software-category rows."""
    soup = BeautifulSoup(html_text, "lxml")
    table = soup.find("table")
    if not table:
        logger.error("No <table> found on page — layout may have changed.")
        return []

    headers: List[str] = []
    thead = table.find("thead")
    if thead:
        for th in thead.find_all(["th", "td"]):
            headers.append(th.get_text(" ", strip=True))

    cols = _detect_columns(headers)

    tbody = table.find("tbody") or table
    data_rows: List[List[str]] = []

    for tr in tbody.find_all("tr"):
        classes = [c.lower() for c in (tr.get("class") or [])]
        if "child" in classes:
            continue
        cells = tr.find_all("td", recursive=False)  # direct children only
        if not cells:
            continue
        if len(cells) == 1 and int(cells[0].get("colspan", 1)) > 1:
            continue
        data_rows.append([c.get_text(" ", strip=True) for c in cells])

    def get(row: List[str], idx: int) -> str:
        return row[idx] if 0 <= idx < len(row) else ""

    records: List[Dict[str, Any]] = []
    seen_ids: set = set()

    for row in data_rows:
        rec = {
            "ps_id": get(row, cols["ps_id"]).strip(),
            "title": _clean_title(get(row, cols["title"])),
            "category": get(row, cols["category"]).strip(),
            "theme": get(row, cols["theme"]).strip(),
            "organization": get(row, cols["organization"]).strip(),
            "submissions": _first_int(get(row, cols["submissions"])) or 0,
        }
        for k in ("ps_id", "category", "theme", "organization"):
            rec[k] = re.sub(r"<[^>]+>", "", rec[k]).strip()

        if rec["category"].lower() != "software":
            continue
        if rec["ps_id"] and rec["ps_id"] in seen_ids:
            continue
        if rec["ps_id"]:
            seen_ids.add(rec["ps_id"])
        records.append(rec)

    return records


async def harvest_cookies(headless: bool = True) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            # Render's containers run without extra sandbox capabilities and
            # have a small /dev/shm — both flags are the standard fix for
            # Chromium inside restricted containers (Render, plain Docker,
            # Heroku, etc). Harmless on a normal VM too.
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-IN",
        )
        page = await context.new_page()
        try:
            await page.goto(PS_PAGE, wait_until="networkidle", timeout=90_000)
            await page.wait_for_timeout(3_000)
        except Exception as e:  # noqa: BLE001 — best-effort navigation
            logger.warning("Navigation warning while harvesting cookies: %s", e)
        for c in await context.cookies():
            if c.get("name") and c.get("value"):
                cookies[c["name"]] = c["value"]
        await browser.close()
    return cookies


def build_session(cookies: Dict[str, str]) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8"),
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Referer": BASE_URL + "/",
    })
    for k, v in cookies.items():
        s.cookies.set(k, v, domain="sih.gov.in")
    if "XSRF-TOKEN" in cookies:
        s.headers["X-XSRF-TOKEN"] = cookies["XSRF-TOKEN"]
    return s


class Scraper:
    """Wraps a requests.Session, re-harvesting cookies via Playwright
    only when the current session gets rejected — keeps 1-minute
    polling cheap since most checks are a single plain HTTP GET."""

    def __init__(self) -> None:
        self._session: Optional[requests.Session] = None

    async def _refresh_session(self) -> None:
        logger.info("Harvesting fresh session cookies via Playwright…")
        cookies = await harvest_cookies(headless=True)
        if "laravel_session" not in cookies:
            raise RuntimeError(
                "laravel_session cookie not obtained — the portal may be "
                "blocking headless Chromium. Try SIH_HEADFUL=1."
            )
        self._session = build_session(cookies)

    async def fetch_records(self) -> List[Dict[str, Any]]:
        if self._session is None:
            await self._refresh_session()

        loop = asyncio.get_running_loop()
        r = await loop.run_in_executor(None, lambda: self._session.get(PS_PAGE, timeout=60))

        needs_refresh = (
            r.status_code != 200
            or "login" in r.url.lower()
            or "sign in" in r.text[:2000].lower()
        )
        if needs_refresh:
            logger.info("Session expired/rejected — refreshing and retrying once.")
            await self._refresh_session()
            r = await loop.run_in_executor(None, lambda: self._session.get(PS_PAGE, timeout=60))
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code} even after refreshing session.")

        records = parse_html(r.text)
        if not records:
            raise RuntimeError("Parsed zero Software PS records — page layout may have changed.")
        return records


scraper = Scraper()


# ══════════════════════════════════════════════
# STATS / MESSAGE FORMATTING  (HTML parse mode — simple to escape)
# ══════════════════════════════════════════════
def esc(text: Any) -> str:
    return html_lib.escape(str(text))


def compute_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    subs = [r["submissions"] for r in records]
    counter = Counter(subs)
    return {
        "total_ps": len(records),
        "total_submissions": sum(subs),
        "avg": (sum(subs) / len(subs)) if subs else 0.0,
        "median": sorted(subs)[len(subs) // 2] if subs else 0,
        "min": min(subs) if subs else 0,
        "max": max(subs) if subs else 0,
        "zero_count": counter.get(0, 0),
    }


def least_submitted(records: List[Dict[str, Any]], n: int = TOP_LEAST) -> List[Dict[str, Any]]:
    return sorted(records, key=lambda r: (r["submissions"], r["ps_id"]))[:n]


def format_stats_message(records: List[Dict[str, Any]], header: str) -> str:
    stats = compute_stats(records)
    least = least_submitted(records, TOP_LEAST)

    lines = [f"<b>{esc(header)}</b>", ""]
    lines.append(f"🕒 {esc(datetime.now().strftime('%d %b %Y, %H:%M:%S'))}")
    lines.append("")
    lines.append("📊 <b>Summary</b>")
    lines.append(f"Total Software PS: <code>{stats['total_ps']}</code>")
    lines.append(f"Total Submissions: <code>{stats['total_submissions']:,}</code>")
    lines.append(f"Average / PS: <code>{stats['avg']:.1f}</code>")
    lines.append(f"Median / PS: <code>{stats['median']}</code>")
    lines.append(f"Min / Max: <code>{stats['min']} / {stats['max']}</code>")
    lines.append(f"Zero-submission PS: <code>{stats['zero_count']}</code>")
    lines.append("")
    lines.append(f"🏆 <b>Least-Submitted (Top {len(least)})</b>")
    for i, r in enumerate(least, 1):
        title = r["title"][:45]
        lines.append(f"{i}. <code>{esc(r['ps_id'])}</code> — {esc(title)} (<b>{r['submissions']}</b>)")

    return "\n".join(lines)


def format_change_message(changes: Dict[str, Any]) -> str:
    lines = ["🔔 <b>SIH 2026 — Change Detected</b>", ""]

    if changes["new_total_subs"] != changes["old_total_subs"]:
        delta = changes["new_total_subs"] - changes["old_total_subs"]
        sign = "+" if delta > 0 else ""
        lines.append(
            f"Total submissions: <code>{changes['old_total_subs']:,}</code> → "
            f"<code>{changes['new_total_subs']:,}</code> ({sign}{delta})"
        )

    if changes["increased"]:
        lines.append("")
        lines.append("📈 <b>Submissions increased:</b>")
        for c in changes["increased"][:15]:
            lines.append(
                f"<code>{esc(c['ps_id'])}</code> — {esc(c['title'][:40])}: "
                f"{c['old']} → <b>{c['new']}</b> (+{c['new'] - c['old']})"
            )
        if len(changes["increased"]) > 15:
            lines.append(f"<i>...and {len(changes['increased']) - 15} more</i>")

    if changes["new_ps"]:
        lines.append("")
        lines.append("🆕 <b>New PS listed:</b>")
        for r in changes["new_ps"][:10]:
            lines.append(f"<code>{esc(r['ps_id'])}</code> — {esc(r['title'][:45])}")

    lines.append("")
    lines.append(f"🏆 <b>Least-Submitted (Top {TOP_LEAST})</b>")
    for i, r in enumerate(changes["least"], 1):
        lines.append(f"{i}. <code>{esc(r['ps_id'])}</code> — {esc(r['title'][:40])} (<b>{r['submissions']}</b>)")

    return "\n".join(lines)


# ══════════════════════════════════════════════
# CHANGE DETECTION
# ══════════════════════════════════════════════
def diff_records(
    old_state: Dict[str, Dict[str, Any]],
    records: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not old_state:
        return None  # first run ever — nothing to compare against yet

    new_state = {r["ps_id"]: r for r in records}
    old_total = sum(v["submissions"] for v in old_state.values())
    new_total = sum(r["submissions"] for r in records)

    increased: List[Dict[str, Any]] = []
    new_ps: List[Dict[str, Any]] = []
    for ps_id, rec in new_state.items():
        old_rec = old_state.get(ps_id)
        if old_rec is None:
            new_ps.append(rec)
        elif rec["submissions"] > old_rec["submissions"]:
            increased.append({
                "ps_id": ps_id,
                "title": rec["title"],
                "old": old_rec["submissions"],
                "new": rec["submissions"],
            })

    if not increased and not new_ps:
        return None

    return {
        "old_total_subs": old_total,
        "new_total_subs": new_total,
        "increased": sorted(increased, key=lambda c: c["new"] - c["old"], reverse=True),
        "new_ps": new_ps,
        "least": least_submitted(records, TOP_LEAST),
    }


# ══════════════════════════════════════════════
# TELEGRAM COMMAND HANDLERS
# ══════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    subs = load_subscribers()
    if chat_id not in subs:
        subs.add(chat_id)
        save_subscribers(subs)
        await update.message.reply_text(
            "✅ Subscribed! You'll get a message whenever Software PS "
            f"submission counts change (checked every {CHECK_INTERVAL_SECONDS}s).\n\n"
            "/stats — snapshot right now\n"
            "/stop — unsubscribe"
        )
    else:
        await update.message.reply_text("You're already subscribed. /stats for a snapshot, /stop to unsubscribe.")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    subs = load_subscribers()
    if chat_id in subs:
        subs.discard(chat_id)
        save_subscribers(subs)
    await update.message.reply_text("🛑 Unsubscribed. Send /start any time to resume.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Fetching latest data…")
    try:
        records = await scraper.fetch_records()
    except Exception as e:  # noqa: BLE001
        logger.exception("fetch_records failed in /stats")
        await update.message.reply_text(f"❌ Failed to fetch data: {esc(e)}")
        return
    msg = format_stats_message(records, "Current Snapshot")
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start — subscribe to change notifications\n"
        "/stop — unsubscribe\n"
        "/stats — get the current snapshot right now"
    )


# ══════════════════════════════════════════════
# POLLING JOB  (runs every CHECK_INTERVAL_SECONDS)
# ══════════════════════════════════════════════
async def check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        records = await scraper.fetch_records()
    except Exception as e:  # noqa: BLE001 — keep the loop alive on transient errors
        logger.warning("Polling fetch failed: %s", e)
        return

    old_state = load_state()
    changes = diff_records(old_state, records)

    # Always persist the latest snapshot, even if nothing "changed"
    save_state({r["ps_id"]: r for r in records})

    if changes is None:
        logger.info("No change detected (%d Software PS tracked).", len(records))
        return

    msg = format_change_message(changes)
    subs = load_subscribers()
    logger.info("Change detected — notifying %d subscriber(s).", len(subs))
    for chat_id in subs:
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to message %s: %s", chat_id, e)


# ══════════════════════════════════════════════
# ENTRY
# ══════════════════════════════════════════════
def main() -> None:
    if not BOT_TOKEN:
        print("Set the SIH_BOT_TOKEN environment variable to your bot token from @BotFather.")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("help", cmd_help))

    if app.job_queue is None:
        print("job-queue extra not installed. Run: pip install \"python-telegram-bot[job-queue]\"")
        sys.exit(1)

    app.job_queue.run_repeating(check_job, interval=CHECK_INTERVAL_SECONDS, first=5)

    # Only needed on platforms (like Render) that require a bound $PORT to
    # consider the service "up" — harmless everywhere else.
    if os.environ.get("PORT"):
        start_health_server()

    logger.info("Bot starting — polling sih.gov.in every %ds", CHECK_INTERVAL_SECONDS)
    app.run_polling()


if __name__ == "__main__":
    main()
