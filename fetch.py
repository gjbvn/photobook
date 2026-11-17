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
        self.meta = {}          # og:site_name, author, og:title
        self.ld_names = []      # names of people from JSON-LD
        self._in_ld = False
        self._ld_buf = []
        self._skip_depth = 0
        self._in_title = False
        self.is_wordpress = False

    def handle_starttag(self, tag, attrs_list):
        a = {k: (v or "") for k, v in attrs_list}
        if tag in SKIP_CONTAINERS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld, self._ld_buf = True, []
        if tag == "base" and a.get("href"):
            self.base = urllib.parse.urljoin(self.base, a["href"])
        if tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop in ("og:site_name", "author", "og:title", "twitter:title") and a.get("content") and prop not in self.meta:
                self.meta[prop] = a["content"].strip()
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
        if tag == "script" and self._in_ld:
            self._in_ld = False
            try:
                self._read_ld(json.loads("".join(self._ld_buf)))
            except Exception:
                pass

    def _read_ld(self, obj):
        if isinstance(obj, list):
            for o in obj:
                self._read_ld(o)
        elif isinstance(obj, dict):
            kind = obj.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Person" in kinds and isinstance(obj.get("name"), str):
                self.ld_names.append(obj["name"].strip())
            for key in ("@graph", "author", "creator", "founder"):
                if key in obj:
                    self._read_ld(obj[key])

    def handle_data(self, data):
        if self._in_ld:
            self._ld_buf.append(data)
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

GENERIC = {
    "photography", "photographer", "photographers", "photos", "photo", "photographs", "portfolio",
    "home", "homepage", "official", "website", "site", "studio", "prints", "print", "shop", "store",
    "gallery", "galleries", "blog", "journal", "welcome", "index", "untitled", "about", "work",
    "squarespace", "wordpress", "cargo", "format", "darkroom", "fine", "art", "film", "street",
    "landscape", "travel", "page", "the", "by", "and", "&", "of", "online",
}
SEPARATORS = re.compile(r"\s+[|\-–—·•»:]\s+|\s*[|·•»]\s*")


def clean_name(text):
    """'Jane Doe Photography | Portfolio' -> ['Jane Doe', 'Portfolio']"""
    out = []
    for part in SEPARATORS.split(text or ""):
        words = part.strip().strip(".,'\"").split()
        # drop generic words from the ends ("Jane Doe Photography" -> "Jane Doe")
        while words and words[-1].lower().strip(".,") in GENERIC:
            words.pop()
        while words and words[0].lower().strip(".,") in GENERIC:
            words.pop(0)
        if words:
            out.append(" ".join(words))
    return out


def looks_like_person(name):
    words = name.split()
    return (1 < len(words) <= 4 and len(name) <= 40
            and all(re.fullmatch(r"[A-ZÀ-ÝĐ][\w'’.\-]*", w) for w in words)
            and not any(w.lower() in GENERIC for w in words))


def guess_name(parser, url):
    """Work out the photographer's name from their website."""
    candidates = []  # (score, name)
    for n in parser.ld_names:
        candidates += [(6 + looks_like_person(c) * 3, c) for c in clean_name(n)]
    for key, bonus in (("author", 3), ("og:site_name", 2), ("og:title", 1), ("twitter:title", 1)):
        for c in clean_name(parser.meta.get(key, "")):
            if "@" not in c and "http" not in c.lower():
                candidates.append((bonus + looks_like_person(c) * 3, c))
    for c in clean_name(parser.title or ""):
        candidates.append((1 + looks_like_person(c) * 3, c))
    candidates = [(s, c) for s, c in candidates if 1 < len(c) <= 50]
    if candidates:
        return max(candidates, key=lambda x: x[0])[1]
    return host_of(url)


def host_of(url):
    return urllib.parse.urlsplit(url).hostname.lower().removeprefix("www.") if url else ""


def site_list(person):
    """All the websites for a photographer: 'site' first, then any 'pages'."""
    out = []
    for u in [person.get("site")] + list(person.get("pages") or []):
        if u and u not in out:
            out.append(u)
    return out


