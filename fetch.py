#!/usr/bin/env python3
"""
Photobook fetcher.

Reads photographers.yml, visits each photographer's website, collects the
addresses of their photos and writes images.json for the viewer (index.html).

It does NOT copy anyone's photos. It only stores links to the images on the
photographers' own websites, and every picture in the viewer links back to them.

Each website is opened in a real (headless) browser, so pages built with
JavaScript work too. It scrolls each page so lazy-loaded photos appear, and
follows the site's own gallery links to find more work.

Runs once a day on GitHub Actions (see .github/workflows/update.yml).
Needs: PyYAML and Playwright (installed by the workflow).
"""

import asyncio
import json
import os
import random
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
HIDDEN_FILE = ROOT / "hidden.json"   # photos you removed in the book

USER_AGENT = (
    "Mozilla/5.0 (compatible; PersonalPhotobook/1.0; "
    "+https://github.com) personal non-commercial photo reader"
)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TIMEOUT = 25
MAX_IMAGES = 150         # photos kept per photographer
MAX_PAGES = 24           # pages opened per photographer
MAX_DEPTH = 2            # how many clicks away from the start page to explore
MAX_SECONDS = 300        # time budget per photographer
PARALLEL = 4             # photographers checked at the same time
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


def get(url, ignore_robots=False):
    """Download a page as text. Returns (final_url, text)."""
    if not ignore_robots and not allowed_by_robots(url):
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

def upgrade_url(url):
    """Ask common image services for a large version instead of a thumbnail."""
    low = url.lower()
    if "squarespace-cdn.com" in low or "static1.squarespace.com" in low:
        return url.split("?")[0] + "?format=1500w"
    if "wixstatic.com/media/" in low:
        # .../media/abc.jpg/v1/fill/w_300,h_200,.../abc.jpg -> 1600px version
        return re.sub(r"(/media/[^/]+)/v1/[^/]+/[^/]+/", r"\1/v1/fit/w_1600,h_1600,q_90/", url)
    if "cargo.site" in low:
        return re.sub(r"/w/\d+/", "/w/1600/", url)
    if "/wp-content/uploads/" in low:
        url = re.sub(r"-\d{2,4}x\d{2,4}(?=\.(?:jpe?g|png|webp)(?:\?|$))", "", url, flags=re.I)
    parts = urllib.parse.urlsplit(url)
    if parts.query:
        q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        changed = False
        for i, (k, v) in enumerate(q):
            if k.lower() in ("w", "width") and v.isdigit() and int(v) < 1200:
                q[i] = (k, "1600")
                changed = True
        if changed:
            url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))
    return url


def image_key(url):
    """Two addresses for the same photo (different sizes) get the same key."""
    u = url.lower().split("#")[0]
    if "squarespace-cdn.com" in u:
        u = u.split("?")[0]                      # same photo, different ?format= size
    elif "wixstatic.com" in u:
        u = u.split("?")[0].split("/v1/")[0]     # .../media/abc.jpg/v1/fill/... -> .../media/abc.jpg
    u = re.sub(r"/w/\d+/", "/", u)
    u = re.sub(r"-\d{2,4}x\d{2,4}(?=\.(?:jpe?g|png|webp))", "", u)
    u = re.sub(r"[?&](w|width|h|height|q|quality|format|fit|auto|dpr)=[^&]*", "", u)
    return u


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
    return upgrade_url(url)


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


# --------------------------------------------------------------------------
# Browser crawler
# --------------------------------------------------------------------------

