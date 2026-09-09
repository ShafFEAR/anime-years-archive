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


def query_kitsu(title, retries=3):
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
        }
    return None


def search_candidates(title, limit=10, retries=3):
    """Like query_kitsu, but returns up to `limit` ranked candidates instead
    of assuming the top hit is correct. Used by retry_unmatched() to find a
    result whose year actually fits when the top hit didn't."""
    global DELAY
    params = {"filter[text]": title, "page[limit]": limit}
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
        candidates = []
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            titles = attrs.get("titles") or {}
            start_date = attrs.get("startDate") or ""
            start_year = int(start_date[:4]) if start_date[:4].isdigit() else None
            poster = attrs.get("posterImage") or {}
            candidates.append({
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
                "site_url": f"https://kitsu.io/anime/{attrs['slug']}" if attrs.get("slug") else None,
            })
        return candidates
    return None


def fetch_categories(source_id, retries=2):
    for attempt in range(retries):
        try:
            resp = SESSION.get(f"{KITSU_URL}/{source_id}/categories", timeout=15)
        except requests.RequestException:
            time.sleep(2)
            continue
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return [c["attributes"]["title"] for c in data.get("data", []) if c.get("attributes", {}).get("title")]
    return []


STOPWORDS = {
    "the", "a", "an", "of", "and", "in", "to", "movie", "tv", "series",
    "special", "ova", "ona", "film", "part", "season", "vs", "no", "de",
    # Common romanized-Japanese title filler words -- generic enough (roughly
    # "star"/"sky"/"world"/"dream"/"school" etc.) that one matching alone,
    # the way "the"/"of" can, isn't good evidence two titles are the same
    # show (e.g. "Hoshi no Orufeusu" vs "Hoshi no Oujisama" both mean
    # "[something] of the star" but are unrelated productions).
    "hoshi", "sora", "tsuki", "yume", "sekai", "gakuen", "gakkou", "kishi",
    "senshi", "monogatari", "gekijouban", "gekijoban", "shin", "zoku",
    "shinsaku", "kanzenban", "recap", "digest",
    # Common generic English title descriptors -- words that recur across
    # many unrelated shows ("A Little Princess Sara" vs "Little Memole")
    # and so, like "little" here, are weak evidence of a real match on
    # their own.
    "little", "great", "super", "new", "young", "magical", "magic",
    "princess", "prince", "king", "queen", "world", "story", "stories",
    "legend", "legends", "tale", "tales", "adventure", "adventures",
    "hero", "heroes", "wonder", "wonderful", "happy", "lucky", "dream",
    "dreams", "love", "secret", "mystery", "fantasy", "knight", "knights",
    "warrior", "warriors", "battle", "fighter", "fighters", "robot",
    "robots", "original", "complete", "collection", "final", "first",
    "second", "third",
}


def _normalize_for_similarity(s):
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def title_similarity(query, *candidates):
    """How plausible it is that `query` (a Wikipedia title/search string)
    and one of `candidates` (a Kitsu entry's title fields) refer to the same
    production.

    Trusts distinctive-word overlap over raw character similarity whenever
    both titles actually have distinctive words: two unrelated titles that
    happen to share a common shape ("The Mystery of Mamo" vs "The Castle of
    Cagliostro") can score deceptively high on character ratio alone, since
    "the"/"of" and similar length inflate it even with zero real overlap.
    Falls back to character ratio only when a title is too short/generic to
    have any 4+ letter non-stopword ("Mado", "V") to compare."""
    import difflib
    q = _normalize_for_similarity(query)
    q_words = {w for w in q.split() if len(w) >= 4 and w not in STOPWORDS}
    best = 0.0
    for cand in candidates:
        if not cand:
            continue
        c = _normalize_for_similarity(cand)
        c_words = {w for w in c.split() if len(w) >= 4 and w not in STOPWORDS}
        if q_words and c_words:
            shared = q_words & c_words
            score = len(shared) / max(1, min(len(q_words), len(c_words)))
        else:
            score = difflib.SequenceMatcher(None, q, c).ratio()
        best = max(best, score)
    return best


def pick_best_match(candidates, expected_year, query_title=None, threshold=0.45):
    """First candidate (Kitsu's own relevance order) whose release year
    fits AND whose title is plausibly the same production -- year alone
    isn't enough, since a same-named remake/reboot or an unrelated show
    from the same era can coincidentally fit the year window too.

    The returned dict carries a "_match_similarity" field recording the
    score that actually got it accepted -- callers must store/log that value
    rather than recomputing similarity against a *different* query string
    (e.g. the primary title when a romaji fallback is what actually
    matched), or the record ends up self-contradictory: "kept, but its own
    stored similarity says 0.0"."""
    for c in candidates or []:
        if not (c.get("kitsu_year") and abs(c["kitsu_year"] - expected_year) <= 1):
            continue
        sim = 1.0
        if query_title is not None:
            sim = title_similarity(query_title, c.get("title_english"), c.get("title_romaji"))
            if sim < threshold:
                continue
        result = dict(c)
        result["_match_similarity"] = round(sim, 2)
        return result
    return None


