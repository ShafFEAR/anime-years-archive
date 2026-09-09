"""Scrape Wikipedia's per-year anime lists (1960-present) into structured JSON.

Source: https://en.wikipedia.org/wiki/List_of_years_in_anime and each
"<year> in anime" article linked from it. Text extracted here is Wikipedia
prose/table data (CC BY-SA, reuse with attribution) -- no images are touched
in this script.
"""
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "index"
LOG_DIR = ROOT / "logs"

API_URL = "https://en.wikipedia.org/w/api.php"
HEADERS = {
    "User-Agent": "AnimeYearsArchiveBot/1.0 (personal research/archival project)"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

START_YEAR = 1960
END_YEAR = 2027

HEADER_MAP = {
    "title": "title",
    "english title": "title",
    "english name": "title",
    "name": "title",
    "original title": "original_title",
    "japanese name": "original_title",
    "japanese title": "original_title",
    "alternate title": "alt_title",
    "alternate name": "alt_title",
    "studio": "studio",
    "studio(s)": "studio",
    "director(s)": "director",
    "director": "director",
    "episodes": "episodes",
    "eps": "episodes",
    "running time (minutes)": "running_time",
    "running time": "running_time",
    "duration": "running_time",
    "release date": "release_date",
    "release start date": "release_date",
    "released": "release_date",
    "completed": "release_end_date",
    "first run start and end dates": "air_dates",
    "airing dates": "air_dates",
    "genre(s)": "genre",
    "genre": "genre",
    "based on": "based_on",
    "notes": "notes",
}

# Tables like top-grossing-film rankings reuse a "Title" column but just
# re-list films already covered elsewhere -- skip them to avoid duplicates.
EXCLUDE_IF_HEADERS_INCLUDE = {"rank"}


def fetch_year_page(year, retries=3):
    params = {
        "action": "parse",
        "page": f"{year} in anime",
        "prop": "text",
        "format": "json",
        "redirects": 1,
    }
    for attempt in range(retries):
        try:
            resp = SESSION.get(API_URL, params=params, timeout=30)
            data = resp.json()
        except (requests.RequestException, ValueError):
            time.sleep(2)
            continue
        if "error" in data:
            return None
        return data["parse"]["text"]["*"]
    return None


def expand_table(table):
    """Expand a wikitable's <tr> rows into a rectangular grid of cell tags,
    honoring rowspan/colspan so columns stay aligned across rows."""
    rows = table.find_all("tr")
    grid = []
    active_spans = []  # [{col, remaining, cell}]
    for tr in rows:
        cells = tr.find_all(["th", "td"], recursive=False)
        row = {}
        for span in active_spans:
            row[span["col"]] = span["cell"]
        occupied = set(row.keys())
        col = 0
        new_spans = []
        for cell in cells:
            while col in occupied:
                col += 1
            colspan = int(cell.get("colspan", 1) or 1)
            rowspan = int(cell.get("rowspan", 1) or 1)
            for i in range(colspan):
                row[col + i] = cell
                occupied.add(col + i)
                if rowspan > 1:
                    new_spans.append({"col": col + i, "remaining": rowspan - 1, "cell": cell})
            col += colspan
        kept = []
        for span in active_spans:
            span["remaining"] -= 1
            if span["remaining"] > 0:
                kept.append(span)
        active_spans = kept + new_spans
        width = max(row.keys(), default=-1) + 1
        grid.append([row.get(i) for i in range(width)])
    return grid


def cell_text(cell):
    if cell is None:
        return ""
    return cell.get_text(" ", strip=True)


def cell_link(cell):
    if cell is None:
        return None
    a = cell.find("a", href=True)
    if not a:
        return None
    href = a["href"]
    if not href.startswith("/wiki/"):
        return None
    raw_title = href.split("/wiki/", 1)[1]
    if ":" in raw_title:
        return None  # category/file/etc link, not an article
    is_redlink = "new" in (a.get("class") or [])
    return {
        "title": unquote(raw_title).replace("_", " "),
        "url": "https://en.wikipedia.org" + href,
        "redlink": is_redlink,
    }


def normalize_header(h):
    h = re.sub(r"\[.*?\]", "", h).strip().lower()
    return h


def parse_year(year):
    html = fetch_year_page(year)
    if html is None:
        return None
    soup = BeautifulSoup(html, "lxml")
    content = soup.find("div", class_="mw-parser-output") or soup
    elements = content.find_all(["h2", "h3", "h4", "table"])
    entries = []
    current_heading = "Unknown"
    for el in elements:
        if el.name in ("h2", "h3", "h4"):
            headline = el.find(class_="mw-headline")
            current_heading = headline.get_text(strip=True) if headline else el.get_text(strip=True)
            continue
        if "wikitable" not in (el.get("class") or []):
            continue
        grid = expand_table(el)
        if len(grid) < 2:
            continue
        headers = [normalize_header(cell_text(c)) for c in grid[0]]
        mapped = [HEADER_MAP.get(h, h) for h in headers]
        if "title" not in mapped:
            continue
        if EXCLUDE_IF_HEADERS_INCLUDE & set(headers):
            continue
        title_idx = mapped.index("title")
        for row in grid[1:]:
            if title_idx >= len(row):
                continue
            title_cell = row[title_idx]
            title_text = cell_text(title_cell)
            if not title_text:
                continue
            link = cell_link(title_cell)
            entry = {
                "year": year,
                "section": current_heading,
                "title": title_text,
                "wiki_title": link["title"] if link else None,
                "wiki_url": link["url"] if link else None,
                "redlink": link["redlink"] if link else None,
            }
            for idx, field in enumerate(mapped):
                if field == "title" or idx >= len(row) or field in entry:
                    continue
                entry[field] = cell_text(row[idx])
            entries.append(entry)
    return entries


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def main(years=None):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = LOG_DIR / "index_summary.json"
    missing_path = LOG_DIR / "missing_years.json"
    summary = load_json(summary_path, {})
    still_missing = set(load_json(missing_path, []))
    for year in (years if years else range(START_YEAR, END_YEAR + 1)):
        try:
            entries = parse_year(year)
        except Exception as e:
            print(f"{year}: ERROR {e}")
            still_missing.add(year)
            continue
        if entries is None:
            print(f"{year}: no article found, skipping")
            still_missing.add(year)
            continue
        out_path = OUT_DIR / f"{year}.json"
        out_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        summary[str(year)] = len(entries)
        still_missing.discard(year)
        print(f"{year}: {len(entries)} entries")
        time.sleep(0.3)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    missing_path.write_text(json.dumps(sorted(still_missing), indent=2), encoding="utf-8")
    total = sum(summary.values())
    print(f"\nTotal entries across {len(summary)} years: {total}. Still missing: {sorted(still_missing)}")


if __name__ == "__main__":
    years_arg = None
    if len(sys.argv) > 1:
        years_arg = [int(y) for y in sys.argv[1:]]
    main(years_arg)