# Runs inside each page: lists every photo-like image and every link.
EXTRACT_JS = r"""
() => {
  const chrome = el => el.closest('header, nav, footer, [role=banner], [role=navigation], [role=contentinfo]');
  const best = s => {
    if (!s) return null;
    let b = null, bw = -1;
    for (const part of s.split(/,(?=\s*\S+\s+\d)/)) {
      const bits = part.trim().split(/\s+/);
      const d = bits[1] || '';
      const n = d.endsWith('w') ? parseInt(d) : d.endsWith('x') ? parseFloat(d) * 1000 : 0;
      if (bits[0] && n >= bw) { b = bits[0]; bw = n; }
    }
    return b ? [b, bw] : null;
  };
  const isImgUrl = u => /\.(jpe?g|png|webp)(\?|$)/i.test(u || '');
  const out = [];
  for (const img of document.querySelectorAll('img')) {
    if (chrome(img)) continue;
    const r = img.getBoundingClientRect();
    const ss = best(img.getAttribute('srcset')) || best(img.getAttribute('data-srcset'));
    let ps = null;
    const pic = img.closest('picture');
    if (pic) for (const s of pic.querySelectorAll('source')) {
      const c = best(s.getAttribute('srcset') || s.getAttribute('data-srcset'));
      if (c && (!ps || c[1] > ps[1])) ps = c;
    }
    const a = img.closest('a');
    const full = a && isImgUrl(a.href) ? a.href : null;
    const lazy = img.getAttribute('data-image') || img.getAttribute('data-src') ||
                 img.getAttribute('data-lazy-src') || img.getAttribute('data-original') || null;
    const sized = (ps && (!ss || ps[1] > ss[1])) ? ps : ss;
    out.push({
      src: full || (sized && sized[0]) || lazy || img.currentSrc || img.src,
      nw: img.naturalWidth, nh: img.naturalHeight, rw: r.width, rh: r.height,
      sw: sized ? sized[1] : 0, alt: img.alt || '', full: !!full, lazy: !!lazy,
    });
  }
  let n = 0;
  for (const el of document.querySelectorAll('div, section, figure, a, li, span')) {
    if (++n > 5000) break;
    const bg = getComputedStyle(el).backgroundImage;
    if (!bg || bg === 'none' || bg.indexOf('url(') < 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 250 || r.height < 180 || chrome(el)) continue;
    const m = bg.match(/url\(["']?(.*?)["']?\)/);
    if (m) out.push({src: m[1], nw: 0, nh: 0, rw: r.width, rh: r.height, sw: 0, alt: '', bg: true});
  }
  const links = [...document.querySelectorAll('a[href]')].map(a => ({
    href: a.href,
    text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 80),
    nav: !!a.closest('header, nav, [role=navigation], [class*=menu], [class*=nav]'),
  }));
  return {images: out, links: links, url: location.href, html: document.documentElement.outerHTML.slice(0, 500000)};
}
"""

SCROLL_JS = """
async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  let still = 0, last = 0;
  for (let i = 0; i < 40 && still < 2; i++) {
    window.scrollBy(0, Math.max(400, window.innerHeight * 0.9));
    await sleep(350);
    const h = document.documentElement.scrollHeight;
    const bottom = window.innerHeight + window.scrollY >= h - 5;
    if (bottom && h === last) still++; else still = 0;
    last = h;
  }
  window.scrollTo(0, 0);
}
"""

GALLERY_WORDS = re.compile(
    r"portfolio|work|gallery|galleries|project|series|photo|picture|image|collection|archive|"
    r"selected|street|colou?r|black|white|city|cities|stories|story|books?|essay|\b(19|20)\d\d\b", re.I)
SKIP_LINKS = re.compile(
    r"about|biograph|\bbio\b|contact|cart|checkout|login|log-in|signin|sign-in|account|privacy|terms|"
    r"cookie|faq|imprint|impressum|legal|newsletter|subscribe|workshop|tour|booking|press|\bcv\b|"
    r"search|/tag/|/tags/|/category/|/author/|/feed|wp-admin|wp-login|\.pdf|\.zip|mailto:|tel:|"
    r"javascript:|instagram\.com|facebook\.com|twitter\.com|x\.com|youtube\.com|vimeo\.com|"
    r"linkedin\.com|pinterest\.|tiktok\.com|/shop|/store|product|wedding|engagement", re.I)


def keep_image(rec):
    """Decide from size information whether an image is a real photo, not an icon."""
    if rec.get("full") or rec.get("bg"):
        return True
    natural = max(rec.get("nw") or 0, rec.get("nh") or 0)
    rendered = max(rec.get("rw") or 0, rec.get("rh") or 0)
    if (rec.get("sw") or 0) >= 600:
        return True
    if natural >= 400:
        return True
    if natural == 0 and (rendered >= 200 or rec.get("lazy")):
        return True   # not loaded yet (lazy) but has a real size or a lazy source
    return False


