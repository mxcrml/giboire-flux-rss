#!/usr/bin/env python3
"""
Génère un flux RSS fusionnant les articles et conseils de giboire.com.

Site WordPress + Yoast SEO :
1. Découverte via les deux sitemaps (post-sitemap.xml + advice-sitemap.xml),
   qui fournissent URL + lastmod pour chaque article.
2. Nouveautés vs state_giboire.json ; les articles connus ne sont pas
   re-téléchargés.
3. Scrape de chaque nouvel article : titre, image, date de publication
   réelle (meta article:published_time), contenu converti en Markdown.
4. Écrit feed_giboire_blog.xml (RSS 2.0), trié par date de publication.

Usage : python giboire_blog_rss.py
Dépendances : pip install requests beautifulsoup4 feedgen markdownify
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from markdownify import markdownify

BASE = "https://www.giboire.com"
SITEMAPS = [
    f"{BASE}/post-sitemap.xml",
    f"{BASE}/advice-sitemap.xml",
]
STATE_FILE = Path(__file__).parent / "state_giboire.json"
FEED_FILE = Path(__file__).parent / "feed_giboire_blog.xml"
MAX_ITEMS = 30
SCRAPE_MAX = 40   # nb max d'articles récents à considérer (tri par lastmod)
CONTENT_MAX = 2500
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

JUNK_TAGS = ["nav", "header", "footer", "form", "script", "style",
             "iframe", "button", "input", "select", "textarea", "svg",
             "noscript", "aside"]

# Blocs de fin fréquents sur WordPress (partage, articles liés, commentaires)
STOP_PATTERNS = [
    re.compile(r"^.{0,10}Partage[rz]", re.M | re.I),
    re.compile(r"^.{0,10}Articles? (liés|similaires|récents)", re.M | re.I),
    re.compile(r"^.{0,10}(À|A) lire (aussi|également)", re.M | re.I),
    re.compile(r"^.{0,10}Laisser un commentaire", re.M | re.I),
    re.compile(r"^.{0,10}Newsletter", re.M | re.I),
]


def http_get(url, retries=2, **kw):
    """GET avec timeout généreux et retries (le site peut être lent)."""
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    raise last


def discover():
    """URLs + lastmod depuis les sitemaps Yoast. Retourne {url: lastmod|None}."""
    found = {}
    for sm in SITEMAPS:
        try:
            xml = http_get(sm).text
        except Exception as e:
            print(f"[sitemap] {sm} : {e}", file=sys.stderr)
            continue
        entries = re.findall(
            r"<url>\s*<loc>(.*?)</loc>(?:.*?<lastmod>(.*?)</lastmod>)?.*?</url>",
            xml, re.S)
        for loc, lastmod in entries:
            loc = loc.strip()
            # écarter la page d'accueil du blog et les URLs vides
            if loc and loc.rstrip("/") != BASE:
                found[loc] = lastmod.strip() or None
        print(f"[sitemap] {sm} : {len(entries)} entrées")
    return found


def html_to_markdown(container) -> str:
    for tag in container.find_all(JUNK_TAGS):
        tag.decompose()
    for img in container.find_all("img"):
        img.decompose()
    for a in container.find_all("a"):
        a.replace_with(a.get_text(" ", strip=True))
    md = markdownify(str(container), heading_style="ATX", bullets="-")
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def cut_at_stop_markers(md: str) -> str:
    cut = len(md)
    for pat in STOP_PATTERNS:
        m = pat.search(md)
        if m and m.start() < cut:
            cut = m.start()
    return md[:cut].strip()


def truncate(md: str, limit: int) -> str:
    if len(md) <= limit:
        return md
    cut = md.rfind(" ", 0, limit)
    return md[: cut if cut > 0 else limit].rstrip() + "..."


def parse_date(value):
    """ISO -> datetime UTC, tolère les formats Yoast/WordPress."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def scrape_article(url, lastmod):
    soup = BeautifulSoup(http_get(url).text, "html.parser")
    d = {"url": url}

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find(
            "meta", attrs={"name": prop}
        )
        return tag["content"].strip() if tag and tag.get("content") else None

    d["title"] = meta("og:title") or (soup.title.string.strip() if soup.title else url)
    d["image"] = meta("og:image")

    # Date de publication réelle (WordPress/Yoast), fallback lastmod du sitemap
    pub = parse_date(meta("article:published_time")) or parse_date(lastmod) \
        or datetime.now(timezone.utc)
    d["published"] = pub.isoformat()

    # Contenu : WordPress met l'article dans <article> ou l'entry-content
    container = (soup.find("article")
                 or soup.find(class_=re.compile(r"entry-content|post-content|single-content"))
                 or soup.find("main") or soup.body)
    md = html_to_markdown(container) if container else ""
    md = cut_at_stop_markers(md)
    md = truncate(md, CONTENT_MAX)
    d["content_md"] = md or meta("og:description") or d["title"]

    return d


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    found = discover()
    if not found:
        sys.exit("Aucun article trouvé dans les sitemaps - site inaccessible ?")
    print(f"[info] {len(found)} articles au total (posts + conseils)")

    # Ne considérer que les SCRAPE_MAX plus récents (lastmod décroissant) :
    # inutile de scraper tout l'historique du blog.
    recent = sorted(found.items(),
                    key=lambda kv: kv[1] or "", reverse=True)[:SCRAPE_MAX]
    recent_urls = {u for u, _ in recent}
    print(f"[info] {len(recent)} articles récents retenus")

    for url, lastmod in recent:
        if url in state:
            continue
        try:
            data = scrape_article(url, lastmod)
            state[url] = data
            print(f"[nouveau] {data['title']}")
            time.sleep(1)  # rester poli avec le serveur
        except Exception as e:
            print(f"[erreur] {url} : {e}", file=sys.stderr)

    for url in state:
        state[url]["active"] = url in recent_urls

    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))

    # --- Génération du flux RSS ---
    fg = FeedGenerator()
    fg.id(f"{BASE}/actualites")
    fg.title("Giboire - Actualités et conseils")
    fg.link(href=BASE, rel="alternate")
    fg.description("Articles et conseils immobiliers du groupe Giboire")
    fg.language("fr")

    active = [d for d in state.values() if d.get("active")]
    active.sort(key=lambda d: d["published"], reverse=True)

    # feedgen insère chaque entrée en tête : on ajoute du plus ancien au
    # plus récent pour obtenir un fichier trié newest-first (standard RSS).
    for d in reversed(active[:MAX_ITEMS]):
        fe = fg.add_entry()
        fe.id(d["url"])
        fe.link(href=d["url"])
        fe.title(d["title"])
        fe.description(d.get("content_md") or d["title"])
        if d.get("image"):
            fe.enclosure(d["image"], 0, "image/jpeg")
        fe.pubDate(datetime.fromisoformat(d["published"]))

    fg.rss_file(str(FEED_FILE), pretty=True)
    print(f"[ok] {FEED_FILE} généré - {len(active)} articles actifs")


if __name__ == "__main__":
    main()
