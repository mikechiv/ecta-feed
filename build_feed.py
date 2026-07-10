#!/usr/bin/env python3
"""
Build an RSS feed for Econometrica, combining:
  1. Forthcoming papers (scraped from the Econometric Society website --
     these have no DOIs yet, so CrossRef can't see them)
  2. Articles in the latest published issue (via CrossRef)

The forthcoming-papers page has no dates, so first-seen dates are stored
in seen.json (committed alongside feed.xml) to give RSS items stable
timestamps. Stdlib only - no pip installs needed.
"""

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from xml.sax.saxutils import escape

ISSN = "1468-0262"  # Econometrica online ISSN (for the latest-issue query)
JOURNAL_NAME = "Econometrica"
FEED_TITLE = f"{JOURNAL_NAME} - Forthcoming + Latest Issue"
FEED_LINK = "https://www.econometricsociety.org/publications/econometrica"
FORTHCOMING_URL = ("https://www.econometricsociety.org/publications/"
                   "econometrica/forthcoming-papers")
OUTPUT = "feed.xml"
STATE_FILE = "seen.json"

BASE = f"https://api.crossref.org/journals/{ISSN}/works"
UA = {"User-Agent": "Mozilla/5.0 (compatible; ecta-feed/1.0; "
                    "mailto:you@example.com)"}


# ---------------------------------------------------------------- fetching

def fetch_url(url, attempts=4):
    """GET a URL with retries; returns raw bytes."""
    last_error = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:
            last_error = e
            wait = 10 * (attempt + 1)
            print(f"Fetch failed ({e}); retrying in {wait}s "
                  f"[{attempt + 1}/{attempts}]")
            if attempt < attempts - 1:
                time.sleep(wait)
    raise last_error


def fetch_crossref(url, attempts=4):
    return json.loads(fetch_url(url, attempts))["message"]["items"]


# ------------------------------------------- forthcoming papers (scraper)

class ForthcomingParser(HTMLParser):
    """
    The page lists each paper as:
        <h3>Title</h3>
        Author line
        <a href=".../file/NNNNN-N.pdf">View</a> ...
    A record is only kept if it has a /file/ PDF link, which filters out
    unrelated headings elsewhere on the page.
    """

    def __init__(self):
        super().__init__()
        self.papers = []
        self.current = None
        self.in_h3 = False
        self.after_h3 = False   # collecting the author line

    def handle_starttag(self, tag, attrs):
        if tag == "h3":
            self._flush()
            self.in_h3 = True
            self.current = {"title": "", "authors": "", "pdf": None}
        elif tag == "a" and self.current and self.after_h3:
            href = dict(attrs).get("href", "")
            if "/file/" in href and href.endswith(".pdf"):
                if not self.current["pdf"]:
                    self.current["pdf"] = href
                self.after_h3 = False  # author line ends at first link

    def handle_endtag(self, tag):
        if tag == "h3" and self.in_h3:
            self.in_h3 = False
            self.after_h3 = True

    def handle_data(self, data):
        if self.in_h3:
            self.current["title"] += data
        elif self.current and self.after_h3:
            self.current["authors"] += data

    def _flush(self):
        c = self.current
        if c and c["pdf"] and c["title"].strip():
            m = re.search(r"/file/(\d+)", c["pdf"])
            self.papers.append({
                "id": f"ecta-forthcoming-{m.group(1) if m else c['pdf']}",
                "title": " ".join(c["title"].split()),
                "authors": " ".join(c["authors"].split()),
                "pdf": c["pdf"],
            })
        self.current = None

    def close(self):
        self._flush()
        super().close()


def get_forthcoming():
    html = fetch_url(FORTHCOMING_URL).decode("utf-8", errors="replace")
    parser = ForthcomingParser()
    parser.feed(html)
    parser.close()
    for p in parser.papers:
        if p["pdf"].startswith("/"):
            p["pdf"] = "https://www.econometricsociety.org" + p["pdf"]
    return parser.papers