PAGINATION_TEXT = re.compile(r"\d{1,3}|next|next page|older|older posts|more|load more|›|»|→|>", re.I)
PAGINATION_HREF = re.compile(r"[?&](page|offset|p)=\d+|/page/\d+", re.I)


def is_pagination(link):
    """Links to the next part of the same list ("2", "Next", ?page=3)."""
    return bool(PAGINATION_TEXT.fullmatch((link.get("text") or "").strip())
                or PAGINATION_HREF.search(link["href"]))


def rank_link(link, start, follow_re):
    """Higher score = more likely to lead to more photos."""
    href = link["href"].split("#")[0]
    if not href.startswith("http") or not same_site(href, start):
        return None
    if re.search(r"\.(jpe?g|png|webp|gif|svg|mp4|mov)(\?|$)", href, re.I):
        return None
    text = link.get("text") or ""
    if follow_re is not None and follow_re.search(href):
        return 20
    if SKIP_LINKS.search(href) or SKIP_LINKS.search(text):
        return None
    score = 1
    if is_pagination(link):
        score += 2.5
    if link.get("nav"):
        score += 3
    if GALLERY_WORDS.search(href) or GALLERY_WORDS.search(text):
        score += 2
    score -= urllib.parse.urlsplit(href).path.strip("/").count("/") * 0.5
    return score


async def open_page(page, url, log):
    """Open a page, let it settle and scroll through it. Returns extracted data or None."""
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=40000)
    except Exception as e:
        msg = str(e).splitlines()[0][:120]
        log(f"    ! could not open {url}: {msg}")
        return None, "load: " + msg
    if resp is not None and resp.status >= 400:
        log(f"    ! {url} answered {resp.status}")
        return None, f"status: the site answered {resp.status}"
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    try:
        await page.evaluate(SCROLL_JS)
        await page.wait_for_timeout(800)
        return await page.evaluate(EXTRACT_JS), None
    except Exception as e:
        msg = str(e).splitlines()[0][:120]
        log(f"    ! could not read {url}: {msg}")
        return None, "read: " + msg


async def crawl_site(context, start, follow, log, budget, ignore_robots=False):
    """Explore one website. Returns (images, first_page_html, feeds, problem)."""
    follow_re = re.compile(follow) if follow else None
    page = await context.new_page()
    queue = [(100, 0, start)]          # (score, depth, url)
    seen_urls, found, counts = set(), {}, {}
    first_html, feeds, problems, opened = None, [], [], 0
    started = time.time()
    try:
        while queue and budget["pages"] > 0 and len(found) < budget["images"]:
            if time.time() - started > budget.get("seconds", MAX_SECONDS):
                log("    (time limit for this photographer reached)")
                break
            queue.sort(key=lambda x: -x[0])
            score, depth, url = queue.pop(0)
            key = url.split("#")[0].rstrip("/")
            if key in seen_urls:
                continue
            seen_urls.add(key)
            if not ignore_robots and not await asyncio.to_thread(allowed_by_robots, url):
                log(f"    - {url}: the site asks not to be collected (robots.txt)")
                problems.append("robots")
                budget.setdefault("details", []).append(f"{url} — skipped: robots.txt (set to respect)")
                continue
            data, problem = await open_page(page, url, log)
            opened += 1
            budget["pages"] -= 1
            if problem:
                problems.append(problem.split(":")[0])
                budget.setdefault("details", []).append(f"{url} — {problem.split(': ', 1)[-1]}")
                continue
            seen_urls.add(data["url"].split("#")[0].rstrip("/"))
            if first_html is None:
                first_html = data["html"]
            parser = PageParser(data["url"])
            parser.feed(data["html"])
            feeds += parser.feeds + ([urllib.parse.urljoin(data["url"], "/feed/")] if parser.is_wordpress else [])

            new_here = 0
            for rec in data["images"]:
                if not keep_image(rec):
                    continue
                src = clean_image_url(rec["src"], data["url"])
                if not src:
                    continue
                k = image_key(src)
                counts[k] = counts.get(k, 0) + 1
                if k not in found:
                    found[k] = {"src": src, "link": data["url"], "title": rec.get("alt", "")[:120]}
                    new_here += 1
                    # a few photos from each followed page (e.g. each artist), so many get a turn
                    if depth > 0 and budget.get("per_page") and new_here >= budget["per_page"]:
                        break
            log(f"    {new_here:3d} new photos on {data['url']}")

            # From a "follow" page we only want its main photo, not more links
            if follow_re is not None and depth > 0:
                continue
            if depth < MAX_DEPTH:
                for link in data["links"]:
                    r = rank_link(link, start, follow_re)
                    if r is None:
                        continue
                    if r == 20 and budget.get("shuffle"):
                        r += random.random()     # a different selection on each run
                    href = link["href"].split("#")[0]
                    if href.rstrip("/") not in seen_urls:
                        # the next part of the same list doesn't count as going deeper
                        queue.append((r, depth if is_pagination(link) else depth + 1, href))
    finally:
        await page.close()

    # Drop things that appear on nearly every page (site decoration, not photos)
    if opened >= 4:
        limit = max(3, int(opened * 0.6))
        for k in [k for k, c in counts.items() if c >= limit]:
            found.pop(k, None)

    problem = None
    if not found:
        if problems and all(p == "robots" for p in problems):
            problem = "robots"
        elif problems and opened and all(p in ("load", "status", "read") for p in problems):
            problem = "blocked"
    return list(found.values()), first_html, feeds, problem


