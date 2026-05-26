"""
Plaza Utrecht – Limapad Studio Watcher
========================================
Polls the Plaza API and sends a Telegram message (+ optional email)
the moment a new studio appears at Limapad, Utrecht.

QUICK SETUP (5 minutes):
  1. pip install requests
  2. Create a Telegram bot -> talk to @BotFather on Telegram, type /newbot
  3. Fill in TELEGRAM_TOKEN and TELEGRAM_CHAT_ID below (see instructions)
  4. Optionally fill in email settings too
  5. Schedule to run every 5-10 minutes (see bottom of file)
"""

import requests
import json
import os
import smtplib
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from zoneinfo import ZoneInfo

AMS = ZoneInfo("Europe/Amsterdam")

# --- TELEGRAM SETUP (recommended) ---------------------------------------------
#
# Step 1: Open Telegram, search for @BotFather, send /newbot, follow the steps.
#         You will receive a token like:  7123456789:AAF...xyz
#
# Step 2: Start a chat with your new bot (search its name, press Start).
#         Then open this URL in your browser to get your chat ID:
#         https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
#         Look for "chat":{"id": 123456789} -- that number is your chat ID.
#
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

# --- EMAIL SETUP (optional, leave blank if using Telegram only) ---------------
EMAIL_TO   = os.environ.get("EMAIL_TO", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")

# --- FILTER SETTINGS ----------------------------------------------------------
LIMAPAD_ONLY = True   # Set False to alert on ANY Utrecht studio

# --- RETRY SETTINGS -----------------------------------------------------------
TELEGRAM_RETRIES = 3   # number of send attempts before giving up
TELEGRAM_RETRY_DELAY = 2  # seconds between retries

# --- DO NOT CHANGE BELOW THIS LINE --------------------------------------------

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_ids.json")

API_BASE  = "https://mosaic-plaza-aanbodapi.zig365.nl/api/v1/actueel-aanbod"
PAGE_SIZE = 60


def make_fingerprint(item):
    """
    Unique key per listing *publication*.
    Combines id + publicationDate so that:
      - a re-listed apartment with a new publication date triggers again
      - the same listing appearing in multiple poll cycles does NOT re-trigger
      - ID reuse (rare) cannot cause a missed alert
    """
    listing_id  = str(item.get("id", ""))
    pub_date    = item.get("publicationDate", "") or ""
    return f"{listing_id}|{pub_date}"


def fetch_all_listings():
    """Fetches ALL pages from the API. Stops when a page is not full."""
    headers  = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    all_items = []
    page = 0

    while True:
        url = (
            f"{API_BASE}?limit={PAGE_SIZE}&locale=en_GB"
            f"&page={page}&sort=%2BreactionData.aangepasteTotaleHuurprijs"
        )
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data  = response.json()
        items = data if isinstance(data, list) else data.get("data", [])

        all_items.extend(items)

        if len(items) < PAGE_SIZE:
            break   # last page reached

        page += 1

    return all_items


def filter_listings(all_items):
    """Apply city, type, and optional Limapad filters. Returns dict keyed by fingerprint."""
    listings = {}

    for item in all_items:
        if not item.get("id"):
            continue

        city_name   = (item.get("city") or {}).get("name", "") or ""
        region_name = (item.get("regio") or {}).get("name", "") or ""
        type_name   = (item.get("dwellingType") or {}).get("localizedName", "") or ""
        street      = item.get("street", "") or ""
        house_num   = item.get("houseNumber", "") or ""
        house_add   = item.get("houseNumberAddition", "") or ""
        total_rent  = item.get("totalRent") or "?"
        url_key     = item.get("urlKey", "")
        area        = item.get("areaDwelling")
        floor       = (item.get("floor") or {}).get("localizedName", "") or ""
        pub_date    = item.get("publicationDate", "") or ""

        if "utrecht" not in f"{city_name} {region_name}".lower():
            continue

        if "studio" not in type_name.lower():
            continue

        if LIMAPAD_ONLY and "limapad" not in street.lower():
            continue

        fingerprint = make_fingerprint(item)
        listings[fingerprint] = {
            "address":  f"{street} {house_num} {house_add}".strip(),
            "city":     city_name,
            "type":     type_name,
            "rent":     total_rent,
            "area":     area,
            "floor":    floor,
            "pub_date": pub_date,
            "url":      f"https://plaza.newnewnew.space/en/object/{url_key}",
        }

    return listings


def send_telegram(listing):
    """
    Send one Telegram message for a single listing.
    Retries up to TELEGRAM_RETRIES times on failure.
    Returns True on success, False if all attempts fail.
    """
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "YOUR_BOT_TOKEN_HERE":
        return True  # not configured, skip silently

    area_str  = f" | {listing['area']}m2" if listing['area'] else ""
    floor_str = f" | {listing['floor']}" if listing['floor'] else ""

    message = (
        f"NEW Limapad Studio in Utrecht!\n\n"
        f"Address : {listing['address']}, {listing['city']}\n"
        f"Rent    : EUR {listing['rent']}/month{area_str}{floor_str}\n\n"
        f"Link: {listing['url']}\n\n"
        f"React snel - deze gaan snel weg!"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    for attempt in range(1, TELEGRAM_RETRIES + 1):
        try:
            resp = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text":    message,
            }, timeout=10)

            if resp.status_code == 200:
                return True

            # Telegram returned a non-200 (e.g. 429 rate limit, bad token)
            error_info = resp.json().get("description", resp.text)
            print(f"  [Telegram] attempt {attempt}/{TELEGRAM_RETRIES} failed: "
                  f"HTTP {resp.status_code} - {error_info}")

        except requests.RequestException as e:
            print(f"  [Telegram] attempt {attempt}/{TELEGRAM_RETRIES} failed: {e}")

        if attempt < TELEGRAM_RETRIES:
            time.sleep(TELEGRAM_RETRY_DELAY)

    print(f"  [Telegram] GIVING UP after {TELEGRAM_RETRIES} attempts -- alert lost!")
    return False


