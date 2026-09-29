#!/usr/bin/env python3
"""
Fetch BSE StAR MF notices + NSE NMF circulars and merge into index.html.
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

BSE_SOURCES = [
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
    "Connection": "keep-alive",
}

TOPIC_RULES = [
    (r"\bNFO\b|New Fund Offer|Launch of .*ETF|Launch of New Fund", "NFO Launch"),
    (r"\bMerger\b|Scheme Merger", "Scheme Merger"),
    (r"\bMaturity\b|\bRedemption\b", "Redemption / Maturity"),
    (r"Availability .* for ongoing|ongoing transactions|Resumption of Subscription|Revoke of Temporary Suspension", "Scheme Availability"),
    (r"Temporary Suspension|Suspension of", "Scheme Availability"),
    (r"Scheme Modification|Modification of|Change in", "Scheme Modification"),
    (r"Listing of Units|Listing of", "Listing"),
    (r"Enable|Enabling of|Feature|facility|SMART Switch|SIP Facility|Choti SIP|Weekly frequency", "Feature Enablement"),
    (r"Downtime|maintenance|Password", "Feature Enablement"),
]


def classify_topic(subject: str) -> str:
    s = subject or ""
    for pat, topic in TOPIC_RULES:
        if re.search(pat, s, re.I):
            return topic
    return "Other"


def parse_date_from_notice_no(notice_no: str):
    m = re.match(r"^(\d{4})(\d{2})(\d{2})-", notice_no or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def parse_nse_date(cir_date: str) -> str:
    """20260928 or 2026-09-28 → 2026-09-28"""
    if not cir_date:
        return ""
    s = str(cir_date).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return s


# ---------- BSE ----------

def fetch_html(url: str):
    try:
        h = dict(HEADERS)
        h["Referer"] = "https://www.bseindia.com/"
        r = requests.get(url, headers=h, timeout=30)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
        print(f"  {url} → HTTP {r.status_code}", file=sys.stderr)
    except Exception as e:
        print(f"  {url} → {e}", file=sys.stderr)
    return None


def extract_bse_notices(html: str):
    soup = BeautifulSoup(html, "lxml")
    notices = []
    for tr in soup.select("table tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        notice_no = cells[0]
        if not re.match(r"^\d{8}-\d+$", notice_no):
            continue
        subject = cells[1]
        segment = cells[2] if len(cells) > 2 else "Mutual Fund"
        category = cells[3] if len(cells) > 3 else ""
        department = cells[4] if len(cells) > 4 else ""
        pdf = ""
        a = tr.find("a", href=True)
        if a and (".pdf" in a["href"].lower() or "UploadDocs" in a["href"]):
            pdf = urljoin("https://www.bseindia.com", a["href"])
        if not pdf:
            pdf = f"https://www.bseindia.com/downloads/UploadDocs/Notices/{notice_no}/{notice_no}.pdf"
        if "Mutual Fund" not in segment and segment.strip():
            continue
        notices.append({
            "noticeNo": notice_no,
            "date": parse_date_from_notice_no(notice_no) or "",
            "subject": subject,
            "segment": "Mutual Fund",
            "category": category or "Trading",
            "department": department or "Trading Operations",
            "topic": classify_topic(subject),
            "pdf": pdf,
            "source": "current",
            "exchange": "BSE",
        })
    seen = set()
    unique = []
    for n in notices:
        if n["noticeNo"] not in seen:
            seen.add(n["noticeNo"])
            unique.append(n)
    return unique


# ---------- NSE NMF ----------

def fetch_nse_nmf_notices():
    """
    NSE circulars API — Mutual Fund dept (fileDept NMF).
    Needs a homepage hit first for cookies.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.nseindia.com/resources/exchange-communication-circulars",
    })
    try:
        session.get("https://www.nseindia.com/", timeout=30)
        # Broad circulars feed; filter NMF client-side
        r = session.get("https://www.nseindia.com/api/circulars", timeout=30)
        if r.status_code != 200:
            print(f"  NSE API → HTTP {r.status_code}", file=sys.stderr)
            return []
        data = r.json()
    except Exception as e:
        print(f"  NSE API → {e}", file=sys.stderr)
        return []

    rows = data if isinstance(data, list) else data.get("data") or data.get("circulars") or []
    notices = []
    for row in rows:
        dept = (row.get("fileDept") or row.get("circDepartment") or "").upper()
        dept_name = (row.get("circDepartment") or "").lower()
        if dept not in ("NMF", "NMFTM", "MFSS") and "mutual fund" not in dept_name:
            continue
        notice_no = row.get("circDisplayNo") or row.get("circNumber") or ""
        if not notice_no and row.get("circNumber"):
            notice_no = f"NSE/NMF/{row.get('circNumber')}"
        subject = row.get("sub") or row.get("subject") or ""
        pdf = row.get("circFilelink") or ""
        if not pdf and row.get("circFilename"):
            pdf = f"https://nsearchives.nseindia.com/content/circulars/{row['circFilename']}"
        date = parse_nse_date(row.get("cirDate") or "")
        notices.append({
            "noticeNo": str(notice_no),
            "date": date,
            "subject": subject,
            "segment": "Mutual Fund",
            "category": row.get("circCategory") or "Trading",
            "department": row.get("circDepartment") or "Mutual Fund",
            "topic": classify_topic(subject),
            "pdf": pdf,
            "source": "current",
            "exchange": "NSE",
        })
    seen = set()
    unique = []
    for n in notices:
        if n["noticeNo"] not in seen:
            seen.add(n["noticeNo"])
            unique.append(n)
    unique.sort(key=lambda x: (x.get("date", ""), x["noticeNo"]), reverse=True)
    return unique


