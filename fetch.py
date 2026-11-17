#!/usr/bin/env python3
"""
Photobook fetcher.

Reads photographers.yml, visits each photographer's website, collects the
addresses of their photos and writes images.json for the viewer (index.html).

It does NOT copy anyone's photos. It only stores links to the images on the
photographers' own websites, and every picture in the viewer links back to them.

Runs once a day on GitHub Actions (see .github/workflows/update.yml).
Only uses Python's standard library plus PyYAML.
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
LIST_FILE = ROOT / "photographers.yml"
OUT_FILE = ROOT / "images.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; PersonalPhotobook/1.0; "
    "+https://github.com) personal non-commercial photo reader"
)
TIMEOUT = 25
MAX_IMAGES = 40          # per photographer
MAX_FOLLOW = 15          # max linked pages to open per photographer
PAUSE = 1.0              # seconds between requests to the same site (be polite)

SKIP_WORDS = (
    "logo", "icon", "favicon", "avatar", "sprite", "badge", "placeholder",
    "spinner", "loader", "emoji", "gravatar", "pixel", "tracking", "banner-ad",
    "social", "instagram", "youtube", "facebook", "twitter", "payment",
)
SKIP_EXT = (".svg", ".gif", ".ico")
SKIP_CONTAINERS = {"header", "nav", "footer"}


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------

_robots = {}
_last_hit = {}


def allowed_by_robots(url):
    """Respect each site's robots.txt."""
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
        except Exception:
            rp = None  # no robots.txt (or unreachable) -> allowed
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def get(url):
    """Download a page as text. Returns (final_url, text)."""
    if not allowed_by_robots(url):
        raise RuntimeError(f"robots.txt asks not to fetch {url}")
    host = urllib.parse.urlsplit(url).netloc
    wait = PAUSE - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        _last_hit[host] = time.time()
        charset = r.headers.get_content_charset() or "utf-8"
        return r.geturl(), r.read().decode(charset, "replace")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def clean_image_url(src, base):
    if not src:
        return None
    src = src.strip()
    if src.startswith("data:") or src.startswith("blob:"):
        return None
    url = urllib.parse.urljoin(base, src)
    if not url.startswith("http"):
        return None
    low = url.lower()
    path = urllib.parse.urlsplit(low).path
    if path.endswith(SKIP_EXT):
        return None
    if any(w in low for w in SKIP_WORDS):
        return None
    # Squarespace: ask for a large-but-sensible size
    if "squarespace-cdn.com" in low or "static1.squarespace.com" in low:
        url = url.split("?")[0] + "?format=1500w"
    return url


def best_from_srcset(srcset):
    """Pick the widest candidate from a srcset attribute."""
    best, best_w = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1 and bits[1].endswith("w"):
            try:
                w = int(bits[1][:-1])
            except ValueError:
                w = 0
        if w >= best_w:
            best, best_w = bits[0], w
    return best


def too_small(attrs):
    for key in ("width", "height"):
        v = attrs.get(key, "")
        if v.isdigit() and int(v) < 200:
            return True
    return False


def same_site(a, b):
    ha = urllib.parse.urlsplit(a).netloc.lower().removeprefix("www.")
    hb = urllib.parse.urlsplit(b).netloc.lower().removeprefix("www.")
    return ha == hb


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------

class PageParser(HTMLParser):
    """Collects content images (skipping header/nav/footer), links, feeds, og:image."""

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.images = []        # list of (url, alt)
        self.links = []         # hrefs
        self.feeds = []
        self.og_image = None
        self.title = None
        self._skip_depth = 0
        self._in_title = False
        self.is_wordpress = False

    def handle_starttag(self, tag, attrs_list):
        a = {k: (v or "") for k, v in attrs_list}
        if tag in SKIP_CONTAINERS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "base" and a.get("href"):
            self.base = urllib.parse.urljoin(self.base, a["href"])
        if tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop in ("og:image", "twitter:image") and not self.og_image:
                self.og_image = a.get("content")
            if prop == "generator" and "wordpress" in a.get("content", "").lower():
                self.is_wordpress = True
        if tag == "link":
            rel = a.get("rel", "").lower()
            typ = a.get("type", "").lower()
            if "alternate" in rel and ("rss" in typ or "atom" in typ):
                self.feeds.append(urllib.parse.urljoin(self.base, a.get("href", "")))
        if tag == "a" and a.get("href"):
            self.links.append(urllib.parse.urljoin(self.base, a["href"]))
        if tag in ("img", "source") and not self._skip_depth:
            if tag == "img" and too_small(a):
                return
            candidates = [
                a.get("data-image"), a.get("data-src"), a.get("data-lazy-src"),
                a.get("data-original"),
                best_from_srcset(a.get("data-srcset", "")) if a.get("data-srcset") else None,
                best_from_srcset(a.get("srcset", "")) if a.get("srcset") else None,
                a.get("src"),
            ]
            for c in candidates:
                url = clean_image_url(c, self.base)
                if url:
                    self.images.append((url, a.get("alt", "").strip()))
                    break
        src = a.get("src", "") + a.get("href", "")
        if "wp-content" in src or "wp-includes" in src:
            self.is_wordpress = True

    def handle_endtag(self, tag):
        if tag in SKIP_CONTAINERS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and data.strip() and not self.title:
            self.title = data.strip()