# ------------------------------------------- latest issue (via CrossRef)

def is_article(item):
    if item.get("type") != "journal-article":
        return False
    title = (item.get("title") or [""])[0].lower()
    if title.startswith(("erratum", "correction", "corrigendum",
                         "retraction", "forthcoming papers",
                         "front matter", "back matter")):
        return False
    return bool(item.get("title"))


def in_issue(item):
    return bool(item.get("volume")) or bool(item.get("issue"))


def published_dt(item):
    for key in ("published", "published-online", "published-print"):
        parts = item.get(key, {}).get("date-parts", [[None]])[0]
        if parts and parts[0]:
            y = parts[0]
            m = parts[1] if len(parts) > 1 else 1
            d = parts[2] if len(parts) > 2 else 1
            return datetime(y, m, d, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def get_latest_issue():
    try:
        items = fetch_crossref(f"{BASE}?sort=published&order=desc&rows=100")
    except Exception as e:
        print(f"WARNING: latest-issue query failed ({e}); "
              "building feed with forthcoming papers only this run.")
        return [], None
    issue_items = [w for w in items if is_article(w) and in_issue(w)]
    if not issue_items:
        return [], None
    newest = max(issue_items, key=published_dt)
    vol, iss = newest.get("volume"), newest.get("issue")
    latest = [w for w in issue_items
              if w.get("volume") == vol and w.get("issue") == iss]
    label = f"Volume {vol}" + (f", Issue {iss}" if iss else "")
    return latest, label


def crossref_authors(item):
    names = []
    for a in item.get("author", []):
        full = f"{a.get('given', '')} {a.get('family', '')}".strip()
        if full:
            names.append(full)
    return ", ".join(names)


# ------------------------------------------------------- state & output

def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=1, sort_keys=True)


def rss_item(guid, title, link, section, date, authors=""):
    desc_bits = [f"[{section}]"]
    if authors:
        desc_bits.append(f"Authors: {authors}")
    return "\n".join([
        "<item>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(link)}</link>",
        f"<guid isPermaLink=\"false\">{escape(guid)}</guid>",
        f"<category>{escape(section)}</category>",
        f"<pubDate>{format_datetime(date)}</pubDate>",
        f"<description>{escape('<br/><br/>'.join(desc_bits))}</description>",
        "</item>",
    ])


def main():
    forthcoming = get_forthcoming()
    if not forthcoming:
        raise SystemExit("Scraper found 0 forthcoming papers - the page "
                         "layout may have changed; aborting so the feed "
                         "isn't wiped.")
    issue, issue_label = get_latest_issue()

    seen = load_seen()
    today = datetime.now(timezone.utc)
    entries = []

    for p in sorted(forthcoming,
                    key=lambda p: seen.get(p["id"], today.isoformat()),
                    reverse=True):
        first_seen = seen.setdefault(p["id"], today.isoformat())
        date = datetime.fromisoformat(first_seen)
        entries.append(rss_item(p["id"], p["title"], p["pdf"],
                                "Forthcoming", date, p["authors"]))

    for w in issue:
        entries.append(rss_item(w["DOI"], w["title"][0],
                                f"https://doi.org/{w['DOI']}",
                                issue_label, published_dt(w),
                                crossref_authors(w)))

    rss = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{escape(FEED_TITLE)}</title>",
        f"<link>{escape(FEED_LINK)}</link>",
        "<description>Econometrica forthcoming papers (accepted "
        "manuscripts) and the latest published issue</description>",
        f"<lastBuildDate>{format_datetime(today)}</lastBuildDate>",
        *entries,
        "</channel>",
        "</rss>",
    ])
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(rss)
    save_seen(seen)
    print(f"Wrote {OUTPUT}: {len(forthcoming)} forthcoming, "
          f"{len(issue)} from {issue_label}")


if __name__ == "__main__":
    main()