async def collect(person, context, log):
    """Return a list of {src, link, title} for one photographer, from all their websites."""
    sites = site_list(person)
    # Personal reader: collect every site unless you set "robots: respect" for it
    ignore_robots = str(person.get("robots", "")).lower() != "respect"
    cap = int(person.get("max_photos") or MAX_IMAGES)
    pages = int(person.get("max_pages") or MAX_PAGES)
    budget = {"pages": pages + 4 * (len(sites) - 1), "images": cap,
              "per_page": int(person.get("per_page") or 0), "shuffle": bool(person.get("shuffle")),
              "seconds": max(MAX_SECONDS, pages * 15)}
    start_pages = budget["pages"]
    buckets, feeds, problems = [], list(person.get("feeds") or []), []
    person["_guess"] = None
    for start in sites:
        budget["images"] = cap          # each site may fill the book; we interleave below
        imgs, html, found_feeds, problem = await crawl_site(
            context, start, person.get("follow"), log, budget, ignore_robots)
        if html and not person["_guess"] and not person.get("name"):
            parser = PageParser(start)
            parser.feed(html)
            person["_guess"] = guess_name(parser, start)
        buckets.append(imgs)
        feeds += found_feeds
        if problem:
            problems.append(problem)

    # Feeds help with blogs: use them when asked for, or when pages gave too few photos
    if person.get("feeds") or sum(len(b) for b in buckets) < 6:
        for feed in list(dict.fromkeys(feeds))[:2]:
            try:
                final, text = await asyncio.to_thread(get, feed, ignore_robots)
                buckets.append(parse_feed(text, final))
            except Exception as e:
                log(f"    ! feed {feed}: {e}")

    # Take turns between websites so every site gets a fair share of pages
    images, seen = [], set()
    while len(images) < cap and any(buckets):
        for b in buckets:
            while b:
                img = b.pop(0)
                k = image_key(img["src"])
                if k not in seen:
                    seen.add(k)
                    images.append(img)
                    break
            if len(images) >= cap:
                break
        buckets = [b for b in buckets if b]

    person["_details"] = budget.get("details", [])[:5]
    person["_pages_opened"] = start_pages - budget["pages"]
    problem = None
    if not images and problems:
        problem = "robots" if all(p == "robots" for p in problems) else "blocked"
    return images, problem


PROBLEM_TEXT = {
    "robots": "This website asks automated tools not to collect its pages, so your book left it alone. Tap “Collect anyway” to change that.",
    "blocked": "This website couldn’t be opened automatically. It may block automated visitors, or it was down. Tap the link to visit.",
    None: "No photos could be found on this website. Try adding the address of a gallery page.",
}


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


