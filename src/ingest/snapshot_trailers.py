"""Take today's snapshot of every trailer's views, likes and comments.

Reads data/raw/trailers.csv (every row with a video_id) and appends one row per
trailer to data/snapshots/trailer_snapshots.csv. Running it twice on the same
day skips trailers already recorded for that date, so it is safe to rerun.

Quota: 1 unit per 50 trailers, so about 2 of your 10,000 daily units.

Setup:
    pip install requests python-dotenv
    .env file in the repo root:  YOUTUBE_API_KEY=your-key-here
    (in GitHub Actions the key comes from the YOUTUBE_API_KEY secret instead)
Run (from the repo root):
    python src/ingest/snapshot_trailers.py
"""

import csv
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("YOUTUBE_API_KEY")
if not API_KEY:
    sys.exit("YOUTUBE_API_KEY not found. Add it to your .env file.")

VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
TRAILERS_FILE = "data/raw/trailers.csv"
OUT_FILE = "data/snapshots/trailer_snapshots.csv"
FIELDS = ["snapshot_date", "snapshot_time_utc", "title", "season", "release_date",
          "video_id", "channel", "views", "likes", "comments"]


def fetch_stats(video_ids):
    """Return {video_id: statistics} for up to 50 ids per request."""
    stats = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        params = {"part": "statistics", "id": ",".join(batch), "key": API_KEY}
        r = requests.get(VIDEOS_URL, params=params, timeout=30)
        if r.status_code == 403:
            sys.exit("YouTube refused the request (quota used up or key restricted).")
        r.raise_for_status()
        for item in r.json().get("items", []):
            stats[item["id"]] = item.get("statistics", {})
    return stats


def main():
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    with open(TRAILERS_FILE, encoding="utf-8-sig") as f:
        trailers = [r for r in csv.DictReader(f) if r.get("video_id", "").strip()]

    already = set()
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding="utf-8") as f:
            already = {r["video_id"] for r in csv.DictReader(f) if r["snapshot_date"] == today}
    todo = [t for t in trailers if t["video_id"].strip() not in already]
    if not todo:
        print(f"Already snapshotted all {len(trailers)} trailers for {today}.")
        return

    stats = fetch_stats(sorted({t["video_id"].strip() for t in todo}))

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    new_file = not os.path.exists(OUT_FILE)
    written, missing = 0, []
    with open(OUT_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for t in todo:
            vid = t["video_id"].strip()
            s = stats.get(vid)
            if s is None:
                missing.append(t["title"])  # video deleted or made private
                continue
            w.writerow({
                "snapshot_date": today,
                "snapshot_time_utc": now.strftime("%H:%M"),
                "title": t["title"], "season": t["season"],
                "release_date": t["release_date"], "video_id": vid,
                "channel": t.get("channel", ""),
                "views": s.get("viewCount", ""),
                "likes": s.get("likeCount", ""),        # blank if hidden by the channel
                "comments": s.get("commentCount", ""),  # blank if comments are off
            })
            written += 1

    print(f"{today}: saved {written} trailer snapshots to {OUT_FILE}.")
    if missing:
        print(f"No longer available on YouTube ({len(missing)}): {', '.join(missing)}")


if __name__ == "__main__":
    main()
