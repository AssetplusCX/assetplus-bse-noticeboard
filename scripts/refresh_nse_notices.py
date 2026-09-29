#!/usr/bin/env python3
"""
Fetch latest Mutual Fund circulars from NSE and merge into index.html's
NSE_NOTICES array. Mirrors scripts/refresh_notices.py (the BSE refresher):
only ever ADDS notices, only ever touches `const NSE_NOTICES = [...]`, and
never fails loudly -- if NSE can't be reached (its anti-bot protection is
stricter than BSE's and may block datacenter IPs like GitHub Actions
runners), the script leaves index.html untouched and exits cleanly so the
workflow never breaks the site.
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

IST = timezone(timedelta(hours=5, minutes=30))
INDEX = Path("index.html")

CIRCULARS_PAGE = "https://www.nseindia.com/resources/exchange-communication-circulars"
API_URL = "https://www.nseindia.com/api/circulars"
DEPT_CODE = "MF"  # "Mutual Fund" under NSE's "Trading" category, per /api/circulars-departments

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Ch-Ua": '"Chromium";v="120", "Not_A Brand";v="24", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "X-Requested-With": "XMLHttpRequest",
}

TOPIC_RULES = [
    (r"\bNFO\b|New Fund Offer|allotment date", "NFO Launch"),
    (r"Revoke of Temporary Suspension|Temporary Suspension|\bSuspension\b|Discontinuation", "Suspension / Discontinuation"),
    (r"Downtime|maintenance activity|Schedule Maintenance", "System Maintenance"),
    (r"\bMerger\b", "Scheme Merger"),
    (r"Change in|Change of|scheme name|Systematic Cycle date|Minimum application amount|Maximum application amount", "Scheme Modification"),
    (r"Availability of", "Scheme Availability"),
]


def classify_topic(subject: str) -> str:
    s = subject or ""
    for pat, topic in TOPIC_RULES:
        if re.search(pat, s, re.I):
            return topic
    return "Other"


def fetch_circulars(session: requests.Session, from_date: str, to_date: str):
    """fromDate/toDate must be DD-MM-YYYY, matching NSE's own UI."""
    params = {"fromDate": from_date, "toDate": to_date, "dept": DEPT_CODE}
    headers = dict(HEADERS)
    headers["Referer"] = CIRCULARS_PAGE
    headers["Accept"] = "application/json, text/plain, */*"
    r = session.get(API_URL, params=params, headers=headers, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = r.json()
    if not isinstance(data, list):
        data = data.get("data", []) if isinstance(data, dict) else []
    return data


def to_notice(item: dict) -> dict:
    cir_date = item.get("cirDate", "")
    date = ""
    if re.match(r"^\d{8}$", cir_date or ""):
        date = f"{cir_date[0:4]}-{cir_date[4:6]}-{cir_date[6:8]}"
    subject = item.get("sub", "").strip()
    return {
        "noticeNo": item.get("circDisplayNo", "").strip(),
        "date": date,
        "subject": subject,
        "segment": "Mutual Fund",
        "category": "Mutual Fund",
        "department": "Mutual Fund",
        "topic": classify_topic(subject),
        "pdf": item.get("circFilelink", ""),
        "source": "current",
        "exchange": "NSE",
    }


def load_existing_notices(html: str):
    m = re.search(r"const NSE_NOTICES\s*=\s*(\[.*?\])\s*;", html, re.S)
    if not m:
        raise SystemExit("Could not find const NSE_NOTICES = [...] in index.html")
    return json.loads(m.group(1))


def merge(existing, fresh):
    by_no = {n["noticeNo"]: n for n in existing}
    added = 0
    for n in fresh:
        if not n["noticeNo"] or not n["date"]:
            continue
        if n["noticeNo"] not in by_no:
            by_no[n["noticeNo"]] = n
            added += 1
    merged = sorted(
        by_no.values(),
        key=lambda x: (x.get("date", ""), x["noticeNo"]),
        reverse=True,
    )
    return merged, added


def update_html(html: str, notices) -> str:
    new_json = json.dumps(notices, ensure_ascii=False, separators=(",", ":"))
    html = re.sub(
        r"const NSE_NOTICES\s*=\s*\[.*?\]\s*;",
        f"const NSE_NOTICES = {new_json};",
        html,
        count=1,
        flags=re.S,
    )
    return html


def main():
    if not INDEX.exists():
        raise SystemExit("index.html not found — run from repo root")

    html = INDEX.read_text(encoding="utf-8")
    existing = load_existing_notices(html)
    print(f"Existing NSE notices: {len(existing)}")

    now_ist = datetime.now(IST)
    to_date = now_ist.strftime("%d-%m-%Y")
    from_date = (now_ist - timedelta(days=60)).strftime("%d-%m-%Y")

    fresh_raw = []
    try:
        session = requests.Session()
        # Warm-up requests so NSE's front door sets its usual session cookies
        # before we hit the JSON API -- same shape as a real browser visit
        # (home page first, then the circulars page that actually calls the API).
        warmup_headers = {k: v for k, v in HEADERS.items() if not k.startswith("Sec-Fetch")}
        warmup_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        session.get("https://www.nseindia.com/", headers=warmup_headers, timeout=30)
        session.get(CIRCULARS_PAGE, headers=warmup_headers, timeout=30)
        fresh_raw = fetch_circulars(session, from_date, to_date)
        print(f"Fetched {len(fresh_raw)} raw circulars from NSE ({from_date} to {to_date})")
    except Exception as e:
        print(f"WARNING: Could not fetch NSE circulars ({e}).", file=sys.stderr)
        print("NSE may be blocking this runner. Leaving index.html unchanged.", file=sys.stderr)
        sys.exit(0)

    if not fresh_raw:
        print("No circulars returned. Leaving index.html unchanged.")
        return

    fresh = [to_notice(item) for item in fresh_raw]
    fresh = [n for n in fresh if n["noticeNo"] and n["date"]]

    merged, added = merge(existing, fresh)
    print(f"Merged total: {len(merged)}  (newly added: {added})")

    new_html = update_html(html, merged)

    if new_html == html:
        print("No content change.")
        return

    INDEX.write_text(new_html, encoding="utf-8")
    print(f"Updated index.html NSE section (+{added} notices)")


if __name__ == "__main__":
    main()
