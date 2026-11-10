"""Find each title's official trailer on YouTube.

Reads data/raw/titles.csv, searches YouTube for "<title> official trailer", keeps only
videos posted by a channel whose name contains "Netflix", and writes data/raw/trailers.csv.

Quota: each search costs 100 of your 10,000 daily units, so about 95 searches a
day. Rows in trailers.csv that already have a video_url are kept as they are
(so your hand fixes are safe); only rows with an empty video_url are searched
again. To make a wrong match retry, clear its video_url.

Setup:
    pip install requests python-dotenv
    .env file in the repo root:  YOUTUBE_API_KEY=your-key-here
Run (from the repo root):
    python src/ingest/find_trailers.py
"""

import csv
import os
import re
import sys
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    sys.exit("YOUTUBE_API_KEY not found. Add it to your .env file.")

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
TITLES_FILE = "data/raw/titles.csv"
OUT_FILE = "data/raw/trailers.csv"
MAX_SEARCHES = 95  # stay under the 100-searches/day quota
OUT_FIELDS = ["release_date", "title", "season", "type", "status",
              "video_url", "video_id", "youtube_title", "channel", "published_at"]


def norm(text):
    """Lowercase, drop accents-insensitive punctuation, for loose matching."""
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def search(query, release_date):
    # Only accept videos posted in the 6 months before release, so an older
    # season's trailer (or an unrelated older title) can't match.
    after = (date.fromisoformat(release_date) - timedelta(days=183)).isoformat()
    params = {"part": "snippet", "q": query, "type": "video",
              "maxResults": 10, "publishedAfter": f"{after}T00:00:00Z",
              "key": API_KEY}
    r = requests.get(SEARCH_URL, params=params, timeout=30)
    if r.status_code == 403:
        sys.exit("YouTube says quota used up (or key restricted). Try again tomorrow.")
    r.raise_for_status()
    return r.json().get("items", [])


def pick(items, title):
    """First result from a Netflix channel whose video title contains the show title."""
    want = norm(title.split(":")[0])  # "Love Is Blind: Habibi" -> "love is blind"
    netflix = [i for i in items if "netflix" in i["snippet"]["channelTitle"].lower()]
    for i in netflix:
        if want and want in norm(i["snippet"]["title"]):
            return i, "found"
    if netflix:
        return netflix[0], "check"  # Netflix video, but title doesn't clearly match
    return None, "not_found"


def main():
    # Rows you already have (including ones you fixed by hand) are kept as-is.
    # Only titles with an empty video_url get searched again.
    existing, fields = {}, list(OUT_FIELDS)
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fields += [c for c in (reader.fieldnames or []) if c not in fields]
            existing = {(r["title"], r["season"]): r for r in reader}

    with open(TITLES_FILE, encoding="utf-8-sig") as f:
        titles = list(csv.DictReader(f))

    rows, searches = [], 0
    for row in titles:
        key = (row["title"], row["season"])
        old = existing.get(key)
        if old and old.get("video_url", "").strip():
            rows.append(old)  # keep: has a link (found, checked, or fixed by hand)
            continue
        if searches >= MAX_SEARCHES:
            if searches == MAX_SEARCHES:
                print(f"Stopped at {MAX_SEARCHES} searches to save quota. Run again tomorrow.")
                searches += 1  # print the message only once
            rows.append(old or dict(row, status="not_found"))
            continue
        season = row["season"]
        extra = f" season {season}" if season.isdigit() and season != "1" else ""
        query = f"{row['title']}{extra} official trailer Netflix"
        item, status = pick(search(query, row["release_date"]), row["title"])
        searches += 1
        out = dict(old or row, **row, status=status)
        if item:
            vid = item["id"]["videoId"]
            s = item["snippet"]
            out.update(video_id=vid, video_url=f"https://www.youtube.com/watch?v={vid}",
                       youtube_title=s["title"], channel=s["channelTitle"],
                       published_at=s["publishedAt"])
        rows.append(out)
        print(f"{status:9} {row['title']}")

    with open(OUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print(f"Done: {min(searches, MAX_SEARCHES)} searches used. Review rows marked 'check' or 'not_found'.")


if __name__ == "__main__":
    main()
