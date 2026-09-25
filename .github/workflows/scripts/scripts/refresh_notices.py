#!/usr/bin/env python3
"""
Fetch latest Mutual Fund notices from BSE and merge into index.html.
Designed to be resilient to BSE's Akamai protection.
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

IST = timezone(timedelta(hours=5, minutes=30))
INDEX = Path("index.html")

# Multiple candidate endpoints / pages (BSE changes layout often)
SOURCES = [
    # Public beta-style listing that has worked recently
    "https://www.bseindia.com/markets/MarketInfo/NoticesCirculars.aspx?id=5",
    "https://beta.bseindia.com/markets/MarketInfo/NoticesCirculars.aspx?id=5",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.bseindia.com/",
    "Connection": "keep-alive",
}

# Heuristic topic tags (same spirit as the existing board)
TOPIC_RULES = [
    (r"\bNFO\b|New Fund Offer|Launch of .*ETF|Launch of New Fund", "NFO Launch"),
    (r"\bMerger\b|Scheme Merger", "Scheme Merger"),
    (r"\bMaturity\b|\bRedemption\b", "Redemption / Maturity"),
    (r"Availability .* for ongoing|ongoing transactions|Resumption of Subscription", "Scheme Availability"),
    (r"Scheme Modification|Modification of|Change in", "Scheme Modification"),
    (r"Listing of Units|Listing of", "Listing"),
    (r"Enable|Enabling of|Feature|facility|SMART Switch|SIP Facility|Choti SIP", "Feature Enablement"),
]


def classify_topic(subject: str) -> str:
    s = subject or ""
    for pat, topic in TOPIC_RULES:
        if re.search(pat, s, re.I):
            return topic
    return "Other"


def parse_date_from_notice_no(notice_no: str) -> str | None:
    """20260924-19 → 2026-09-24"""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-", notice_no or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def fetch_html(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
        print(f"  {url} → HTTP {r.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  {url} → {e}", file=sys.stderr)
    return None


def extract_notices_from_html(html: str) -> list[dict]:
    """
    Parse notice rows. Works on both classic table markup and
    the denser text dumps BSE sometimes serves.
    """
    soup = BeautifulSoup(html, "lxml")
    notices = []

    # Strategy 1: proper <table> rows
    for tr in soup.select("table tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        # Expect: Notice No | Subject | Segment | Category | Department | PDF
        notice_no = cells[0]
        if not re.match(r"^\d{8}-\d+$", notice_no):
            continue
        subject = cells[1]
        segment = cells[2] if len(cells) > 2 else "Mutual Fund"
        category = cells[3] if len(cells) > 3 else ""
        department = cells[4] if len(cells) > 4 else ""

        # PDF link
        pdf = ""
        a = tr.find("a", href=True)
        if a and (".pdf" in a["href"].lower() or "UploadDocs" in a["href"]):
            pdf = urljoin("https://www.bseindia.com", a["href"])
        if not pdf:
            # conventional path
            pdf = f"https://www.bseindia.com/downloads/UploadDocs/Notices/{notice_no}/{notice_no}.pdf"

        if "Mutual Fund" not in segment and segment.strip():
            continue  # safety filter

        notices.append(
            {
                "noticeNo": notice_no,
                "date": parse_date_from_notice_no(notice_no) or "",
                "subject": subject,
                "segment": "Mutual Fund",
                "category": category or "Trading",
                "department": department or "Trading Operations",
                "topic": classify_topic(subject),
                "pdf": pdf,
                "source": "current",
            }
        )

    # Strategy 2: regex fallback for flattened pages
    if not notices:
        pattern = re.compile(
            r"(20\d{6}-\d+)\s+(.+?)\s+Mutual Fund\s+(\w[\w /]*)\s+([\w \-]+?)(?:\s|$)",
            re.I,
        )
        for m in pattern.finditer(html):
            notice_no, subject, category, department = m.groups()
            notices.append(
                {
                    "noticeNo": notice_no,
                    "date": parse_date_from_notice_no(notice_no) or "",
                    "subject": subject.strip(),
                    "segment": "Mutual Fund",
                    "category": category.strip(),
                    "department": department.strip(),
                    "topic": classify_topic(subject),
                    "pdf": f"https://www.bseindia.com/downloads/UploadDocs/Notices/{notice_no}/{notice_no}.pdf",
                    "source": "current",
                }
            )

    # Deduplicate by noticeNo, keep first
    seen = set()
    unique = []
    for n in notices:
        if n["noticeNo"] not in seen:
            seen.add(n["noticeNo"])
            unique.append(n)
    return unique


def load_existing_notices(html: str) -> list[dict]:
    m = re.search(r"const NOTICES\s*=\s*(\[.*?\]);", html, re.S)
    if not m:
        raise SystemExit("Could not find const NOTICES = [...] in index.html")
    return json.loads(m.group(1))


def merge(existing: list[dict], fresh: list[dict]) -> tuple[list[dict], int]:
    by_no = {n["noticeNo"]: n for n in existing}
    added = 0
    for n in fresh:
        if n["noticeNo"] not in by_no:
            by_no[n["noticeNo"]] = n
            added += 1
        else:
            # refresh PDF / subject if needed, keep original source if archive
            old = by_no[n["noticeNo"]]
            if old.get("source") != "archive":
                by_no[n["noticeNo"]] = {**old, **n, "source": old.get("source", "current")}
    # Sort newest first
    merged = sorted(by_no.values(), key=lambda x: (x.get("date", ""), x["noticeNo"]), reverse=True)
    return merged, added


def update_html(html: str, notices: list[dict], captured_at: str) -> str:
    # Replace NOTICES array
    new_json = json.dumps(notices, ensure_ascii=False, separators=(",", ":"))
    html = re.sub(r"const NOTICES\s*=\s*\[.*?\];", f"const NOTICES = {new_json};", html, count=1, flags=re.S)

    # Update "Last refreshed" timestamp in the source card
    html = re.sub(
        r'(id="capturedAt">)[^<]+',
        rf"\g<1>{captured_at}",
        html,
        count=1,
    )
    return html


def main():
    if not INDEX.exists():
        raise SystemExit("index.html not found — run from repo root")

    html = INDEX.read_text(encoding="utf-8")
    existing = load_existing_notices(html)
    print(f"Existing notices: {len(existing)}")

    fresh = []
    for url in SOURCES:
        print(f"Trying {url} ...")
        page = fetch_html(url)
        if not page:
            continue
        found = extract_notices_from_html(page)
        print(f"  → {len(found)} notices parsed")
        if found:
            fresh = found
            break

    if not fresh:
        print("WARNING: Could not fetch any notices (BSE may be blocking this runner).", file=sys.stderr)
        print("Leaving index.html unchanged.", file=sys.stderr)
        # Still update the timestamp so the UI shows the attempt? Optional — skip for now.
        sys.exit(0)

    merged, added = merge(existing, fresh)
    print(f"Merged total: {len(merged)}  (newly added: {added})")

    now = datetime.now(IST).strftime("%d %b %Y, %H:%M IST")
    new_html = update_html(html, merged, now)

    if new_html == html:
        print("No content change.")
        return

    INDEX.write_text(new_html, encoding="utf-8")
    print(f"Updated index.html  (Last refreshed → {now})")


if __name__ == "__main__":
    main()
