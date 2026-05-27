"""
Vercel serverless handler for the Plaza watcher.
Called every 5 minutes by cron-job.org.
seen_ids.json is persisted in the GitHub repo via the API (no local filesystem).
"""

from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import base64
import json
import os
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EMAIL_TO         = os.environ.get("EMAIL_TO", "")
EMAIL_FROM       = os.environ.get("EMAIL_FROM", "")
EMAIL_PASS       = os.environ.get("EMAIL_PASS", "")
CRON_SECRET      = os.environ.get("CRON_SECRET", "")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "")   # "owner/repo"

LIMAPAD_ONLY        = True
TELEGRAM_RETRIES    = 3
TELEGRAM_RETRY_DELAY = 2

API_BASE  = "https://mosaic-plaza-aanbodapi.zig365.nl/api/v1/actueel-aanbod"
PAGE_SIZE = 60
SEEN_PATH = "seen_ids.json"
AMS       = ZoneInfo("Europe/Zurich")


# ── GitHub state ──────────────────────────────────────────────────────────────

def _gh_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def load_seen():
    url  = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{SEEN_PATH}"
    resp = requests.get(url, headers=_gh_headers(), timeout=8)
    if resp.status_code == 404:
        return set(), None
    resp.raise_for_status()
    d = resp.json()
    return set(json.loads(base64.b64decode(d["content"]))), d["sha"]


def save_seen(seen, sha):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{SEEN_PATH}"
    for attempt in range(2):
        body = {
            "message": "chore: update seen_ids [skip ci]",
            "content": base64.b64encode(json.dumps(list(seen)).encode()).decode(),
            "committer": {"name": "plaza-watcher[bot]", "email": "watcher@noreply"},
        }
        if sha:
            body["sha"] = sha
        r = requests.put(url, headers=_gh_headers(), json=body, timeout=8)
        if r.status_code == 409 and attempt == 0:
            print(f"  [save_seen] SHA conflict on attempt 1, re-fetching SHA...")
            fresh = requests.get(url, headers=_gh_headers(), timeout=8)
            if fresh.status_code == 200:
                sha = fresh.json()["sha"]
                continue
        r.raise_for_status()
        return


# ── Plaza API ─────────────────────────────────────────────────────────────────

def make_fingerprint(item):
    return f"{item.get('id', '')}|{item.get('publicationDate', '') or ''}"


def fetch_all_listings():
    hdrs  = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    items, page = [], 0
    while True:
        url   = (f"{API_BASE}?limit={PAGE_SIZE}&locale=en_GB"
                 f"&page={page}&sort=%2BreactionData.aangepasteTotaleHuurprijs")
        resp  = requests.get(url, headers=hdrs, timeout=10)
        resp.raise_for_status()
        d     = resp.json()
        chunk = d if isinstance(d, list) else d.get("data", [])
        items.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            break
        page += 1
    return items


def filter_listings(all_items):
    out = {}
    for item in all_items:
        if not item.get("id"):
            continue
        city     = (item.get("city")        or {}).get("name", "")          or ""
        region   = (item.get("regio")       or {}).get("name", "")          or ""
        typ      = (item.get("dwellingType") or {}).get("localizedName", "") or ""
        street   = item.get("street", "") or ""
        reaction = item.get("reactionData") or {}

        if "utrecht" not in f"{city} {region}".lower():
            continue
        if "studio" not in typ.lower():
            continue
        if LIMAPAD_ONLY and "limapad" not in street.lower():
            continue

        # Skip listings whose reaction period is closed / in selection.
        # isOpenForReaction is False once the deadline has passed; if the field
        # is absent we let it through (fail-open) so we don't miss a new format.
        if reaction.get("isOpenForReaction") is False:
            continue

        fp     = make_fingerprint(item)
        out[fp] = {
            "address":  f"{street} {item.get('houseNumber', '') or ''} {item.get('houseNumberAddition', '') or ''}".strip(),
            "city":     city,
            "rent":     item.get("totalRent") or "?",
            "area":     item.get("areaDwelling"),
            "floor":    (item.get("floor") or {}).get("localizedName", "") or "",
            "pub_date": item.get("publicationDate", "") or "",
            "url":      f"https://plaza.newnewnew.space/en/object/{item.get('urlKey', '')}",
        }
    return out