# --------------------------------------------------------------------------
# Feed parsing (RSS / Atom)
# --------------------------------------------------------------------------

IMG_IN_HTML = re.compile(r"""<img[^>]+?(?:data-src|src)\s*=\s*["']([^"']+)["']""", re.I)


def parse_feed(xml_text, base):
    out = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "replace"))
    except ET.ParseError:
        return out
    items = [el for el in root.iter() if el.tag.split("}")[-1] in ("item", "entry")]
    for item in items:
        link, title = base, ""
        found = []
        for child in item.iter():
            name = child.tag.split("}")[-1]
            if name == "link":
                link = child.get("href") or (child.text or "").strip() or link
            elif name == "title" and not title:
                title = (child.text or "").strip()
            elif name in ("content", "thumbnail") and child.get("url"):
                if (child.get("medium") or "image") == "image" or "image" in (child.get("type") or ""):
                    found.append(child.get("url"))
            elif name == "enclosure" and "image" in (child.get("type") or ""):
                found.append(child.get("url"))
            if child.text and "<img" in child.text:
                found.extend(IMG_IN_HTML.findall(child.text))
        for f in found:
            url = clean_image_url(f, link)
            if url:
                out.append({"src": url, "link": link, "title": title})
    return out


# --------------------------------------------------------------------------
# Main collection logic
# --------------------------------------------------------------------------

def collect(person, log):
    """Return a list of {src, link, title} for one photographer."""
    images = []
    seen = set()

    def add(src, link, title=""):
        if src and src not in seen and len(images) < MAX_IMAGES:
            seen.add(src)
            images.append({"src": src, "link": link, "title": title})

    site = person.get("site")
    pages = person.get("pages") or ([site] if site else [])
    feeds = list(person.get("feeds") or [])
    follow = person.get("follow")
    discovered_feeds = []

    for page in pages:
        try:
            final, html = get(page)
        except Exception as e:
            log(f"    ! could not open {page}: {e}")
            continue
        p = PageParser(final)
        p.feed(html)
        discovered_feeds += p.feeds
        if p.is_wordpress:
            discovered_feeds.append(urllib.parse.urljoin(final, "/feed/"))

        if follow:
            pattern = re.compile(follow)
            targets = []
            for href in p.links:
                if same_site(href, final) and pattern.search(href) and href not in targets:
                    targets.append(href)
            log(f"    following {len(targets[:MAX_FOLLOW])} linked pages")
            for href in targets[:MAX_FOLLOW]:
                try:
                    f2, h2 = get(href)
                except Exception as e:
                    log(f"    ! {href}: {e}")
                    continue
                p2 = PageParser(f2)
                p2.feed(h2)
                pick = p2.images[0][0] if p2.images else clean_image_url(p2.og_image, f2)
                add(pick, f2, p2.title or "")
        else:
            for url, alt in p.images:
                add(url, final, alt)

    # Use feeds when asked for, or when the page itself had too few photos
    if feeds or len(images) < 6:
        for feed in (feeds or discovered_feeds[:1]):
            try:
                final, text = get(feed)
                for img in parse_feed(text, final):
                    add(img["src"], img["link"], img["title"])
            except Exception as e:
                log(f"    ! feed {feed}: {e}")

    return images


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_people():
    data = yaml.safe_load(LIST_FILE.read_text(encoding="utf-8")) or {}
    people = []
    for status in ("following", "suggested"):
        for person in data.get(status) or []:
            if not person or not person.get("name"):
                continue
            person = dict(person)
            person["status"] = status
            lists = person.get("lists") or []
            person["lists"] = [lists] if isinstance(lists, str) else list(lists)
            people.append(person)
    return people


def main():
    def log(msg):
        print(msg, flush=True)

    previous = {}
    if OUT_FILE.exists():
        try:
            for p in json.loads(OUT_FILE.read_text(encoding="utf-8")).get("photographers", []):
                previous[p["id"]] = p
        except Exception:
            pass

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = []
    for person in load_people():
        pid = slug(person["name"])
        log(f"- {person['name']}")
        old = previous.get(pid, {})
        first_seen = {img["src"]: img.get("first_seen", now) for img in old.get("images", [])}
        try:
            images = collect(person, log)
            error = None
        except Exception as e:  # never let one site break the whole run
            images, error = [], str(e)
        if not images and old.get("images"):
            images = old["images"]
            error = error or "No photos found this time; showing the last ones found."
        elif not images:
            error = error or "No photos could be found automatically."
        for img in images:
            img["first_seen"] = first_seen.get(img["src"], img.get("first_seen", now))
        log(f"    {len(images)} photos" + (f"  ({error})" if error else ""))
        result.append({
            "id": pid,
            "name": person["name"],
            "site": person.get("site") or (person.get("pages") or [""])[0],
            "lists": person["lists"],
            "status": person["status"],
            "note": person.get("note", ""),
            "error": error,
            "images": images,
        })

    OUT_FILE.write_text(json.dumps({"updated": now, "photographers": result}, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    log(f"Wrote {OUT_FILE.name} with {len(result)} photographers.")


if __name__ == "__main__":
    sys.exit(main())
