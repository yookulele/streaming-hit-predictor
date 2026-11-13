"""Look up every title on TMDB and build one title table.

Inputs
    data/processed/top10_titles.csv   past titles from the Netflix Top 10 (with the hit label)
    data/raw/titles.csv               upcoming Netflix originals you're tracking
Outputs
    data/processed/tmdb_cache.json    raw TMDB answers, so reruns don't repeat lookups
    data/processed/title_table.csv    one row per title: Netflix data + TMDB details

For each title it searches TMDB (movie or TV), picks the best match, and pulls:
genres, original language, origin country, runtime, release date, number of
seasons, TMDB popularity, and the top 5 cast members.

Setup:
    pip install requests python-dotenv pandas
    .env file in the repo root:  TMDB_TOKEN=your-API-Read-Access-Token
Run (from the repo root):
    python src/ingest/fetch_tmdb.py

About 2 requests per title (~8,000 total). TMDB allows plenty per second, so it
takes roughly 10-15 minutes the first time. It saves progress every 100 titles;
if it stops, just run it again and it picks up where it left off.
"""

import json
import os
import re
import sys
import time
import unicodedata

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TMDB_TOKEN")
if not TOKEN:
    sys.exit("TMDB_TOKEN not found. Add your API Read Access Token to .env.")

API = "https://api.themoviedb.org/3"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "accept": "application/json"}
TOP10_FILE = "data/processed/top10_titles.csv"
UPCOMING_FILE = "data/raw/titles.csv"
CACHE_FILE = "data/processed/tmdb_cache.json"
OUT_FILE = "data/processed/title_table.csv"

session = requests.Session()
TOKEN = TOKEN.strip()
if len(TOKEN) == 32 and "." not in TOKEN:
    # Short v3 "API Key" instead of the long Read Access Token: send it as a parameter
    session.headers.update({"accept": "application/json"})
    session.params = {"api_key": TOKEN}
else:
    session.headers.update(HEADERS)


def get(path, **params):
    for attempt in range(5):
        r = session.get(f"{API}{path}", params=params, timeout=30)
        if r.status_code == 429:  # too many requests: wait and retry
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 401:
            sys.exit("TMDB rejected the token. Check TMDB_TOKEN in .env "
                     "(use the long 'API Read Access Token').")
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"TMDB kept rate-limiting {path}")


def norm(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def search_title(row):
    """Return (tmdb_id, media, match) for the best TMDB result, or (None, media, 'none')."""
    media = row["media"]
    results = get(f"/search/{media}", query=row["search_name"],
                  include_adult="false", language="en-US").get("results", [])
    if not results:
        return None, media, "none"

    date_key = "release_date" if media == "movie" else "first_air_date"
    cutoff = row["cutoff_date"]  # the title must have existed by this date
    want = norm(row["search_name"])

    def ok_date(r):
        d = r.get(date_key) or ""
        return not d or d <= cutoff

    def names(r):
        return {norm(r.get("title") or r.get("name") or ""),
                norm(r.get("original_title") or r.get("original_name") or "")}

    candidates = [r for r in results if ok_date(r)] or results
    for r in candidates:  # 1st choice: exact name match, existed in time
        if want in names(r):
            return r["id"], media, "exact"
    return candidates[0]["id"], media, "fuzzy"  # 2nd choice: TMDB's top result


def details(tmdb_id, media):
    return get(f"/{media}/{tmdb_id}", language="en-US", append_to_response="credits")


def flatten(d, media):
    if not d:
        return {}
    cast = [c["name"] for c in (d.get("credits", {}).get("cast") or [])[:5]]
    if media == "movie":
        runtime = d.get("runtime")
        released = d.get("release_date")
    else:
        rts = d.get("episode_run_time") or []
        runtime = rts[0] if rts else None
        released = d.get("first_air_date")
    return {
        "tmdb_id": d.get("id"),
        "tmdb_title": d.get("title") or d.get("name"),
        "tmdb_media": media,
        "genres": "|".join(g["name"] for g in d.get("genres", [])),
        "original_language": d.get("original_language"),
        "origin_country": "|".join(d.get("origin_country") or
                                   [c["iso_3166_1"] for c in d.get("production_countries", [])]),
        "runtime_min": runtime,
        "tmdb_release_date": released,
        "number_of_seasons": d.get("number_of_seasons"),
        "tmdb_popularity": d.get("popularity"),
        "tmdb_vote_average": d.get("vote_average"),
        "tmdb_vote_count": d.get("vote_count"),
        "top_cast": "|".join(cast),
        "networks": "|".join(n["name"] for n in d.get("networks", [])),
    }


def load_titles():
    top = pd.read_csv(TOP10_FILE)
    top["source"] = "top10_history"
    top["media"] = top["category"].str.startswith("Films").map({True: "movie", False: "tv"})
    top["search_name"] = top["show_title"]
    top["season_number"] = top["title_key"].str.extract(r"Season (\d+)", expand=False)
    top["cutoff_date"] = top["first_week"]

    up = pd.read_csv(UPCOMING_FILE, dtype=str).fillna("")
    up["source"] = "upcoming"
    up["media"] = up["type"].isin(["Film", "Documentary", "Film (animated)"]).map({True: "movie", False: "tv"})
    up["search_name"] = up["title"]
    up["title_key"] = up.apply(lambda r: f"{r['title']}: Season {r['season']}"
                               if r["season"].isdigit() else r["title"], axis=1)
    up["season_number"] = up["season"].where(up["season"].str.isdigit())
    up["cutoff_date"] = "2027-12-31"  # upcoming: any date is fine
    return pd.concat([top, up], ignore_index=True)


def main():
    titles = load_titles()
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)

    # One lookup per show (all seasons of a show share TMDB details)
    lookups = titles.drop_duplicates(["media", "search_name"])
    todo = [r for _, r in lookups.iterrows() if f"{r['media']}|{r['search_name']}" not in cache]
    print(f"{len(lookups)} unique titles, {len(todo)} still to look up.")

    for i, r in enumerate(todo, 1):
        key = f"{r['media']}|{r['search_name']}"
        try:
            tmdb_id, media, match = search_title(r)
            d = details(tmdb_id, media) if tmdb_id else None
            cache[key] = {"match": match, "info": flatten(d, media)}
        except requests.RequestException as e:
            print(f"  skipped {r['search_name']}: {e}")
            continue
        if i % 100 == 0 or i == len(todo):
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            print(f"  {i}/{len(todo)} looked up")
        time.sleep(0.05)

    rows = []
    for _, r in titles.iterrows():
        hit = cache.get(f"{r['media']}|{r['search_name']}", {})
        rows.append({**r.to_dict(), "tmdb_match": hit.get("match", "not_looked_up"),
                     **hit.get("info", {})})
    out = pd.DataFrame(rows).drop(columns=["search_name", "cutoff_date"])
    out.to_csv(OUT_FILE, index=False)

    print(f"\nSaved {len(out)} rows to {OUT_FILE}")
    print(out.groupby("source")["tmdb_match"].value_counts().to_string())
    print("\n'exact' = name matched; 'fuzzy' = TMDB's best guess, worth spot-checking; "
          "'none' = not found on TMDB.")


if __name__ == "__main__":
    main()