def collect_page(page, follow, log):
    """Photos from one website. Returns (images, feeds_found)."""
    found = []
    final, html = get(page)
    p = PageParser(final)
    p.feed(html)
    feeds = list(p.feeds)
    if p.is_wordpress:
        feeds.append(urllib.parse.urljoin(final, "/feed/"))

    targets = []
    if follow:
        pattern = re.compile(follow)
        for href in p.links:
            if same_site(href, final) and pattern.search(href) and href not in targets:
                targets.append(href)
    if targets:
        log(f"    following {len(targets[:MAX_FOLLOW])} linked pages on {page}")
        for href in targets[:MAX_FOLLOW]:
            try:
                f2, h2 = get(href)
            except Exception as e:
                log(f"    ! {href}: {e}")
                continue
            p2 = PageParser(f2)
            p2.feed(h2)
            pick = p2.images[0][0] if p2.images else clean_image_url(p2.og_image, f2)
            if pick:
                found.append({"src": pick, "link": f2, "title": p2.title or ""})
    else:
        for url, alt in p.images:
            found.append({"src": url, "link": final, "title": alt})
    return found, feeds, p


def collect(person, log):
    """Return a list of {src, link, title} for one photographer, from all their websites."""
    pages = site_list(person)
    feeds = list(person.get("feeds") or [])
    follow = person.get("follow")
    limit = MAX_IMAGES if len(pages) <= 1 else MAX_IMAGES + 20

    buckets, discovered = [], []
    person["_guess"] = None
    for page in pages:
        try:
            imgs, f, parser = collect_page(page, follow, log)
            if not person["_guess"] and not person.get("name"):
                person["_guess"] = guess_name(parser, page)
            buckets.append(imgs)
            discovered += f
            log(f"    {len(imgs)} photos on {page}")
        except Exception as e:
            log(f"    ! could not open {page}: {e}")

    # Feeds: when asked for, or when the websites themselves gave too few photos
    if feeds or sum(len(b) for b in buckets) < 6:
        for feed in (feeds or discovered[:2]):
            try:
                final, text = get(feed)
                buckets.append(parse_feed(text, final))
            except Exception as e:
                log(f"    ! feed {feed}: {e}")

    # Take turns between websites so every site gets a fair share of pages
    images, seen = [], set()
    while len(images) < limit and any(buckets):
        for b in buckets:
            while b:
                img = b.pop(0)
                if img["src"] not in seen:
                    seen.add(img["src"])
                    images.append(img)
                    break
            if len(images) >= limit:
                break
        buckets = [b for b in buckets if b]
    return images


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def person_id(person):
    """Named people go by their name; unnamed ones by their first website."""
    if person.get("name"):
        return slug(person["name"])
    return slug(host_of(site_list(person)[0]))


def load_people():
    data = yaml.safe_load(LIST_FILE.read_text(encoding="utf-8")) or {}
    people = []
    for status in ("following", "suggested"):
        for person in data.get(status) or []:
            if not person:
                continue
            person = dict(person)
            if person.get("name"):
                person["name"] = str(person["name"]).strip()
            if not person.get("name") and not site_list(person):
                continue
            person["status"] = status
            lists = person.get("lists") or []
            person["lists"] = [lists] if isinstance(lists, str) else list(lists)
            people.append(person)
    return people


def main():
    def log(msg):
        print(msg, flush=True)

    previous, previous_by_site = {}, {}
    if OUT_FILE.exists():
        try:
            for p in json.loads(OUT_FILE.read_text(encoding="utf-8")).get("photographers", []):
                previous[p["id"]] = p
                if p.get("site"):
                    previous_by_site[p["site"]] = p
        except Exception:
            pass

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = []
    for person in load_people():
        pid = person_id(person)
        first_site = (site_list(person) or [""])[0]
        log(f"- {person.get('name') or first_site}")
        old = previous.get(pid) or previous_by_site.get(first_site) or {}
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
        name = person.get("name") or person.get("_guess") or (old.get("name") if old.get("auto_name") else None) or host_of(first_site)
        if not person.get("name"):
            log(f"    name found: {name}")
        result.append({
            "id": pid,
            "name": name,
            "auto_name": not person.get("name"),
            "site": (site_list(person) or [""])[0],
            "sites": site_list(person),
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