async def run_all(people, previous, previous_by_site, now, hidden=frozenset()):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        launch = {"headless": True}
        if os.environ.get("PHOTOBOOK_CHROME"):
            launch["executable_path"] = os.environ["PHOTOBOOK_CHROME"]
        browser = await pw.chromium.launch(**launch)
        gate = asyncio.Semaphore(PARALLEL)

        async def one(person):
            async with gate:
                lines = []
                log = lines.append
                pid = person_id(person)
                first_site = (site_list(person) or [""])[0]
                old = previous.get(pid) or previous_by_site.get(first_site) or {}
                context = await browser.new_context(
                    user_agent=BROWSER_UA, viewport={"width": 1440, "height": 1000},
                    locale="en-US", ignore_https_errors=True)
                problem = None
                try:
                    images, problem = await collect(person, context, log)
                    error = None if images else PROBLEM_TEXT.get(problem, PROBLEM_TEXT[None])
                except Exception as e:  # never let one site break the whole run
                    images, error = [], f"Something went wrong: {e}"
                finally:
                    await context.close()
                if not images and old.get("images") and problem != "robots":
                    images = old["images"]
                    error = ("Couldn’t reach the website this time; showing the photos found before."
                             if problem == "blocked" else
                             "No photos were found this time; showing the photos found before.")
                if person.get("keep_old") and images and old.get("images"):
                    # keep earlier finds too (newest first), so a rotating selection builds up
                    have = {image_key(i["src"]) for i in images}
                    images = images + [i for i in old["images"] if image_key(i["src"]) not in have]
                    images = images[:int(person.get("max_photos") or MAX_IMAGES)]
                if hidden:
                    images = [i for i in images if image_key(i["src"]) not in hidden]
                first_seen = {image_key(i["src"]): i.get("first_seen", now) for i in old.get("images", [])}
                for img in images:
                    img["first_seen"] = first_seen.get(image_key(img["src"]), img.get("first_seen", now))
                name = (person.get("name") or person.get("_guess")
                        or (old.get("name") if old.get("auto_name") else None) or host_of(first_site))
                print(f"- {name}  →  {len(images)} photos" + (f"  ({error})" if error else ""), flush=True)
                for line in lines:
                    print(line, flush=True)
                return {
                    "id": pid,
                    "name": name,
                    "auto_name": not person.get("name"),
                    "site": first_site,
                    "sites": site_list(person),
                    "lists": person["lists"],
                    "status": person["status"],
                    "note": person.get("note", ""),
                    "error": error,
                    "robots_blocked": problem == "robots",
                    "_details": person.get("_details", []),
                    "_pages": person.get("_pages_opened", 0),
                    "robots_respected": str(person.get("robots", "")).lower() == "respect",
                    "images": images,
                }

        try:
            return await asyncio.gather(*(one(p) for p in people))
        finally:
            await browser.close()


def load_hidden():
    """Photos removed in the book: their keys, so other sizes of the same photo match too."""
    try:
        return {image_key(h["src"]) for h in json.loads(HIDDEN_FILE.read_text(encoding="utf-8")) if h.get("src")}
    except Exception:
        return set()


def main():
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
    people = load_people()
    started = time.time()
    hidden = load_hidden()
    if hidden:
        print(f"Leaving out {len(hidden)} photos you removed.", flush=True)
    result = asyncio.run(run_all(people, previous, previous_by_site, now, hidden))
    # A small summary of this run (easy to read without opening the big images.json)
    summary = [
        {"name": p["name"], "photos": len(p["images"]), "status": p["status"],
         "sites": p["sites"], "pages_opened": p.pop("_pages", 0),
         "problem": p["error"], "details": p.pop("_details", [])}
        for p in result
    ]
    summary.sort(key=lambda s: s["photos"])
    OUT_FILE.write_text(json.dumps({"updated": now, "photographers": result}, indent=1, ensure_ascii=False),
                        encoding="utf-8")
    (ROOT / "status.json").write_text(json.dumps(
        {"updated": now, "seconds": int(time.time() - started), "photographers": summary},
        indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(len(p["images"]) for p in result)
    empty = [p["name"] for p in result if not p["images"]]
    print(f"\nWrote {OUT_FILE.name}: {len(result)} photographers, {total} photos, "
          f"{int(time.time() - started)}s.", flush=True)
    if empty:
        print("No photos for: " + ", ".join(empty), flush=True)


if __name__ == "__main__":
    sys.exit(main())
