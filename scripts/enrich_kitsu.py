"""Enrich the Wikipedia-scraped anime index with Kitsu synopsis + cover art.

Kitsu's API is public and built for third-party apps to display this data
(search, synopsis, posterImage), so like AniList, these images are fine to
cache locally and redistribute with attribution -- unlike Wikipedia's
non-free cover art. Used as a stand-in while AniList's API is down
("temporarily disabled due to severe stability issues" as of 2026-09-09).

Resumable: results are cached in data/kitsu_cache.json keyed by search
title, and images are deduped by Kitsu id, so re-running only fetches
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
CACHE_PATH = ROOT / "data" / "kitsu_cache.json"
LOG_DIR = ROOT / "logs"

KITSU_URL = "https://kitsu.io/api/edge/anime"
HEADERS = {
    "User-Agent": "AnimeYearsArchiveBot/1.0 (personal research/archival project)",
    "Accept": "application/vnd.api+json",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

DELAY = 0.8


def load_cache():
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def clean_synopsis(text):
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\(Source:.*?\)\s*$", "", text, flags=re.IGNORECASE)
    return text.strip() or None


def query_kitsu(title, expected_year=None, retries=3):
    global DELAY
    params = {
        "filter[text]": title,
        "page[limit]": 1,
        "include": "categories",
    }
    for attempt in range(retries):
        try:
            resp = SESSION.get(KITSU_URL, params=params, timeout=20)
        except requests.RequestException:
            time.sleep(3)
            continue
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "30") or "30")
            print(f"  rate limited, sleeping {retry_after}s")
            time.sleep(retry_after + 1)
            DELAY = min(3.0, DELAY + 0.3)
            continue
        if resp.status_code >= 500:
            time.sleep(5)
            continue
        if resp.status_code != 200:
            print(f"  Kitsu returned {resp.status_code}: {resp.text[:200]}")
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        items = data.get("data") or []
        if not items:
            return {"not_found": True}
        item = items[0]
        attrs = item.get("attributes") or {}
        titles = attrs.get("titles") or {}
        start_date = attrs.get("startDate") or ""
        start_year = int(start_date[:4]) if start_date[:4].isdigit() else None
        year_match = None
        if expected_year and start_year:
            year_match = abs(start_year - expected_year) <= 1
        categories = [
            c["attributes"]["title"]
            for c in (data.get("included") or [])
            if c.get("type") == "categories" and c.get("attributes", {}).get("title")
        ]
        poster = attrs.get("posterImage") or {}
        return {
            "source": "kitsu",
            "source_id": item.get("id"),
            "title_romaji": titles.get("en_jp") or attrs.get("canonicalTitle"),
            "title_english": titles.get("en") or titles.get("en_us"),
            "title_native": titles.get("ja_jp"),
            "description": clean_synopsis(attrs.get("synopsis")),
            "cover_small": poster.get("small") or poster.get("medium"),
            "cover_large": poster.get("large") or poster.get("original"),
            "kitsu_year": start_year,
            "format": attrs.get("subtype"),
            "episodes": attrs.get("episodeCount"),
            "genres": categories,
            "site_url": f"https://kitsu.io/anime/{attrs['slug']}" if attrs.get("slug") else None,
            "year_match": year_match,
        }
    return None


def download_image(url, source_id):
    ext = ".png" if url.lower().endswith(".png") else ".jpg"
    dest = IMAGES_DIR / f"kitsu-{source_id}{ext}"
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
        # Start from an already-enriched file if one exists (e.g. a partial
        # AniList pass), so we only fill in gaps rather than redo everything.
        enriched_path = ENRICHED_DIR / f"{year}.json"
        if enriched_path.exists():
            entries = json.loads(enriched_path.read_text(encoding="utf-8"))
        else:
            entries = json.loads(yf.read_text(encoding="utf-8"))
        for entry in entries:
            total_entries += 1
            if entry.get("anilist") or entry.get("enrichment"):
                continue  # already enriched by a previous pass
            raw_title = entry.get("wiki_title") or entry["title"]
            search_title = raw_title.split("#", 1)[0].strip() or entry["title"]
            cache_key = search_title.strip().lower()
            if cache_key in cache:
                result = cache[cache_key]
            else:
                result = query_kitsu(search_title, expected_year=year)
                total_queried += 1
                if result is not None:
                    cache[cache_key] = result
                time.sleep(DELAY)
                if total_queried % 50 == 0:
                    save_cache(cache)
                    print(f"...{total_queried} Kitsu queries so far (year {year}, delay={DELAY:.1f}s)")
            if not result or result.get("not_found"):
                entry["enrichment"] = None
                unmatched.append({"year": year, "title": entry["title"], "reason": "no_match"})
                continue
            if result.get("year_match") is False:
                # Fuzzy text search returned something, but the release year is
                # way off -- almost certainly the wrong anime. Reject rather
                # than attach a plausible-looking but incorrect synopsis/cover.
                entry["enrichment"] = None
                unmatched.append({
                    "year": year, "title": entry["title"], "reason": "year_mismatch",
                    "matched_to": result.get("title_english") or result.get("title_romaji"),
                    "matched_year": result.get("kitsu_year"),
                })
                continue
            entry["enrichment"] = {k: v for k, v in result.items() if k != "cover_small"}
            if result.get("cover_small"):
                local_path = download_image(result["cover_small"], result["source_id"])
                entry["enrichment"]["image_path"] = local_path
        enriched_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        matched = sum(1 for e in entries if e.get("enrichment") or e.get("anilist"))
        print(f"{year}: wrote {len(entries)} enriched entries ({matched} matched)")
        save_cache(cache)

    (LOG_DIR / "unmatched_kitsu.json").write_text(json.dumps(unmatched, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDone. {total_entries} total entries, {total_queried} new Kitsu queries, {len(unmatched)} unmatched.")


if __name__ == "__main__":
    years_arg = None
    if len(sys.argv) > 1:
        years_arg = {int(y) for y in sys.argv[1:]}
    main(years_arg)