# ---------- Merge / HTML ----------

def load_array(html: str, name: str):
    m = re.search(rf"const {name}\s*=\s*(\[.*?\]);", html, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(1))
    except Exception:
        return []


def merge(existing, fresh):
    by_no = {n["noticeNo"]: n for n in existing}
    added = 0
    for n in fresh:
        if n["noticeNo"] not in by_no:
            by_no[n["noticeNo"]] = n
            added += 1
        else:
            old = by_no[n["noticeNo"]]
            if old.get("source") != "archive":
                by_no[n["noticeNo"]] = {**old, **n, "source": old.get("source", "current")}
    merged = sorted(
        by_no.values(),
        key=lambda x: (x.get("date", ""), x["noticeNo"]),
        reverse=True,
    )
    return merged, added


def replace_or_insert_array(html: str, name: str, data: list) -> str:
    new_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    pat = rf"const {name}\s*=\s*\[.*?\];"
    if re.search(pat, html, re.S):
        return re.sub(pat, f"const {name} = {new_json};", html, count=1, flags=re.S)
    # Insert after NOTICES if present
    m = re.search(r"(const NOTICES\s*=\s*\[.*?\];)", html, re.S)
    if m and name != "NOTICES":
        return html[: m.end()] + f"\nconst {name} = {new_json};" + html[m.end() :]
    return html


def update_html(html: str, bse, nse, captured_at: str) -> str:
    html = replace_or_insert_array(html, "NOTICES", bse)
    html = replace_or_insert_array(html, "NSE_NOTICES", nse)
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
    existing_bse = load_array(html, "NOTICES")
    existing_nse = load_array(html, "NSE_NOTICES")
    print(f"Existing BSE: {len(existing_bse)}  NSE: {len(existing_nse)}")

    # BSE
    fresh_bse = []
    for url in BSE_SOURCES:
        print(f"Trying BSE {url} ...")
        page = fetch_html(url)
        if not page:
            continue
        found = extract_bse_notices(page)
        print(f"  → {len(found)} BSE notices")
        if found:
            fresh_bse = found
            break

    # NSE
    print("Trying NSE NMF circulars ...")
    fresh_nse = fetch_nse_nmf_notices()
    print(f"  → {len(fresh_nse)} NSE NMF notices")

    if not fresh_bse and not fresh_nse:
        print("WARNING: Could not fetch BSE or NSE notices.", file=sys.stderr)
        sys.exit(0)

    merged_bse, add_bse = merge(existing_bse, fresh_bse) if fresh_bse else (existing_bse, 0)
    merged_nse, add_nse = merge(existing_nse, fresh_nse) if fresh_nse else (existing_nse, 0)
    print(f"BSE total {len(merged_bse)} (+{add_bse})  NSE total {len(merged_nse)} (+{add_nse})")

    now = datetime.now(IST).strftime("%d %b %Y, %H:%M IST")
    new_html = update_html(html, merged_bse, merged_nse, now)

    if new_html == html:
        print("No content change.")
        return

    INDEX.write_text(new_html, encoding="utf-8")
    print(f"Updated index.html  (Last refreshed → {now})")


if __name__ == "__main__":
    main()