def extract_romaji(original_title):
    """Pull the romanized title out of Wikipedia's "Kanji ( Romaji )" style
    original_title field, for use as a fallback search when the English
    title alone doesn't turn up a year-appropriate match."""
    if not original_title:
        return None
    m = re.search(r"\(([^()]+)\)\s*$", original_title)
    if not m:
        return None
    return m.group(1).strip() or None


def pick_search_title(entry):
    """The best string to search Kitsu with. When the Wikipedia link only
    points to an anchor on someone else's page (a filmography mention with
    no dedicated article, marked by "#" in the link target) that target is
    a person/page name, not this anime's name -- search with the entry's
    own display title instead."""
    wiki_title = entry.get("wiki_title")
    if wiki_title and "#" not in wiki_title:
        return wiki_title
    return entry["title"]


def retry_unmatched():
    """Re-attempt entries logged as unmatched, this time scanning several
    search results per title (instead of trusting only the top hit) and
    falling back to the romanized Japanese title when the English title
    search doesn't yield a candidate for the right year."""
    unmatched_path = LOG_DIR / "unmatched_kitsu.json"
    if not unmatched_path.exists():
        print("No unmatched_kitsu.json found -- run the main pass first.")
        return
    unmatched = json.loads(unmatched_path.read_text(encoding="utf-8"))
    by_year = {}
    for u in unmatched:
        by_year.setdefault(u["year"], set()).add(u["title"])

    search_cache_path = ROOT / "data" / "kitsu_search_cache.json"
    search_cache = json.loads(search_cache_path.read_text(encoding="utf-8")) if search_cache_path.exists() else {}

    still_unmatched = []
    newly_matched = 0
    checked = 0

    for year in sorted(by_year):
        enriched_path = ENRICHED_DIR / f"{year}.json"
        entries = json.loads(enriched_path.read_text(encoding="utf-8"))
        title_set = by_year[year]
        changed = False
        for entry in entries:
            if entry["title"] not in title_set or entry.get("enrichment"):
                continue
            checked += 1
            search_title = pick_search_title(entry)
            queries = [search_title]
            romaji = extract_romaji(entry.get("original_title"))
            if romaji and romaji.lower() != search_title.lower():
                queries.append(romaji)

            winner = None
            for q in queries:
                key = q.strip().lower()
                if key in search_cache:
                    candidates = search_cache[key]
                else:
                    candidates = search_candidates(q)
                    if candidates is not None:
                        search_cache[key] = candidates
                    time.sleep(DELAY)
                if candidates:
                    winner = pick_best_match(candidates, year, query_title=q)
                    if winner:
                        break

            if winner:
                genres = fetch_categories(winner["source_id"])
                time.sleep(DELAY)
                enrichment = {k: v for k, v in winner.items() if k not in ("cover_small", "_match_similarity")}
                enrichment["genres"] = genres
                enrichment["year_match"] = True
                enrichment["similarity"] = winner["_match_similarity"]
                if winner.get("cover_small"):
                    enrichment["image_path"] = download_image(winner["cover_small"], winner["source_id"])
                entry["enrichment"] = enrichment
                newly_matched += 1
                changed = True
            else:
                still_unmatched.append({"year": year, "title": entry["title"], "reason": "no_year_fit_in_candidates"})

            if checked % 50 == 0:
                search_cache_path.write_text(json.dumps(search_cache, ensure_ascii=False), encoding="utf-8")
                print(f"...checked {checked}, newly matched {newly_matched}")

        if changed:
            enriched_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"{year}: updated ({sum(1 for e in entries if e['title'] in title_set and e.get('enrichment'))} newly matched)")

    search_cache_path.write_text(json.dumps(search_cache, ensure_ascii=False), encoding="utf-8")
    unmatched_path.write_text(json.dumps(still_unmatched, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nRetry done. Checked {checked}, newly matched {newly_matched}, still unmatched {len(still_unmatched)}.")


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


def revalidate():
    """Re-check every currently-matched entry against the title-similarity
    gate (added after the first two passes ran), since a match accepted on
    year alone can be a same-named remake, an unrelated show from the same
    era, or -- for entries whose wiki_title is just a "#anchor" on someone
    else's page -- was searched with the wrong string entirely."""
    search_cache_path = ROOT / "data" / "kitsu_search_cache.json"
    search_cache = json.loads(search_cache_path.read_text(encoding="utf-8")) if search_cache_path.exists() else {}

    reverted = 0
    fixed_to_different = 0
    kept = 0
    checked = 0
    still_bad = []

    year_files = sorted(ENRICHED_DIR.glob("*.json"), key=lambda p: int(p.stem))
    for yf in year_files:
        year = int(yf.stem)
        entries = json.loads(yf.read_text(encoding="utf-8"))
        changed = False
        for entry in entries:
            enr = entry.get("enrichment")
            if not enr:
                continue
            checked += 1
            correct_search_title = pick_search_title(entry)
            needs_full_research = "#" in (entry.get("wiki_title") or "")

            if not needs_full_research:
                sim = title_similarity(correct_search_title, enr.get("title_english"), enr.get("title_romaji"))
                if sim >= 0.45:
                    if enr.get("similarity") != round(sim, 2):
                        enr["similarity"] = round(sim, 2)
                        changed = True
                    kept += 1
                    continue

            # Either the search string was wrong all along, or the existing
            # match fails the similarity bar -- look for a better fit.
            queries = [correct_search_title]
            romaji = extract_romaji(entry.get("original_title"))
            if romaji and romaji.lower() != correct_search_title.lower():
                queries.append(romaji)

            winner = None
            for q in queries:
                key = q.strip().lower()
                if key in search_cache:
                    candidates = search_cache[key]
                else:
                    candidates = search_candidates(q)
                    if candidates is not None:
                        search_cache[key] = candidates
                    time.sleep(DELAY)
                if candidates:
                    winner = pick_best_match(candidates, year, query_title=q)
                    if winner:
                        break

            if winner and winner["source_id"] != enr.get("source_id"):
                genres = fetch_categories(winner["source_id"])
                time.sleep(DELAY)
                new_enr = {k: v for k, v in winner.items() if k not in ("cover_small", "_match_similarity")}
                new_enr["genres"] = genres
                new_enr["year_match"] = True
                new_enr["similarity"] = winner["_match_similarity"]
                if winner.get("cover_small"):
                    new_enr["image_path"] = download_image(winner["cover_small"], winner["source_id"])
                entry["enrichment"] = new_enr
                fixed_to_different += 1
                changed = True
            elif winner:
                enr["similarity"] = winner["_match_similarity"]
                kept += 1
            else:
                entry["enrichment"] = None
                still_bad.append({"year": year, "title": entry["title"], "reason": "failed_revalidation"})
                reverted += 1
                changed = True

            if checked % 100 == 0:
                search_cache_path.write_text(json.dumps(search_cache, ensure_ascii=False), encoding="utf-8")
                print(f"...checked {checked}: kept {kept}, fixed {fixed_to_different}, reverted {reverted}")

        if changed:
            yf.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")

    search_cache_path.write_text(json.dumps(search_cache, ensure_ascii=False), encoding="utf-8")
    unmatched_path = LOG_DIR / "unmatched_kitsu.json"
    existing_unmatched = json.loads(unmatched_path.read_text(encoding="utf-8")) if unmatched_path.exists() else []
    existing_unmatched.extend(still_bad)
    unmatched_path.write_text(json.dumps(existing_unmatched, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nRevalidation done. Checked {checked}: kept {kept}, fixed-to-different-entry {fixed_to_different}, reverted-to-unmatched {reverted}.")


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
            search_title = pick_search_title(entry)
            cache_key = search_title.strip().lower()
            if cache_key in cache:
                result = cache[cache_key]
            else:
                result = query_kitsu(search_title)
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
            # A cached result is keyed by search text only, and the same text
            # can legitimately refer to a different production in a different
            # year (a remake/reboot reusing the original's title). So the
            # year check must be recomputed against *this* entry's year every
            # time, never trusted from when the cache entry was first written.
            kitsu_year = result.get("kitsu_year")
            year_match = abs(kitsu_year - year) <= 1 if kitsu_year else None
            sim = title_similarity(search_title, result.get("title_english"), result.get("title_romaji"))
            if year_match is False or sim < 0.45:
                # Fuzzy text search returned something, but either the release
                # year is way off, or the title doesn't actually resemble the
                # query -- almost certainly the wrong anime (or the right
                # franchise but a different production). Reject rather than
                # attach a plausible-looking but incorrect synopsis/cover.
                entry["enrichment"] = None
                unmatched.append({
                    "year": year, "title": entry["title"], "reason": "year_mismatch" if year_match is False else "low_similarity",
                    "matched_to": result.get("title_english") or result.get("title_romaji"),
                    "matched_year": kitsu_year,
                })
                continue
            entry["enrichment"] = {k: v for k, v in result.items() if k != "cover_small"}
            entry["enrichment"]["year_match"] = year_match
            entry["enrichment"]["similarity"] = round(sim, 2)
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
    if len(sys.argv) > 1 and sys.argv[1] == "retry":
        retry_unmatched()
    elif len(sys.argv) > 1 and sys.argv[1] == "revalidate":
        revalidate()
    else:
        years_arg = None
        if len(sys.argv) > 1:
            years_arg = {int(y) for y in sys.argv[1:]}
        main(years_arg)