def send_email(listing):
    """Send one email for a single listing."""
    if not EMAIL_TO or not EMAIL_FROM or not EMAIL_PASS:
        return

    area_str = f" | {listing['area']}m2" if listing['area'] else ""
    subject  = f"New Limapad studio: {listing['address']} -- EUR {listing['rent']}/mo"

    body = "\n".join([
        "=" * 50,
        "  New studio at Limapad, Utrecht!",
        f"  Checked at: {datetime.now(AMS).strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 50, "",
        f"  Address  : {listing['address']}, {listing['city']}",
        f"  Rent     : EUR {listing['rent']}/month{area_str}",
        f"  Floor    : {listing['floor']}",
        f"  Published: {listing['pub_date']}",
        f"  Link     : {listing['url']}",
        "",
        "-" * 50,
        "React quickly -- these go fast!",
    ])

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    recipients = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_FROM, EMAIL_PASS)
        server.sendmail(EMAIL_FROM, recipients, msg.as_string())


def main():
    now = datetime.now(AMS).strftime("%Y-%m-%d %H:%M:%S")

    # Load previously alerted fingerprints
    seen = set()
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                seen = set(json.load(f))
        except Exception:
            seen = set()

    # Fetch and filter
    try:
        all_items = fetch_all_listings()
        current   = filter_listings(all_items)
    except Exception as e:
        print(f"[{now}] ERROR fetching API: {e}")
        return

    # New = fingerprint not seen before
    new = {fp: v for fp, v in current.items() if fp not in seen}

    if new:
        print(f"[{now}] {len(new)} new listing(s)! Sending alerts...")
        alerted = set()

        for fp, listing in new.items():
            print(f"       -> {listing['address']} (published {listing['pub_date']}) "
                  f"-- EUR {listing['rent']}/mo")

            send_telegram(listing)
            send_email(listing)
            alerted.add(fp)

        updated_seen = seen | alerted
    else:
        label = "Limapad Utrecht studios" if LIMAPAD_ONLY else "Utrecht studios"
        print(f"[{now}] No new {label}. "
              f"({len(current)} matching live, {len(all_items)} total fetched)")
        updated_seen = seen

    with open(SEEN_FILE, "w") as f:
        json.dump(list(updated_seen), f)


if __name__ == "__main__":
    main()


# --- HOW TO SCHEDULE ----------------------------------------------------------
#
# PythonAnywhere (free, recommended -- runs 24/7 in the cloud):
#   1. pythonanywhere.com -> sign up free
#   2. Upload this file
#   3. Tasks tab -> add: python3 /home/yourname/plaza_watcher.py
#   4. Set to every 10 minutes (free tier limit)
#
# Mac/Linux (only while computer is on):
#   crontab -e
#   */5 * * * * python3 /path/to/plaza_watcher.py >> /path/to/watcher.log 2>&1
#
# Windows (only while computer is on):
#   Task Scheduler -> Create Basic Task -> repeat every 5 minutes
#
# ------------------------------------------------------------------------------
