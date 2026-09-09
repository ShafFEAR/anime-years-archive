"""Generate the browsable Markdown site (README index + one page per year)
from the scraped/enriched JSON data. Safe to re-run any time -- it only
reads data/ and images/, and rewrites README.md and years/*.md.
"""
import json
import sys
from collections import OrderedDict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = ROOT / "data" / "index"
ENRICHED_DIR = ROOT / "data" / "enriched"
YEARS_DIR = ROOT / "years"

SECTION_ORDER = [
    "Television series",
    "Original net animations",
    "Original video animations",
    "Films",
    "Releases",
    "Unknown",
]


def section_sort_key(name):
    try:
        return (SECTION_ORDER.index(name), name)
    except ValueError:
        return (len(SECTION_ORDER), name)


def load_year(year):
    enriched = ENRICHED_DIR / f"{year}.json"
    if enriched.exists():
        return json.loads(enriched.read_text(encoding="utf-8")), True
    idx = INDEX_DIR / f"{year}.json"
    if idx.exists():
        return json.loads(idx.read_text(encoding="utf-8")), False
    return None, False


def escape_md(text):
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_entry(entry):
    lines = []
    title = entry.get("title") or "(untitled)"
    anilist = entry.get("anilist")
    display_title = title
    if anilist and anilist.get("title_english") and anilist["title_english"].lower() != title.lower():
        display_title = f"{title} ({anilist['title_english']})"
    lines.append(f"### {escape_md(display_title)}")
    lines.append("")

    if anilist and anilist.get("image_path"):
        rel = "../" + anilist["image_path"]
        lines.append(f'<img src="{rel}" alt="{escape_md(title)} cover" width="200">')
        lines.append("")

    meta_bits = []
    if entry.get("studio"):
        meta_bits.append(f"**Studio:** {escape_md(entry['studio'])}")
    if entry.get("episodes"):
        meta_bits.append(f"**Episodes:** {escape_md(entry['episodes'])}")
    if entry.get("director"):
        meta_bits.append(f"**Director:** {escape_md(entry['director'])}")
    when = entry.get("air_dates") or entry.get("release_date")
    if when:
        meta_bits.append(f"**Aired/Released:** {escape_md(when)}")
    if anilist and anilist.get("genres"):
        meta_bits.append(f"**Genres:** {escape_md(', '.join(anilist['genres']))}")
    if meta_bits:
        lines.append(" &nbsp;|&nbsp; ".join(meta_bits))
        lines.append("")

    synopsis = None
    source_note = None
    if anilist and anilist.get("description"):
        synopsis = anilist["description"]
        source_note = "AniList"
    if synopsis:
        lines.append(f"> {escape_md(synopsis)}")
        lines.append(">")
        lines.append(f"> _Synopsis source: {source_note}_")
        lines.append("")

    links = []
    if entry.get("wiki_url") and not entry.get("redlink"):
        links.append(f"[Wikipedia]({entry['wiki_url']})")
    if anilist and anilist.get("site_url"):
        links.append(f"[AniList]({anilist['site_url']})")
    if links:
        lines.append(" · ".join(links))
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def generate_year_page(year, entries, is_enriched):
    by_section = OrderedDict()
    for e in entries:
        by_section.setdefault(e.get("section", "Unknown"), []).append(e)

    matched = sum(1 for e in entries if e.get("anilist"))
    lines = [
        f"# {year} in Anime",
        "",
        "[← Back to index](../README.md)",
        "",
        f"{len(entries)} titles"
        + (f" · {matched} with AniList synopsis/art" if is_enriched else " · not yet enriched with synopsis/art")
        + f" · [source]({f'https://en.wikipedia.org/wiki/{year}_in_anime'})",
        "",
    ]
    for section in sorted(by_section.keys(), key=section_sort_key):
        lines.append(f"## {section}")
        lines.append("")
        for entry in by_section[section]:
            lines.append(render_entry(entry))
    YEARS_DIR.mkdir(parents=True, exist_ok=True)
    (YEARS_DIR / f"{year}.md").write_text("\n".join(lines), encoding="utf-8")


def build_decade_map(years):
    decades = OrderedDict()
    for y in years:
        decade = (y // 10) * 10
        decades.setdefault(decade, []).append(y)
    return decades


def generate_readme(all_years_meta):
    years = sorted(all_years_meta.keys())
    decades = build_decade_map(years)
    total_entries = sum(m["count"] for m in all_years_meta.values())
    total_matched = sum(m["matched"] for m in all_years_meta.values())

    lines = [
        "# List of Years in Anime",
        "",
        "An archive of anime organized by year, built from Wikipedia's "
        "[List of years in anime](https://en.wikipedia.org/wiki/List_of_years_in_anime) "
        "index and each year's article, enriched with synopses and cover art "
        "from the [AniList](https://anilist.co) API.",
        "",
        f"**{total_entries} titles** across {len(years)} years, "
        f"**{total_matched}** with a matched synopsis/cover image.",
        "",
    ]
    for decade in sorted(decades.keys()):
        lines.append(f"## {decade}s")
        lines.append("")
        links = []
        for y in sorted(decades[decade]):
            m = all_years_meta[y]
            marker = "" if m["matched"] > 0 or not m["enriched"] else ""
            links.append(f"[{y}](years/{y}.md)")
        lines.append(" · ".join(links))
        lines.append("")

    lines += [
        "## Attribution & licensing",
        "",
        "- Year-by-year title listings are drawn from Wikipedia "
        "(text available under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)).",
        "- Synopses and cover images are fetched from the AniList API "
        "(https://anilist.co), which is built for this kind of third-party display use.",
        "- Wikipedia's own cover art for anime articles is almost always "
        "non-free \"fair use\" content licensed only for use within that "
        "specific Wikipedia article, so it is intentionally **not** mirrored "
        "here -- entries without an AniList match simply have no image.",
        "- This is an unofficial, non-commercial fan archive/index, not affiliated "
        "with Wikipedia, the Wikimedia Foundation, or AniList.",
        "",
    ]
    (ROOT / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    years_present = sorted(
        {int(p.stem) for p in INDEX_DIR.glob("*.json")}
        | {int(p.stem) for p in ENRICHED_DIR.glob("*.json")}
    )
    meta = {}
    for year in years_present:
        entries, is_enriched = load_year(year)
        if entries is None:
            continue
        generate_year_page(year, entries, is_enriched)
        meta[year] = {
            "count": len(entries),
            "matched": sum(1 for e in entries if e.get("anilist")),
            "enriched": is_enriched,
        }
        print(f"{year}: page generated ({meta[year]['count']} entries)")
    generate_readme(meta)
    print(f"\nREADME.md and {len(meta)} year pages generated.")


if __name__ == "__main__":
    main()
