"""Enrich the Wikipedia-scraped anime index with AniList synopsis + cover art.

AniList's API is built for third-party apps to display this data (search,
description, coverImage), so unlike Wikipedia's non-free cover art, these
images are fine to cache locally and redistribute with attribution.

Resumable: results are cached in data/anilist_cache.json keyed by search
title, and images are deduped by AniList id, so re-running only fetches
what's missing.
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = ROOT / "data" / "index"
ENRICHED_DIR = ROOT / "data" / "enriched"
IMAGES_DIR = ROOT / "images"
CACHE_PATH = ROOT / "data" / "anilist_cache.json"
LOG_DIR = ROOT / "logs"

ANILIST_URL = "https://graphql.anilist.co"
HEADERS = {
    "User-Agent": "AnimeYearsArchiveBot/1.0 (personal research/archival project)",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    title { romaji english native }
    description(asHtml: false)
    coverImage { medium large color }
    startDate { year }
    format
    episodes
    genres
    siteUrl
  }
}
"""

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

DELAY = 2.2


def load_cache():
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_description(desc):
    if not desc:
        return None
    desc = re.sub(r"<br\s*/?>", " ", desc)
    desc = re.sub(r"<[^>]+>", "", desc)
    desc = re.sub(r"\(Source:.*?\)", "", desc, flags=re.IGNORECASE | re.DOTALL)
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc


def query_anilist(title, expected_year=None, retries=3):
    global DELAY
    payload = {"query": QUERY, "variables": {"search": title}}
    for attempt in range(retries):
        try:
            resp = SESSION.post(ANILIST_URL, json=payload, timeout=20)
        except requests.RequestException:
            time.sleep(3)
            continue
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60") or "60")
            print(f"  rate limited, sleeping {retry_after}s")
            time.sleep(retry_after + 1)
            DELAY = min(5.0, DELAY + 0.3)
            continue
        if resp.status_code >= 500:
            time.sleep(5)
            continue
        if resp.status_code != 200:
            return {"not_found": True}
        try:
            data = resp.json()
        except ValueError:
            return None
        if "errors" in data and not data.get("data"):
            return None
        media = (data.get("data") or {}).get("Media")
        if media is None:
            return {"not_found": True}
        year_match = None
        start_year = (media.get("startDate") or {}).get("year")
        if expected_year and start_year:
            year_match = abs(start_year - expected_year) <= 1
        title_obj = media.get("title") or {}
        cover = media.get("coverImage") or {}
        return {
            "anilist_id": media["id"],
            "title_romaji": title_obj.get("romaji"),
            "title_english": title_obj.get("english"),
            "title_native": title_obj.get("native"),
            "description": clean_description(media.get("description")),
            "cover_medium": cover.get("medium"),
            "cover_large": cover.get("large"),
            "color": cover.get("color"),
            "anilist_year": start_year,
            "format": media.get("format"),
            "episodes": media.get("episodes"),
            "genres": media.get("genres"),
            "site_url": media.get("siteUrl"),
            "year_match": year_match,
        }
    return None


def download_image(url, anilist_id):
    ext = ".png" if url.lower().endswith(".png") else ".jpg"
    dest = IMAGES_DIR / f"{anilist_id}{ext}"
    if dest.exists():
        return str(dest.relative_to(ROOT)).replace("\\", "/")
    try:
        resp = SESSION.get(url, timeout=30)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return str(dest.relative_to(ROOT)).replace("\\", "/")
    except requests.RequestException:
        return None


def main(limit_years=None):
    ENRICHED_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cache = load_cache()
    unmatched = []

    year_files = sorted(INDEX_DIR.glob("*.json"), key=lambda p: int(p.stem))
    if limit_years:
        year_files = [p for p in year_files if int(p.stem) in limit_years]

    total_entries = 0
    total_queried = 0

    for yf in year_files:
        year = int(yf.stem)
        entries = json.loads(yf.read_text(encoding="utf-8"))
        for entry in entries:
            total_entries += 1
            search_title = entry.get("wiki_title") or entry["title"]
            cache_key = search_title.strip().lower()
            if cache_key in cache:
                result = cache[cache_key]
            else:
                result = query_anilist(search_title, expected_year=year)
                total_queried += 1
                if result is not None:
                    cache[cache_key] = result
                time.sleep(DELAY)
                if total_queried % 25 == 0:
                    save_cache(cache)
                    print(f"...{total_queried} AniList queries so far (year {year}, delay={DELAY:.1f}s)")
            if not result or result.get("not_found"):
                entry["anilist"] = None
                unmatched.append({"year": year, "title": entry["title"]})
                continue
            entry["anilist"] = {k: v for k, v in result.items() if k != "cover_medium"}
            if result.get("cover_medium"):
                local_path = download_image(result["cover_medium"], result["anilist_id"])
                entry["anilist"]["image_path"] = local_path
        out_path = ENRICHED_DIR / f"{year}.json"
        out_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        matched = sum(1 for e in entries if e.get("anilist"))
        print(f"{year}: wrote {len(entries)} enriched entries ({matched} matched)")
        save_cache(cache)

    (LOG_DIR / "unmatched.json").write_text(json.dumps(unmatched, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDone. {total_entries} total entries, {total_queried} new AniList queries, {len(unmatched)} unmatched.")


if __name__ == "__main__":
    years_arg = None
    if len(sys.argv) > 1:
        years_arg = {int(y) for y in sys.argv[1:]}
    main(years_arg)