# ── Notifications ─────────────────────────────────────────────────────────────

def send_telegram(listing):
    if not TELEGRAM_TOKEN:
        return True
    area  = f" | {listing['area']}m2" if listing['area'] else ""
    floor = f" | {listing['floor']}"  if listing['floor'] else ""
    msg   = (
        f"NEW Limapad Studio in Utrecht!\n\n"
        f"Address : {listing['address']}, {listing['city']}\n"
        f"Rent    : EUR {listing['rent']}/month{area}{floor}\n\n"
        f"Link: {listing['url']}\n\nReact snel - deze gaan snel weg!"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(1, TELEGRAM_RETRIES + 1):
        try:
            r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=8)
            if r.status_code == 200:
                return True
            print(f"  [Telegram] attempt {attempt} failed: HTTP {r.status_code}")
        except requests.RequestException as e:
            print(f"  [Telegram] attempt {attempt} failed: {e}")
        if attempt < TELEGRAM_RETRIES:
            time.sleep(TELEGRAM_RETRY_DELAY)
    return False


def send_email(listing):
    if not (EMAIL_TO and EMAIL_FROM and EMAIL_PASS):
        return
    area    = f" | {listing['area']}m2" if listing['area'] else ""
    subject = f"New Limapad studio: {listing['address']} -- EUR {listing['rent']}/mo"
    body    = "\n".join([
        "=" * 50,
        "  New studio at Limapad, Utrecht!",
        f"  Checked at: {datetime.now(AMS).strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 50, "",
        f"  Address  : {listing['address']}, {listing['city']}",
        f"  Rent     : EUR {listing['rent']}/month{area}",
        f"  Floor    : {listing['floor']}",
        f"  Published: {listing['pub_date']}",
        f"  Link     : {listing['url']}",
        "", "-" * 50,
        "React quickly -- these go fast!",
        "",
        "Browse all available studios:",
        "https://plaza.newnewnew.space/en/availables-places/living-place#?gesorteerd-op=prijs%2B&locatie=Nederland%2B-%2BUtrecht",
    ])
    msg            = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    recipients     = [a.strip() for a in EMAIL_TO.split(",") if a.strip()]
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=8) as srv:
        srv.login(EMAIL_FROM, EMAIL_PASS)
        srv.sendmail(EMAIL_FROM, recipients, msg.as_string())


# ── Watcher logic ─────────────────────────────────────────────────────────────

def run_watcher():
    now     = datetime.now(AMS).strftime("%Y-%m-%d %H:%M:%S")
    seen, sha = load_seen()
    current   = filter_listings(fetch_all_listings())
    new       = {fp: v for fp, v in current.items() if fp not in seen}

    if new:
        print(f"[{now}] {len(new)} new listing(s)!")
        alerted = set()
        for fp, listing in new.items():
            print(f"  -> {listing['address']} EUR {listing['rent']}/mo")
            send_telegram(listing)
            send_email(listing)
            alerted.add(fp)
        try:
            save_seen(seen | alerted, sha)
            print(f"  [save_seen] OK — {len(alerted)} fingerprint(s) persisted.")
        except Exception as e:
            print(f"  [save_seen] ERROR — fingerprints NOT saved, will re-alert next run: {e}")
    else:
        label = "Limapad studios" if LIMAPAD_ONLY else "Utrecht studios"
        print(f"[{now}] No new {label}. ({len(current)} live, {len(current)} matched)")


# ── Vercel entry point ────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        secret = params.get("secret", [""])[0]
        if CRON_SECRET and secret != CRON_SECRET:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return
        try:
            run_watcher()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            print(f"Error: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, format, *args):
        pass
