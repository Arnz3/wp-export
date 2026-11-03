#!/usr/bin/env python3
"""
wp_export.py — Haal alle content en beelden uit WordPress-sites, zet ze om naar
Markdown (zonder HTML-tags) en zet het resultaat per opticienId in een map,
optioneel geüpload naar Google Drive.

Bedoeld als neutrale export voor migratie/testen naar een ander framework.

Gebruik (zie README.md):
    python wp_export.py --sites sites.csv --out ./export
    python wp_export.py --sites sites.csv --out ./export --upload
    python wp_export.py --site https://opticien-x.be --opticien-id 1234 --out ./export
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlencode, urlunparse

import requests
import yaml
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optioneel
    pass

log = logging.getLogger("wp_export")

USER_AGENT = "Mozilla/5.0 (compatible; RUBIO-wp-export/1.0)"
TIMEOUT = 30

# Post types die geen echte content zijn
SKIP_TYPES = {
    "attachment",
    "nav_menu_item",
    "wp_block",
    "wp_template",
    "wp_template_part",
    "wp_navigation",
    "wp_global_styles",
    "wp_font_family",
    "wp_font_face",
    "elementor_library",
    "oembed_cache",
    "user_request",
    "customize_changeset",
}

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif", ".bmp", ".tif", ".tiff", ".ico"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".mp4", ".mov", ".mp3"}


# Overgebleven shortcodes (Visual Composer, Divi, Avada, Contact Form 7, ...)
SHORTCODE_RE = re.compile(
    r"\[/?(?:[a-z][a-z0-9]*[_-][\w-]*|gallery|caption|embed|video|audio|playlist|"
    r"wpforms|ninja_form|gravityform|rev_slider|layerslider)(?:\s[^\]]*)?\]",
    re.IGNORECASE,
)
HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?/?>")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def make_session(auth: tuple[str, str] | None = None) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4,
        connect=1,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers["User-Agent"] = USER_AGENT
    if auth:
        s.auth = auth
    return s


class ApiUnavailable(Exception):
    pass


class ResponseError(ValueError):
    """Response die niet verwerkt kon worden; bewaart de response voor de foutlog."""

    def __init__(self, message: str, response: requests.Response):
        super().__init__(message)
        self.response = response


def describe_error(e: BaseException, body_chars: int = 1000) -> dict:
    """Bouw een gedetailleerde, JSON-serialiseerbare beschrijving van een fout."""
    import traceback

    info: dict = {
        "error_type": f"{type(e).__module__}.{type(e).__qualname__}".removeprefix("builtins."),
        "error": str(e),
    }
    stage = getattr(e, "export_stage", None)
    if stage:
        info["stage"] = stage
    # zoek een HTTP-response in de exceptie of in de oorzaakketen
    resp, cur = None, e
    while cur is not None and resp is None:
        resp = getattr(cur, "response", None)
        if resp is None and getattr(cur, "request", None) is not None:
            req = cur.request
            info.setdefault("request", {"method": req.method, "url": req.url})
        cur = cur.__cause__ or cur.__context__
    if isinstance(resp, requests.Response):
        body = resp.text or ""
        info["request"] = {"method": resp.request.method if resp.request else None, "url": resp.url}
        info["response"] = {
            "status": resp.status_code,
            "reason": resp.reason,
            "content_type": resp.headers.get("Content-Type"),
            "length": len(resp.content or b""),
            "elapsed_s": round(resp.elapsed.total_seconds(), 2) if resp.elapsed else None,
            "body_start": body[:body_chars],
        }
    chain, cur = [], e.__cause__ or e.__context__
    while cur is not None:
        chain.append(f"{type(cur).__name__}: {cur}")
        cur = cur.__cause__ or cur.__context__
    if chain:
        info["caused_by"] = chain
    info["traceback"] = traceback.format_exception(type(e), e, e.__traceback__)
    return info


# --------------------------------------------------------------------------- #
# Hulpfuncties
# --------------------------------------------------------------------------- #
def slugify(value: str, fallback: str = "item") -> str:
    value = unquote(value or "").strip().lower()
    value = re.sub(r"[^\w\-]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-{2,}", "-", value).strip("-_")
    return value[:120] or fallback


def safe_dirname(value: str) -> str:
    """Behoud de opticienId zoals hij is; vervang enkel tekens die niet in een pad mogen."""
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(value).strip()).strip(". ")
    return value or "onbekend"


def html_to_text(value: str) -> str:
    """Titels/excerpts: HTML weg, entities decoderen."""
    if not value:
        return ""
    return html.unescape(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))


def strip_html_deep(obj):
    """ACF/meta-velden: haal HTML uit alle strings (recursief)."""
    if isinstance(obj, str):
        return html_to_text(obj) if "<" in obj else html.unescape(obj)
    if isinstance(obj, list):
        return [strip_html_deep(v) for v in obj]
    if isinstance(obj, dict):
        return {k: strip_html_deep(v) for k, v in obj.items()}
    return obj


def best_from_srcset(srcset: str) -> str | None:
    best, best_w = None, -1
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        w = 0
        if len(bits) > 1:
            m = re.match(r"(\d+(?:\.\d+)?)([wx])", bits[1])
            if m:
                w = float(m.group(1)) * (1000 if m.group(2) == "x" else 1)
        if w > best_w:
            best, best_w = url, w
    return best


def ext_of(url: str) -> str:
    return Path(urlparse(url).path).suffix.lower()


def dump_frontmatter(meta: dict) -> str:
    clean = {k: v for k, v in meta.items() if v not in (None, "", [], {})}
    return "---\n" + yaml.safe_dump(clean, allow_unicode=True, sort_keys=False, width=1000) + "---\n\n"


# --------------------------------------------------------------------------- #
# Beelden
# --------------------------------------------------------------------------- #
class ImageStore:
    """
    Houdt de beelden bij die in de content voorkomen (enkel de URL + alt/titel/caption).
    Er wordt niets gedownload: de Markdown verwijst rechtstreeks naar de URL op de site.
    """

    def __init__(self, prefer_originals: bool = True, extra_exts: set[str] | None = None):
        self.prefer_originals = prefer_originals
        self.allowed_exts = IMAGE_EXTS | (extra_exts or set())
        self.meta: dict[str, dict] = {}  # url -> info (alt, caption, ...)
        self.originals: dict[str, str] = {}  # verkleinde url -> originele url (uit mediabibliotheek)
        self.lock = threading.Lock()

    @staticmethod
    def _clean(url: str, base_url: str = "") -> str | None:
        if not url or url.startswith("data:"):
            return None
        url = urljoin(base_url, url.strip())
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        # querystrings van resizers (?resize=, ?w=) weg
        return urlunparse(parsed._replace(query="", fragment=""))

    def register_media(self, m: dict) -> None:
        """Koppel alle verkleinde versies uit de mediabibliotheek aan het origineel."""
        orig = self._clean(m.get("source_url") or "")
        if not orig:
            return
        sizes = ((m.get("media_details") or {}).get("sizes") or {}).values()
        with self.lock:
            for size in sizes:
                u = self._clean(size.get("source_url") or "")
                if u and u != orig:
                    self.originals[u] = orig

    def get(self, url: str, base_url: str = "", info: dict | None = None) -> str | None:
        """Geeft de (absolute, bij voorkeur originele) URL van het beeld terug, of None."""
        url = self._clean(url, base_url)
        if not url or (ext_of(url) and ext_of(url) not in self.allowed_exts):
            return None
        with self.lock:
            if self.prefer_originals:
                url = self.originals.get(url, url)
            m = self.meta.setdefault(url, {})
            if info:
                m.update({k: v for k, v in info.items() if v})
        return url


# --------------------------------------------------------------------------- #
# HTML -> Markdown
# --------------------------------------------------------------------------- #
class _MD(MarkdownConverter):
    def convert_img(self, el, text, *args, **kwargs):  # markdownify >= 0.11 signatuur
        alt = (el.attrs.get("alt") or "").replace("\n", " ").strip()
        src = el.attrs.get("src") or ""
        if not src:
            return ""
        title = el.attrs.get("title")
        t = f' "{title}"' if title else ""
        return f"\n\n![{alt}]({src}{t})\n\n"


def html_to_markdown(content_html: str, page_url: str, images: ImageStore) -> str:
    if not content_html:
        return ""
    content_html = SHORTCODE_RE.sub("", content_html)
    soup = BeautifulSoup(content_html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "template", "form", "button", "input", "select", "textarea", "link", "meta"]):
        tag.decompose()

    # iframes (YouTube, Google Maps, ...) -> gewone link
    for fr in soup.find_all("iframe"):
        src = fr.get("src") or fr.get("data-src")
        if src:
            a = soup.new_tag("a", href=urljoin(page_url, src))
            a.string = fr.get("title") or "Ingesloten inhoud"
            fr.replace_with(a)
        else:
            fr.decompose()

    # <picture><source srcset> -> beste bron op de <img>
    for pic in soup.find_all("picture"):
        img = pic.find("img")
        if img is not None:
            for source in pic.find_all("source"):
                if source.get("srcset") and not img.get("srcset"):
                    img["srcset"] = source["srcset"]
                source.decompose()
            pic.replace_with(img)

    # achtergrondbeelden uit inline style (Elementor, page builders)
    for el in soup.find_all(style=True):
        for m in re.finditer(r"url\(['\"]?([^'\")]+)['\"]?\)", el["style"]):
            src = images.get(m.group(1), page_url)
            if src:
                new = soup.new_tag("img", src=src, alt="")
                new["data-exported"] = "1"
                el.insert(0, new)

    for img in soup.find_all("img"):
        if img.get("data-exported"):
            img.attrs = {"src": img["src"], "alt": img.get("alt", "")}
            continue
        src = (
            img.get("data-orig-file")
            or img.get("data-large-file")
            or img.get("data-lazy-src")
            or img.get("data-src")
            or (best_from_srcset(img.get("data-srcset") or img.get("srcset") or ""))
            or img.get("src")
        )
        alt = img.get("alt", "")
        url = images.get(src, page_url, {"alt": alt}) if src else None
        if url:
            img.attrs = {"src": url, "alt": alt}
        else:
            img.decompose()

    # links: altijd absoluut; links naar beelden/documenten registreren
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        if ext_of(href) in images.allowed_exts:
            href = images.get(href, page_url) or href
        a["href"] = href

    md = _MD(heading_style="ATX", bullets="-", strip=["span", "div", "section", "article", "figure", "font", "center"]).convert_soup(soup)

    # opkuis
    md = HTML_TAG_RE.sub("", md)  # garantie: geen HTML-tags meer
    md = html.unescape(md)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = "\n".join(line.rstrip() for line in md.splitlines())
    return md.strip() + "\n"


# --------------------------------------------------------------------------- #
# WordPress REST API
# --------------------------------------------------------------------------- #
def response_json(r: requests.Response):
    """
    Parse de JSON van een REST-response. Sommige sites (plugins/shortcodes die
    tijdens het renderen echo'en) zetten HTML vóór de JSON; in dat geval zoeken
    we de JSON-payload die tot het einde van de response loopt.
    """
    try:
        return r.json()
    except ValueError:
        pass
    text = r.text
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            data, end = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if not text[end:].strip():
            log.debug("JSON gevonden na %d bytes rommel: %s", m.start(), r.url)
            return data
    ctype = r.headers.get("Content-Type", "?")
    raise ResponseError(
        f"geen geldige JSON in response (HTTP {r.status_code}, {ctype}, {len(r.content)} bytes) van {r.url}", r)


class WPClient:
    def __init__(self, base_url: str, session: requests.Session):
        self.base = base_url.rstrip("/")
        self.s = session
        self.mode = None  # "pretty" of "query"

    def url(self, route: str, params: dict | None = None) -> str:
        route = route.lstrip("/")
        if self.mode == "query":
            q = {"rest_route": "/" + route, **(params or {})}
            return f"{self.base}/?{urlencode(q)}"
        q = f"?{urlencode(params)}" if params else ""
        return f"{self.base}/wp-json/{route}{q}"

    def detect(self) -> dict:
        for mode in ("pretty", "query"):
            self.mode = mode
            try:
                r = self.s.get(self.url(""), timeout=TIMEOUT)
                if r.status_code == 200 and "application/json" in r.headers.get("Content-Type", ""):
                    data = response_json(r)
                    if isinstance(data, dict) and "namespaces" in data:
                        if "wp/v2" not in data.get("namespaces", []):
                            raise ApiUnavailable("wp/v2 namespace ontbreekt")
                        return data
            except (requests.RequestException, ValueError):
                continue
        raise ApiUnavailable("REST API niet bereikbaar")

    def get_json(self, route: str, params: dict | None = None):
        r = self.s.get(self.url(route, params), timeout=TIMEOUT)
        if r.status_code in (401, 403, 404):
            raise ApiUnavailable(f"{route}: HTTP {r.status_code}")
        r.raise_for_status()
        return r

    def get_all(self, route: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            p = {"per_page": 100, "page": page, **(params or {})}
            try:
                r = self.get_json(route, p)
            except requests.HTTPError as e:
                # sommige sites geven 400 als je voorbij de laatste pagina gaat
                if e.response is not None and e.response.status_code == 400 and page > 1:
                    break
                raise
            data = response_json(r)
            if not isinstance(data, list):
                break
            items.extend(data)
            total = int(r.headers.get("X-WP-TotalPages", "1") or 1)
            if page >= total or not data:
                break
            page += 1
        return items


# --------------------------------------------------------------------------- #
# Export van één site
# --------------------------------------------------------------------------- #
class SiteExporter:
    def __init__(self, opticien_id: str, url: str, out_root: Path, auth=None,
                 all_media: bool = False, prefer_originals: bool = True, max_pages: int = 2000,
                 huisstijl: bool = True, content: bool = True):
        self.opticien_id = str(opticien_id).strip()
        self.url = url.strip().rstrip("/")
        if not urlparse(self.url).scheme:
            self.url = "https://" + self.url
        self.root = out_root / safe_dirname(self.opticien_id)
        self.session = make_session(auth)
        self.auth = auth
        self.max_pages = max_pages
        self.prefer_originals = prefer_originals
        self.all_media = all_media
        self.huisstijl = huisstijl
        self.content = content
        self.images: ImageStore | None = None
        self.docs: list[dict] = []
        self.site_info: dict = {}
        self.method = ""
        self.tax_rest_base: dict[str, str] = {}
        self.stage = "init"

    # ---- gemeenschappelijk ------------------------------------------------ #
    def write_doc(self, folder: str, slug: str, meta: dict, body_md: str):
        d = self.root / folder
        d.mkdir(parents=True, exist_ok=True)
        base = slugify(slug, "item")
        path = d / f"{base}.md"
        i = 2
        while path.exists() and any(x["file"] == f"{folder}/{path.name}" for x in self.docs):
            path = d / f"{base}-{i}.md"
            i += 1
        title = meta.get("title") or base
        heading = f"# {title}\n\n"
        if body_md.lstrip().startswith(f"# {title}\n") or body_md.strip() == f"# {title}":
            heading = ""
        text = dump_frontmatter(meta) + heading + body_md.lstrip()
        path.write_text(text, encoding="utf-8")
        self.docs.append({"file": f"{folder}/{path.name}", "title": title, "type": folder,
                          "url": meta.get("url"), "date": meta.get("date")})

    # ---- REST-route -------------------------------------------------------- #
    def export_via_rest(self) -> None:
        wp = WPClient(self.url, self.session)
        self.stage = "rest: detectie API"
        root = wp.detect()
        self.method = f"rest-api ({wp.mode})"
        self.site_info = {
            "name": html_to_text(root.get("name", "")),
            "description": html_to_text(root.get("description", "")),
            "home": root.get("home") or root.get("url") or self.url,
        }
        ctx = {"context": "edit"} if self.auth else {}
        status = {"status": "any"} if self.auth else {}

        # taxonomieën (id -> naam)
        terms: dict[str, dict[int, str]] = {}
        self.tax_rest_base: dict[str, str] = {}
        self.stage = "rest: taxonomieën"
        try:
            taxes = response_json(wp.get_json("wp/v2/taxonomies"))
            for tax_slug, tax in (taxes or {}).items():
                rb = tax.get("rest_base") or tax_slug
                self.tax_rest_base[tax_slug] = rb
                ns = tax.get("rest_namespace") or "wp/v2"
                try:
                    terms[rb] = {t["id"]: html_to_text(t.get("name", "")) for t in wp.get_all(f"{ns}/{rb}")}
                except Exception as e:  # noqa: BLE001
                    log.debug("taxonomie %s overgeslagen: %s", rb, e)
        except Exception as e:  # noqa: BLE001
            log.debug("taxonomieën niet beschikbaar: %s", e)

        # mediabibliotheek
        media_by_id: dict[int, dict] = {}
        self.stage = "rest: mediabibliotheek"
        try:
            for m in wp.get_all("wp/v2/media", ctx):
                media_by_id[m["id"]] = m
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] mediabibliotheek niet leesbaar: %s", self.opticien_id, e)

        for m in media_by_id.values():
            self.images.register_media(m)
            mime = m.get("mime_type", "") or ""
            if mime.startswith("image/") or (self.all_media and ext_of(m.get("source_url", "")) in DOC_EXTS):
                self.images.get(m.get("source_url"), self.url, {
                    "alt": m.get("alt_text"),
                    "title": html_to_text((m.get("title") or {}).get("rendered", "")),
                    "caption": html_to_text((m.get("caption") or {}).get("rendered", "")),
                    "media_id": m.get("id"),
                })

        # post types
        self.stage = "rest: post types ophalen"
        types = response_json(wp.get_json("wp/v2/types"))
        for type_slug, t in types.items():
            if type_slug in SKIP_TYPES:
                continue
            rb = t.get("rest_base")
            if not rb:
                continue
            ns = t.get("rest_namespace") or "wp/v2"
            self.stage = f"rest: {ns}/{rb} ophalen"
            try:
                items = wp.get_all(f"{ns}/{rb}", {**ctx, **status})
            except (ApiUnavailable, requests.HTTPError) as e:
                log.debug("type %s overgeslagen: %s", type_slug, e)
                continue
            if not items:
                continue
            log.info("[%s] %s: %d items", self.opticien_id, rb, len(items))
            tax_keys = [k for k in t.get("taxonomies", [])]
            for it in items:
                self.stage = f"rest: {ns}/{rb} item id={it.get('id')} ({it.get('slug')}) verwerken"
                self._write_rest_item(rb, it, terms, tax_keys, media_by_id)

    def _write_rest_item(self, folder: str, it: dict, terms, tax_keys, media_by_id):
        link = it.get("link") or ""
        content = it.get("content") or {}
        content_html = content.get("rendered") or ""
        # in edit-context staat ook 'raw', maar 'rendered' bevat de uitgewerkte page-builder output
        content_source = "rest-api"
        # Sommige page builders (Cornerstone/X, Oxygen, Bricks, ...) renderen hun inhoud pas
        # bij het tonen van de pagina; de REST API geeft dan een (bijna) lege container.
        if link and not content.get("protected") and len(html_to_text(content_html)) < 30:
            live = self._fetch_rendered_content(link)
            if live and len(html_to_text(live)) > len(html_to_text(content_html)):
                log.debug("[%s] %s: lege REST-content, live pagina gebruikt", self.opticien_id, link)
                content_html, content_source = live, "live-page"
        body = html_to_markdown(content_html, link or self.url, self.images)

        featured = None
        fm_id = it.get("featured_media")
        if fm_id and fm_id in media_by_id:
            src = media_by_id[fm_id].get("source_url")
            featured = self.images.get(src, self.url)

        tax_out = {}
        for tax in tax_keys:
            key = self.tax_rest_base.get(tax, tax)
            if isinstance(it.get(key), list) and key in terms:
                names = [terms[key].get(i) for i in it[key] if terms[key].get(i)]
                if names:
                    tax_out[key] = names

        meta = {
            "title": html_to_text((it.get("title") or {}).get("rendered", "")) or it.get("slug"),
            "slug": it.get("slug"),
            "type": it.get("type"),
            "id": it.get("id"),
            "status": it.get("status"),
            "url": link,
            "path": urlparse(link).path if link else None,
            "date": it.get("date"),
            "modified": it.get("modified"),
            "parent_id": it.get("parent") or None,
            "menu_order": it.get("menu_order") or None,
            "template": it.get("template") or None,
            "excerpt": html_to_text((it.get("excerpt") or {}).get("rendered", "")),
            "featured_image": featured,
            "content_source": content_source if content_source != "rest-api" else None,
            **tax_out,
        }
        if isinstance(it.get("acf"), dict) and it["acf"]:
            meta["acf"] = strip_html_deep(it["acf"])
        if it.get("yoast_head_json"):
            y = it["yoast_head_json"]
            meta["seo"] = {"title": y.get("title"), "description": y.get("description")}
        self.write_doc(folder, it.get("slug") or str(it.get("id")), meta, body)

    # ---- live pagina (page builders) --------------------------------------- #
    @staticmethod
    def _extract_main(soup: BeautifulSoup):
        """Zoek het hoofdcontent-element van een gerenderde WordPress-pagina."""
        main = (
            soup.select_one(".entry-content")
            or soup.select_one("[data-elementor-type='wp-page'], [data-elementor-type='wp-post']")
            or soup.select_one("#cs-content, .cs-content")  # Cornerstone (X/Pro)
            or soup.select_one("#et-main-area .et_pb_section")  # Divi
            or soup.find("main")
            or soup.find("article")
            or soup.select_one("#content, #main, .site-content, .x-main")
            or soup.body
            or soup
        )
        for t in main.find_all(["header", "footer", "nav", "aside", "script", "style", "noscript"]):
            t.decompose()
        return main

    def _fetch_rendered_content(self, url: str) -> str | None:
        """Haal de live pagina op en geef de HTML van het hoofdcontent terug."""
        try:
            r = self.session.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            log.warning("[%s] live pagina %s niet op te halen: %s", self.opticien_id, url, e)
            return None
        if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
            log.warning("[%s] live pagina %s: HTTP %s (%s)", self.opticien_id, url, r.status_code,
                        r.headers.get("Content-Type"))
            return None
        main = self._extract_main(BeautifulSoup(r.text, "html.parser"))
        return str(main) if main.get_text(strip=True) else None

    # ---- sitemap/HTML-fallback -------------------------------------------- #
    def _sitemap_urls(self) -> list[str]:
        seen_maps, urls = set(), []
        queue = [f"{self.url}/wp-sitemap.xml", f"{self.url}/sitemap_index.xml", f"{self.url}/sitemap.xml"]
        try:
            r = self.session.get(f"{self.url}/robots.txt", timeout=TIMEOUT)
            if r.ok:
                queue += re.findall(r"(?im)^sitemap:\s*(\S+)", r.text)
        except requests.RequestException:
            pass
        while queue and len(urls) < self.max_pages:
            sm = queue.pop(0)
            if sm in seen_maps:
                continue
            seen_maps.add(sm)
            try:
                r = self.session.get(sm, timeout=TIMEOUT)
            except requests.RequestException:
                continue
            if r.status_code != 200 or "<" not in r.text[:200]:
                continue
            soup = BeautifulSoup(r.content, "xml")
            for loc in soup.select("sitemap > loc"):
                queue.append(loc.get_text(strip=True))
            for loc in soup.select("url > loc"):
                u = loc.get_text(strip=True)
                if ext_of(u) in IMAGE_EXTS:
                    continue
                if u not in urls:
                    urls.append(u)
        # sitemaps voor taxonomieën/auteurs zijn archieven: niet nuttig als content
        urls = [u for u in urls if not re.search(r"/(author|category|tag|page)/", urlparse(u).path)]
        return urls[: self.max_pages]

    def export_via_html(self) -> None:
        self.method = "sitemap-crawl"
        self.stage = "sitemap-fallback"
        urls = self._sitemap_urls() or [self.url + "/"]
        log.info("[%s] fallback: %d url's via sitemap", self.opticien_id, len(urls))
        for u in urls:
            try:
                r = self.session.get(u, timeout=TIMEOUT)
            except requests.RequestException as e:
                log.warning("[%s] %s: %s", self.opticien_id, u, e)
                continue
            if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            if not self.site_info:
                og = soup.find("meta", property="og:site_name")
                self.site_info = {"name": og["content"] if og else (soup.title.string if soup.title else ""),
                                  "home": self.url}
            title = ""
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)
            elif soup.title:
                title = soup.title.get_text(strip=True)
            desc = soup.find("meta", attrs={"name": "description"})
            main = self._extract_main(soup)
            body = html_to_markdown(str(main), u, self.images)
            path = urlparse(u).path.strip("/")
            slug = path.replace("/", "-") or "home"
            folder = "posts" if re.match(r"\d{4}/\d{2}/", path) else "pages"
            self.write_doc(folder, slug, {
                "title": title or slug,
                "url": u,
                "path": "/" + path + ("/" if path else ""),
                "seo": {"description": desc["content"]} if desc and desc.get("content") else None,
            }, body)

    # ---- sitemap / sitestructuur ------------------------------------------ #
    @staticmethod
    def _norm_url(url: str | None) -> str:
        """Vergelijkbare vorm: host zonder www, pad zonder slash op het einde, geen query/fragment."""
        if not url:
            return ""
        p = urlparse(url)
        host = p.netloc.lower().removeprefix("www.")
        return f"{host}{unquote(p.path).rstrip('/') or '/'}"

    def _parse_menu(self, ul, page_url: str) -> list[dict]:
        items = []
        for li in ul.find_all("li", recursive=False):
            a = li.find("a", recursive=False) or li.find("a")
            if not a:
                continue
            label_el = a.select_one(".x-anchor-text-primary, .x-anchor-text") or a
            label = re.sub(r"\s+", " ", label_el.get_text(" ", strip=True))
            href = (a.get("href") or "").strip()
            url = urljoin(page_url, href) if href and not href.startswith(("#", "javascript:")) else None
            sub = li.find("ul", recursive=False) or li.find("ul")
            item = {"label": label or (url or "?"), "url": url}
            if sub:
                children = self._parse_menu(sub, page_url)
                if children:
                    item["children"] = children
            items.append(item)
        return items

    def _menus(self) -> list[dict]:
        """Lees de menu's uit de HTML van de homepage (header & footer)."""
        r = self.session.get(self.url + "/", timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        candidates = (soup.select("nav.x-nav-wrap.desktop > ul")  # Pro/X navbar
                      + soup.select("ul.x-menu-first-level, ul.x-menu-inline, ul.x-menu-dropdown")  # Cornerstone
                      + soup.select("nav ul.menu, footer ul.menu, .x-colophon ul"))
        menus, seen = [], set()
        for ul in candidates:
            if ul.find_parent("ul") is not None:
                continue  # submenu, wordt via het bovenliggende menu meegenomen
            items = self._parse_menu(ul, self.url + "/")
            sig = tuple((i["label"], i["url"]) for i in items)
            if len(items) < 2 or sig in seen:
                continue  # dubbele (mobiele) versie of geen echt menu
            seen.add(sig)
            in_footer = ul.find_parent(["footer"]) is not None or ul.find_parent(class_=re.compile(r"colophon|footer")) is not None
            menus.append({"location": "footer" if in_footer else "header", "id": ul.get("id"), "items": items})
        # header eerst
        return sorted(menus, key=lambda m: m["location"] != "header")

    def write_sitemap(self) -> None:
        docs_by_url = {self._norm_url(d.get("url")): d for d in self.docs if d.get("url")}
        try:
            menus = self._menus()
        except requests.RequestException as e:
            log.warning("[%s] menu niet leesbaar: %s", self.opticien_id, e)
            menus = []
        try:
            xml_urls = self._sitemap_urls()
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] sitemap.xml niet leesbaar: %s", self.opticien_id, e)
            xml_urls = []

        in_menu: set[str] = set()

        def annotate(items):
            for it in items:
                d = docs_by_url.get(self._norm_url(it.get("url")))
                if d:
                    it["file"] = d["file"]
                    in_menu.add(d["file"])
                if it.get("url") and urlparse(it["url"]).netloc.removeprefix("www.") != \
                        urlparse(self.url).netloc.removeprefix("www."):
                    it["external"] = True
                annotate(it.get("children", []))

        for m in menus:
            annotate(m["items"])

        # paginaboom volgens URL-pad
        def path_of(d):
            return (urlparse(d.get("url") or "").path.strip("/")) if d.get("url") else d["file"]

        pages = [d for d in self.docs if d["type"] == "pages"]
        tree: dict = {}
        for d in sorted(pages, key=path_of):
            node = tree
            parts = [p for p in path_of(d).split("/") if p] or [""]
            for part in parts:
                node = node.setdefault(part, {"_children": {}})
                last = node
                node = node["_children"]
            last["_doc"] = d

        other = {}
        for d in self.docs:
            if d["type"] != "pages":
                other.setdefault(d["type"], []).append(d)

        exported = set(docs_by_url)
        missing = [u for u in xml_urls if self._norm_url(u) not in exported]

        # ---- markdown ----
        def link(label, d=None, url=None):
            if d:
                return f"[{label}]({d['file']})"
            if url:
                return f"[{label}]({url})"
            return label

        md = [f"# Sitemap – {self.site_info.get('name') or self.url}\n",
              f"Site: {self.url}  ",
              f"Pagina's: {len(pages)} · overige items: {sum(len(v) for v in other.values())} · "
              f"menu's: {len(menus)} · URL's in sitemap.xml: {len(xml_urls)}\n"]

        if menus:
            md.append("## Navigatie\n")
            for m in menus:
                md.append(f"### {'Hoofdmenu' if m['location'] == 'header' else 'Footermenu'}"
                          + (f" (`{m['id']}`)" if m.get("id") else "") + "\n")

                def render(items, depth=0):
                    for it in items:
                        d = docs_by_url.get(self._norm_url(it.get("url")))
                        extra = " ↗" if it.get("external") else ""
                        path = f" — `{urlparse(it['url']).path or '/'}`" if it.get("url") and not it.get("external") else ""
                        md.append("  " * depth + f"- {link(it['label'], d, it.get('url'))}{extra}{path}")
                        render(it.get("children", []), depth + 1)
                render(m["items"])
                md.append("")

            # mermaid-diagram van het hoofdmenu
            main = menus[0]
            if main["location"] == "header":
                ids = iter(range(10_000))
                lines = ["```mermaid", "graph LR", '  home(["Home"])']

                def esc(t):
                    return t.replace('"', "'")[:40]

                def graph(items, parent):
                    for it in items:
                        nid = f"n{next(ids)}"
                        lines.append(f'  {parent} --> {nid}["{esc(it["label"])}"]')
                        graph(it.get("children", []), nid)
                graph(main["items"], "home")
                lines.append("```")
                md += ["### Diagram hoofdmenu\n"] + lines + [""]

        md.append("## Paginastructuur\n")
        md.append("Op basis van het URL-pad; ★ = staat in een menu.\n")

        def render_tree(node, depth=0):
            for key in sorted(node, key=lambda k: (k != "", k)):
                n = node[key]
                d = n.get("_doc")
                label = d["title"] if d else key
                star = " ★" if d and d["file"] in in_menu else ""
                path = f"/{path_of(d)}" if d else f"/{key}"
                md.append("  " * depth + f"- {link(label, d)}{star} — `{path if path != '/' else '/'}`")
                render_tree(n["_children"], depth + 1)
        render_tree(tree)
        md.append("")

        not_in_menu = [d for d in pages if d["file"] not in in_menu]
        if menus and not_in_menu:
            md.append(f"## Pagina's niet in een menu ({len(not_in_menu)})\n")
            md += [f"- {link(d['title'], d)} — `/{path_of(d)}`" for d in sorted(not_in_menu, key=path_of)]
            md.append("")

        for typ, items in sorted(other.items()):
            md.append(f"## {typ} ({len(items)})\n")
            by_year: dict[str, list] = {}
            for d in items:
                by_year.setdefault((d.get("date") or "")[:4] or "zonder datum", []).append(d)
            for year in sorted(by_year, reverse=True):
                md.append(f"### {year} ({len(by_year[year])})\n")
                for d in sorted(by_year[year], key=lambda x: x.get("date") or "", reverse=True):
                    md.append(f"- {(d.get('date') or '')[:10]} {link(d['title'], d)}")
                md.append("")

        if missing:
            md.append(f"## In sitemap.xml maar niet geëxporteerd ({len(missing)})\n")
            md.append("Meestal archieven, producten of pagina's van plugins. Per sectie (eerste deel van het pad):\n")
            groups: dict[str, list[str]] = {}
            for u in missing:
                seg = urlparse(u).path.strip("/").split("/")[0]
                groups.setdefault(f"/{seg}" if seg else "/", []).append(u)
            md += ["| Sectie | Aantal | Voorbeelden |", "|---|---|---|"]
            for seg, us in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                examples = ", ".join(f"[{urlparse(u).path}]({u})" for u in us[:3]) + (" …" if len(us) > 3 else "")
                md.append(f"| `{seg}` | {len(us)} | {examples} |")
            md.append("\nVolledige lijst: `not_exported` in `sitemap.json`.\n")

        (self.root / "sitemap.md").write_text("\n".join(md) + "\n", encoding="utf-8")

        def tree_json(node):
            out = []
            for key in sorted(node, key=lambda k: (k != "", k)):
                n, d = node[key], node[key].get("_doc")
                out.append({k: v for k, v in {
                    "segment": key, "title": d["title"] if d else None, "file": d["file"] if d else None,
                    "url": d.get("url") if d else None, "in_menu": bool(d and d["file"] in in_menu),
                    "children": tree_json(n["_children"]) or None}.items() if v is not None})
            return out

        (self.root / "sitemap.json").write_text(json.dumps({
            "site": self.url,
            "menus": menus,
            "pages": tree_json(tree),
            "other": {t: [{"title": d["title"], "file": d["file"], "url": d.get("url"), "date": d.get("date")}
                          for d in v] for t, v in other.items()},
            "sitemap_xml_urls": xml_urls,
            "not_exported": missing,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- index & manifest -------------------------------------------------- #
    def write_index(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = [dump_frontmatter({
            "opticien_id": self.opticien_id,
            "site": self.url,
            "site_name": self.site_info.get("name"),
            "tagline": self.site_info.get("description"),
            "exported_at": now,
            "method": self.method,
            "documents": len(self.docs),
            "images": len(self.images.meta),
        })]
        lines.append(f"# {self.site_info.get('name') or self.url}\n")
        lines.append("Structuur van de site (menu's, paginaboom): zie [sitemap.md](sitemap.md).  ")
        if (self.root / "huisstijl" / "huisstijl.md").exists():
            lines.append("Huisstijl (kleuren, fonts, logo): zie [huisstijl/huisstijl.md](huisstijl/huisstijl.md).")
        by_type: dict[str, list] = {}
        for d in self.docs:
            by_type.setdefault(d["type"], []).append(d)
        for typ, docs in sorted(by_type.items()):
            lines.append(f"\n## {typ} ({len(docs)})\n")
            for d in sorted(docs, key=lambda x: (x["title"] or "").lower()):
                lines.append(f"- [{d['title']}]({d['file']})")
        if self.images.meta:
            lines.append(f"\n## Beelden ({len(self.images.meta)})\n")
            lines.append("Zie [images.md](images.md).")
        (self.root / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

        # beeldenoverzicht
        img_lines = ["# Beelden\n", "| Beeld | Alt | Titel | Caption |", "|---|---|---|---|"]
        def esc(v):
            return str(v or "").replace("|", "\\|").replace("\n", " ")
        for url, m in sorted(self.images.meta.items()):
            name = unquote(Path(urlparse(url).path).name) or url
            img_lines.append(f"| [{esc(name)}]({url}) | {esc(m.get('alt'))} | {esc(m.get('title'))} | "
                             f"{esc(m.get('caption'))} |")
        (self.root / "images.md").write_text("\n".join(img_lines) + "\n", encoding="utf-8")

        manifest = {
            "opticien_id": self.opticien_id,
            "site": self.url,
            "site_info": self.site_info,
            "exported_at": now,
            "method": self.method,
            "documents": self.docs,
            "images": [{"url": u, **m} for u, m in sorted(self.images.meta.items())],
        }
        (self.root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def check_reachable(self):
        try:
            r = self.session.get(self.url + "/", timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            raise RuntimeError(f"site niet bereikbaar: {e.__class__.__name__}") from e
        if r.status_code >= 400:
            log.warning("[%s] homepage geeft HTTP %s, toch verder", self.opticien_id, r.status_code)
        # volg redirects (http -> https, www, nieuw domein)
        final = r.url.rstrip("/")
        if final and urlparse(final).netloc != urlparse(self.url).netloc:
            log.info("[%s] redirect %s -> %s", self.opticien_id, self.url, final)
            self.url = final

    def run(self) -> dict:
        try:
            return self._run()
        except Exception as e:
            e.export_stage = self.stage  # type: ignore[attr-defined]
            e.export_url = self.url  # type: ignore[attr-defined]
            raise

    def _run(self) -> dict:
        t0 = time.time()
        self.stage = "bereikbaarheid controleren"
        self.check_reachable()
        self.root.mkdir(parents=True, exist_ok=True)
        brand_ok = None
        if self.huisstijl:
            self.stage = "huisstijl"
            try:
                brand = BrandExtractor(self.session, self.url).extract()
                BrandExtractor.write(brand, self.root / "huisstijl")
                brand_ok = True
                log.info("[%s] huisstijl: primary %s, fonts %s / %s", self.opticien_id,
                         brand["colors"].get("primary"), brand["fonts"]["body"].get("family"),
                         brand["fonts"]["headings"].get("family"))
            except Exception as e:  # noqa: BLE001 - huisstijl mag de export niet doen mislukken
                brand_ok = False
                log.warning("[%s] huisstijl niet op te halen: %s: %s", self.opticien_id, type(e).__name__, e)
        if not self.content:
            if brand_ok is False:
                raise RuntimeError("huisstijl niet op te halen")
            return {"opticien_id": self.opticien_id, "dir": self.root, "docs": 0, "images": 0}
        self.images = ImageStore(self.prefer_originals, DOC_EXTS if self.all_media else None)
        try:
            self.export_via_rest()
        except ApiUnavailable as e:
            log.warning("[%s] REST API niet bruikbaar (%s) -> sitemap-fallback", self.opticien_id, e)
            self.export_via_html()
        if not self.docs:
            raise RuntimeError("geen content gevonden")
        self.stage = "index schrijven"
        self.write_index()
        self.stage = "sitemap schrijven"
        try:
            self.write_sitemap()
        except Exception as e:  # noqa: BLE001 - sitemap mag de export niet doen mislukken
            log.warning("[%s] sitemap niet gemaakt: %s: %s", self.opticien_id, type(e).__name__, e)
        n_img = len(self.images.meta)
        log.info("[%s] klaar: %d documenten, %d beeld-URL's (%.0fs, %s)", self.opticien_id,
                 len(self.docs), n_img, time.time() - t0, self.method)
        return {"opticien_id": self.opticien_id, "dir": self.root, "docs": len(self.docs), "images": n_img}


# --------------------------------------------------------------------------- #
# Huisstijl (Themeco Pro / X / Cornerstone)
# --------------------------------------------------------------------------- #
ICON_FONTS = {"fontawesome", "fontawesomepro", "fontawesomeregular", "fontawesomelight", "fontawesomebrands",
              "dashicons", "eicons", "revicons", "star", "woocommerce"}
GENERIC_FONTS = {"inherit", "initial", "unset", "sans-serif", "serif", "monospace", "cursive", "system-ui",
                 "-apple-system", "blinkmacsystemfont"}
COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)")
CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
NAMED_COLORS = {"white": "#ffffff", "black": "#000000"}
# merkkleuren van sociale netwerken (iconen) zijn geen huisstijl van de opticien
SOCIAL_COLORS = {"#3b5998", "#1877f2", "#4267b2", "#1da1f2", "#55acee", "#e4405f", "#c13584", "#833ab4",
                 "#ff0000", "#cd201f", "#0077b5", "#0a66c2", "#25d366", "#bd081c", "#e60023", "#00b489"}


def parse_color(value: str | None) -> tuple[str, float] | None:
    """'#0AA96C' / 'rgb(10,169,108)' / 'rgba(0,0,0,.5)' -> ('#0aa96c', alpha)."""
    if not value:
        return None
    v = value.replace("!important", "").strip().lower()
    if v in NAMED_COLORS:
        return NAMED_COLORS[v], 1.0
    m = re.fullmatch(r"#([0-9a-f]{3,8})", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) not in (6, 8):
            return None
        alpha = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return f"#{h[:6]}", round(alpha, 3)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", v)
    if m:
        parts = [p for p in re.split(r"[\s,/]+", m.group(1).strip()) if p]
        if len(parts) < 3:
            return None
        try:
            rgb = [round(float(p[:-1]) * 2.55) if p.endswith("%") else round(float(p)) for p in parts[:3]]
            alpha = float(parts[3].rstrip("%")) / (100 if parts[3].endswith("%") else 1) if len(parts) > 3 else 1.0
        except ValueError:
            return None
        rgb = [max(0, min(255, c)) for c in rgb]
        return "#{:02x}{:02x}{:02x}".format(*rgb), round(alpha, 3)
    return None


def color_hex(value: str | None) -> str | None:
    """Hex-waarde van een (dekkende) kleur, anders None."""
    c = parse_color(value)
    return c[0] if c and c[1] >= 0.5 else None


def _hls(hex_color: str) -> tuple[float, float, float]:
    import colorsys
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)


def is_neutral(hex_color: str) -> bool:
    """Grijs, (bijna) wit of (bijna) zwart."""
    h, l, s = _hls(hex_color)
    return s < 0.10 or l < 0.08 or l > 0.95


def hue_distance(a: str, b: str) -> float:
    d = abs(_hls(a)[0] - _hls(b)[0])
    return min(d, 1 - d) * 360


def clean_font_family(value: str | None) -> str | None:
    """Eerste, niet-generieke font uit een font-family-lijst."""
    if not value:
        return None
    for part in value.replace("!important", "").split(","):
        name = part.strip().strip("'\"").strip()
        if name and name.lower() not in GENERIC_FONTS and name.lower() not in ICON_FONTS:
            return name
    return None


def font_stack(block: dict) -> str | None:
    """'Custom Font Family' + 'sans-serif' -> '"Custom Font Family", sans-serif'."""
    fam = block.get("family")
    if not fam:
        return None
    css = (block.get("css") or "").lower()
    generic = next((g for g in ("serif", "monospace", "cursive") if re.search(rf"(^|,)\s*{g}\s*(,|$)", css)), "sans-serif")
    return f'"{fam}", {generic}'


class BrandExtractor:
    """
    Haalt de huisstijl uit de gegenereerde CSS van een Themeco Pro/X-site
    (Theme Options -> <style id="cs-inline-css"> + Cornerstone header/footer-CSS).
    De Pro-instellingen zelf zitten niet publiek in de REST API, de CSS wel.
    """

    def __init__(self, session: requests.Session, url: str):
        self.session = session
        self.url = url.rstrip("/")

    # ---- CSS-helpers -------------------------------------------------------- #
    def _rules(self, css: str) -> list[tuple[list[str], str]]:
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        out = []
        for m in CSS_RULE_RE.finditer(css):
            sel = m.group(1).strip()
            if sel.startswith("@"):
                continue
            out.append(([s.strip() for s in sel.split(",")], m.group(2)))
        return out

    @staticmethod
    def _prop(decl: str, prop: str) -> str | None:
        m = None
        for m in re.finditer(r"(?:^|;)\s*" + re.escape(prop) + r"\s*:\s*([^;]+)", decl):
            pass
        return m.group(1).strip() if m else None

    def _get(self, rules, selector: str, prop: str) -> str | None:
        """Laatste waarde van `prop` voor een selector (cascadevolgorde)."""
        value = None
        for sels, decl in rules:
            if selector in sels:
                v = self._prop(decl, prop)
                if v:
                    value = v
        return value

    # ---- extractie ---------------------------------------------------------- #
    def extract(self) -> dict:
        r = self.session.get(self.url + "/", timeout=TIMEOUT)
        r.raise_for_status()
        html_text = r.text
        soup = BeautifulSoup(html_text, "html.parser")

        theme = {}
        m = re.search(r"/wp-content/themes/([\w-]+)/", html_text)
        if m:
            theme["name"] = m.group(1)
        for link in soup.find_all("link", href=True):
            href = link["href"]
            mm = re.search(r"/themes/[\w-]+/.*?/stacks/([\w-]+)\.css(?:\?ver=([\w.]+))?", href)
            if mm:
                theme["stack"] = mm.group(1)
                if mm.group(2):
                    theme["version"] = mm.group(2)
            if "/themes/" in href and "-child/" in href:
                theme["child_theme"] = re.search(r"/themes/([\w-]+)/", href).group(1)

        pro_style = soup.find("style", id="cs-inline-css") or soup.find("style", id="x-generated-css")
        pro_css = pro_style.get_text() if pro_style else ""
        # standaard-CSS van WordPress zelf (blokpresets e.d.) is geen huisstijl
        wp_core = re.compile(r"^(global-styles|wp-block-|classic-theme-styles|core-block-supports|wp-emoji)")
        all_inline = "\n".join(st.get_text() for st in soup.find_all("style")
                                if not wp_core.match(st.get("id") or ""))
        rules = self._rules(pro_css)
        all_rules = self._rules(all_inline)
        g = lambda sel, prop: self._get(rules, sel, prop)  # noqa: E731

        # --- fonts ---
        def font_block(sel: str, fallback_sel: str | None = None) -> dict:
            fam = g(sel, "font-family")
            if (not fam or "inherit" in fam) and fallback_sel:
                fam = g(fallback_sel, "font-family") or fam
            return {k: v for k, v in {
                "family": clean_font_family(fam),
                "css": fam.replace("!important", "").strip() if fam else None,
                "weight": (g(sel, "font-weight") or "").replace("!important", "").strip() or None,
                "style": g(sel, "font-style"),
                "letter_spacing": g(sel, "letter-spacing"),
                "text_transform": g(sel, "text-transform"),
            }.items() if v}

        body_font = font_block("body")
        if not body_font.get("family"):
            body_font["family"] = clean_font_family(self._get(rules, ":root", "--x-root-font-family"))
        heading_font = font_block("h1", "body")
        nav_font = font_block(".x-navbar .x-nav-wrap .x-nav > li > a", "body")
        root_size = self._get(rules, "html", "font-size") or self._get(rules, ":root", "--x-root-font-size")
        if root_size:
            body_font["size"] = root_size
        lh = self._get(rules, ":root", "--x-root-line-height")
        if lh:
            body_font["line_height"] = lh
        heading_sizes = {f"h{i}": self._get(rules, ":root", f"--x-h{i}-font-size") for i in range(1, 7)}

        font_faces = []
        for fm in re.finditer(r"@font-face\s*\{([^}]*)\}", all_inline):
            decl = fm.group(1)
            fam = (self._prop(decl, "font-family") or "").strip("'\" ")
            if not fam or fam.replace(" ", "").lower() in ICON_FONTS:
                continue
            urls = [urljoin(self.url + "/", u) for u in re.findall(r"url\(['\"]?([^'\")]+)['\"]?\)", decl)]
            font_faces.append({k: v for k, v in {
                "family": fam, "weight": self._prop(decl, "font-weight"),
                "style": self._prop(decl, "font-style"), "files": urls}.items() if v})

        google = []
        for link in soup.find_all("link", href=True):
            href = link["href"]
            if "fonts.googleapis.com" not in href or "http://" in unquote(href.split("family=", 1)[-1]):
                continue  # ConvertPlug laadt een vaste lijst met 'http://Bitter|...' -> geen huisstijl
            q = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
            for fam_spec in q.get("family", []):
                for spec in re.split(r"[|]", fam_spec):
                    name, _, weights = spec.partition(":")
                    google.append({"family": name.replace("+", " ").strip(),
                                   "weights": weights or None,
                                   "css_url": href if href.startswith("http") else "https:" + href})
        # dedupe
        seen, google_fonts = set(), []
        for gf in google:
            if gf["family"].lower() not in seen:
                seen.add(gf["family"].lower())
                google_fonts.append({k: v for k, v in gf.items() if v})

        def font_source(family: str | None) -> str | None:
            if not family:
                return None
            if any(f["family"].lower() == family.lower() for f in google_fonts):
                return "google-fonts"
            if any(f["family"].lower() == family.lower() for f in font_faces):
                return "self-hosted (@font-face)"
            return None

        for block in (body_font, heading_font, nav_font):
            src = font_source(block.get("family"))
            if src:
                block["source"] = src

        # --- kleuren uit de Theme Options ---
        roles = {
            "text": g("body", "color"),
            "background": g("body", "background-color"),
            "headings": g("h1", "color"),
            "link": g("a", "color"),
            "link_hover": g("a:hover", "color"),
            "button_background": g(".x-btn", "background-color"),
            "button_text": g(".x-btn", "color"),
            "button_border": g(".x-btn", "border-color"),
            "button_hover_background": g(".x-btn:hover", "background-color"),
            "button_hover_text": g(".x-btn:hover", "color"),
            "navbar_background": g(".x-navbar", "background-color"),
            "navbar_link": g(".x-navbar .desktop .x-nav > li > a", "color"),
            "accent": g(".x-highlight", "background-color") or g(".x-dropcap", "background-color"),
        }
        roles = {k: color_hex(v) for k, v in roles.items()}
        roles = {k: v for k, v in roles.items() if v}

        # --- palet: hoe vaak komt elke kleur voor in alle CSS van de homepage ---
        counts: dict[str, int] = {}
        for sels, decl in all_rules:
            for pm in re.finditer(r"(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)", decl):
                prop, val = pm.group(1), pm.group(2)
                if "color" not in prop and prop not in ("background", "border", "fill", "stroke",
                                                        "border-top", "border-bottom"):
                    continue
                if prop.startswith("--x-") or "shadow" in prop:
                    continue  # Pro-standaardvariabelen en schaduwen tellen niet mee
                for cm in COLOR_RE.finditer(val):
                    hx = color_hex(cm.group(0))
                    if hx:
                        counts[hx] = counts.get(hx, 0) + 1
        for v in roles.values():
            counts[v] = counts.get(v, 0) + 2  # expliciet ingestelde kleuren wegen zwaarder
        palette = sorted(counts.items(), key=lambda kv: -kv[1])
        accents = [c for c, _ in palette if not is_neutral(c) and c not in SOCIAL_COLORS]

        primary, primary_basis = None, None
        for key in ("button_background", "accent", "link"):
            c = roles.get(key)
            if c and not is_neutral(c):
                primary, primary_basis = c, f"theme options: {key}"
                break
        if not primary and accents:
            primary, primary_basis = accents[0], "meest gebruikte niet-neutrale kleur in de CSS"
        secondary, secondary_basis = None, None
        for c in accents:
            if counts[c] < 2 or (primary and (c == primary or hue_distance(c, primary) < 25)):
                continue
            secondary, secondary_basis = c, "volgende meest gebruikte kleur met een andere tint"
            break

        # --- logo & favicon ---
        logo = None
        header = soup.select_one("header.x-masthead, .x-masthead, header")
        img = (header.select_one(".x-brand img, img.x-img, img") if header else None) or soup.select_one(".x-brand img")
        if img:
            src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
            if src and not src.startswith("data:"):
                logo = {"url": urljoin(self.url + "/", src), "alt": img.get("alt") or None}
        icons = {}
        for link in soup.find_all("link", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            if "apple-touch-icon" in rel:
                icons.setdefault("apple_touch_icon", urljoin(self.url + "/", link["href"]))
            elif "icon" in rel:
                icons.setdefault("favicon", urljoin(self.url + "/", link["href"]))
        theme_color = soup.find("meta", attrs={"name": "theme-color"})

        return {
            "site": self.url,
            "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "gegenereerde CSS van de homepage (Themeco Pro Theme Options + Cornerstone)"
                      if pro_css else "inline CSS van de homepage (geen Themeco Pro-CSS gevonden)",
            "theme": theme or None,
            "colors": {
                "primary": primary,
                "primary_basis": primary_basis,
                "secondary": secondary,
                "secondary_basis": secondary_basis,
                "text": roles.get("text"),
                "headings": roles.get("headings"),
                "background": roles.get("background"),
                "link": roles.get("link"),
                "roles": roles,
                "palette": [{"color": c, "count": n, "neutral": is_neutral(c)} for c, n in palette[:16]],
                "meta_theme_color": theme_color.get("content") if theme_color else None,
            },
            "fonts": {
                "body": body_font,
                "headings": {**heading_font, "sizes": {k: v for k, v in heading_sizes.items() if v}},
                "navigation": nav_font,
                "google_fonts": google_fonts,
                "font_faces": font_faces,
            },
            "logo": logo,
            "icons": icons or None,
        }

    # ---- schrijven ---------------------------------------------------------- #
    @staticmethod
    def write(brand: dict, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "huisstijl.json").write_text(json.dumps(brand, ensure_ascii=False, indent=2), encoding="utf-8")

        c, f = brand["colors"], brand["fonts"]
        tokens = {
            "--color-primary": c.get("primary"),
            "--color-secondary": c.get("secondary"),
            "--color-text": c.get("text"),
            "--color-headings": c.get("headings"),
            "--color-background": c.get("background"),
            "--color-link": c.get("link"),
            "--color-button-bg": c["roles"].get("button_background"),
            "--color-button-text": c["roles"].get("button_text"),
            "--font-body": font_stack(f["body"]),
            "--font-headings": font_stack(f["headings"]),
            "--font-weight-body": f["body"].get("weight"),
            "--font-weight-headings": f["headings"].get("weight"),
            "--font-size-root": f["body"].get("size"),
            "--line-height-body": f["body"].get("line_height"),
        }
        css_lines = []
        for gf in f["google_fonts"]:
            css_lines.append(f'@import url("{gf["css_url"]}");')
            break  # één Google Fonts-URL bevat alle families van het thema
        for ff in f["font_faces"]:
            srcs = ", ".join(f'url("{u}")' for u in ff.get("files", []))
            parts = [f'font-family: "{ff["family"]}"', f"src: {srcs}"]
            if ff.get("weight"):
                parts.append(f"font-weight: {ff['weight']}")
            if ff.get("style"):
                parts.append(f"font-style: {ff['style']}")
            css_lines.append("@font-face { " + "; ".join(parts) + "; }")
        css_lines.append(":root {")
        css_lines += [f"  {k}: {v};" for k, v in tokens.items() if v]
        css_lines.append("}")
        (folder / "tokens.css").write_text("\n".join(css_lines) + "\n", encoding="utf-8")

        def row(label, value, basis=None):
            if not value:
                return None
            swatch = f"`{value}`"
            return f"| {label} | {swatch} | {basis or ''} |"

        md = [f"# Huisstijl – {brand['site']}\n",
              f"Bron: {brand['source']}  ",
              f"Thema: {', '.join(f'{k}={v}' for k, v in (brand.get('theme') or {}).items())}\n",
              "## Kleuren\n", "| Rol | Kleur | Basis |", "|---|---|---|"]
        md += [x for x in (
            row("Primary", c.get("primary"), c.get("primary_basis")),
            row("Secondary", c.get("secondary"), c.get("secondary_basis")),
            row("Tekst", c.get("text"), "body color"),
            row("Titels", c.get("headings"), "h1 color"),
            row("Achtergrond", c.get("background"), "body background"),
            row("Links", c.get("link"), "a color"),
            row("Knop achtergrond", c["roles"].get("button_background"), ".x-btn"),
            row("Knop tekst", c["roles"].get("button_text"), ".x-btn"),
        ) if x]
        md += ["", "### Palet (meest gebruikt eerst)\n", "| Kleur | Aantal | Neutraal |", "|---|---|---|"]
        md += [f"| `{p['color']}` | {p['count']} | {'ja' if p['neutral'] else ''} |" for p in c["palette"]]
        md += ["", "## Fonts\n", "| Gebruik | Font | Gewicht | Bron |", "|---|---|---|---|"]
        for label, key in (("Tekst", "body"), ("Titels", "headings"), ("Navigatie", "navigation")):
            b = f.get(key) or {}
            md.append(f"| {label} | {b.get('family') or '-'} | {b.get('weight') or ''} | {b.get('source') or ''} |")
        if f["font_faces"]:
            md += ["", "### Eigen fontbestanden\n"]
            md += [f"- **{ff['family']}** {ff.get('weight', '')} {ff.get('style', '')}: "
                   + ", ".join(ff.get("files", [])) for ff in f["font_faces"]]
        if f["google_fonts"]:
            md += ["", "### Google Fonts\n"]
            md += [f"- {gf['family']}" + (f" ({gf['weights']})" if gf.get("weights") else "") for gf in f["google_fonts"]]
        if brand.get("logo"):
            md += ["", "## Logo\n", f"![{brand['logo'].get('alt') or 'logo'}]({brand['logo']['url']})"]
        if brand.get("icons"):
            md += ["", "## Iconen\n"] + [f"- {k}: {v}" for k, v in brand["icons"].items()]
        md += ["", "Zie ook `huisstijl.json` (alle details) en `tokens.css` (CSS-variabelen)."]
        (folder / "huisstijl.md").write_text("\n".join(md) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Google Drive
# --------------------------------------------------------------------------- #
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveUploader:
    """
    Upload een lokale map naar Google Drive (idempotent: bestaande bestanden met
    dezelfde naam worden overschreven, niet gedupliceerd).

    Authenticatie (één van beide, via .env):
      GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json   (werkt enkel met een Gedeelde Drive)
      GOOGLE_OAUTH_CLIENT_FILE=client_secret.json        (log in als jezelf; token in token.json)
    """

    SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(self):
        from googleapiclient.discovery import build

        sa = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
        oauth = os.getenv("GOOGLE_OAUTH_CLIENT_FILE")
        if sa:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(sa, scopes=self.SCOPES)
        elif oauth:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            token_file = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
            creds = None
            if os.path.exists(token_file):
                creds = Credentials.from_authorized_user_file(token_file, self.SCOPES)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    creds = InstalledAppFlow.from_client_secrets_file(oauth, self.SCOPES).run_local_server(port=0)
                Path(token_file).write_text(creds.to_json())
        else:
            raise SystemExit("Zet GOOGLE_SERVICE_ACCOUNT_FILE of GOOGLE_OAUTH_CLIENT_FILE in .env")
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._children: dict[str, dict[str, dict]] = {}

    def _list(self, folder_id: str) -> dict[str, dict]:
        if folder_id in self._children:
            return self._children[folder_id]
        out, token = {}, None
        while True:
            res = self.svc.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, size, md5Checksum)",
                pageSize=1000, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            for f in res.get("files", []):
                out[f["name"]] = f
            token = res.get("nextPageToken")
            if not token:
                break
        self._children[folder_id] = out
        return out

    def ensure_folder(self, name: str, parent_id: str) -> str:
        children = self._list(parent_id)
        f = children.get(name)
        if f and f["mimeType"] == FOLDER_MIME:
            return f["id"]
        created = self.svc.files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
            fields="id, name, mimeType", supportsAllDrives=True,
        ).execute()
        children[name] = created
        self._children[created["id"]] = {}
        return created["id"]

    @staticmethod
    def _md5(path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def upload_file(self, path: Path, parent_id: str):
        from googleapiclient.http import MediaFileUpload

        children = self._list(parent_id)
        existing = children.get(path.name)
        if existing and existing.get("md5Checksum") == self._md5(path):
            return "ongewijzigd"
        mime = "text/markdown" if path.suffix == ".md" else None
        media = MediaFileUpload(str(path), mimetype=mime, resumable=path.stat().st_size > 5 * 1024 * 1024)
        if existing:
            self.svc.files().update(fileId=existing["id"], media_body=media, supportsAllDrives=True).execute()
            return "bijgewerkt"
        created = self.svc.files().create(
            body={"name": path.name, "parents": [parent_id]}, media_body=media,
            fields="id, name, mimeType, md5Checksum", supportsAllDrives=True,
        ).execute()
        children[path.name] = created
        return "nieuw"

    def upload_tree(self, local_dir: Path, parent_id: str) -> dict:
        stats = {"nieuw": 0, "bijgewerkt": 0, "ongewijzigd": 0, "fout": 0}
        folder_id = self.ensure_folder(local_dir.name, parent_id)
        for p in sorted(local_dir.iterdir()):
            if p.name.endswith(".part"):
                continue
            if p.is_dir():
                sub = self.upload_tree(p, folder_id)
                for k in stats:
                    stats[k] += sub[k]
            else:
                try:
                    stats[self.upload_file(p, folder_id)] += 1
                except Exception as e:  # noqa: BLE001
                    log.error("upload mislukt %s: %s", p, e)
                    stats["fout"] += 1
        return stats


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class RcloneError(RuntimeError):
    pass


class RcloneUploader:
    """
    Upload via rclone (geen Google Cloud Console nodig: rclone logt in met je eigen
    Google-account). Eenmalig instellen:  rclone config create gdrive drive scope=drive
    `rclone copy` stuurt enkel nieuwe of gewijzigde bestanden door.
    """

    def __init__(self, remote: str, folder_id: str | None, transfers: int = 8):
        self.remote = remote.rstrip(":")
        self.folder_id = folder_id
        self.transfers = transfers
        if not shutil.which("rclone"):
            raise SystemExit("rclone niet gevonden. Installeer met: sudo dnf install rclone")
        remotes = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True).stdout.split()
        if f"{self.remote}:" not in remotes:
            raise SystemExit(f"rclone-remote '{self.remote}' bestaat niet. Maak hem aan met:\n"
                             f"  rclone config create {self.remote} drive scope=drive")

    def upload_dir(self, local_dir: Path, dest_name: str) -> None:
        cmd = ["rclone", "copy", str(local_dir), f"{self.remote}:{dest_name}",
               "--transfers", str(self.transfers), "--checkers", str(self.transfers * 2),
               "--stats", "0", "--log-level", "ERROR"]
        if self.folder_id:
            cmd += ["--drive-root-folder-id", self.folder_id]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RcloneError(f"rclone copy faalde (exit {r.returncode}): {r.stderr.strip()[-2000:]}")


def read_sites(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(2048)
        fh.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        rows = list(csv.DictReader(fh, dialect=dialect))
    sites = []
    for r in rows:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        oid = r.get("opticien_id") or r.get("opticienid") or r.get("id")
        url = r.get("url") or r.get("site") or r.get("website")
        if not oid or not url:
            continue
        auth = (r["username"], r["app_password"]) if r.get("username") and r.get("app_password") else None
        sites.append({"opticien_id": oid, "url": url, "auth": auth})
    return sites


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exporteer WordPress-sites naar Markdown per opticienId.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sites", type=Path, help="CSV met kolommen opticien_id,url[,username,app_password]")
    src.add_argument("--site", help="Eén site-URL (samen met --opticien-id)")
    ap.add_argument("--opticien-id", help="opticienId voor --site")
    ap.add_argument("--out", type=Path, default=Path(os.getenv("EXPORT_DIR", "./export")),
                    help="Lokale uitvoermap (mag ook een gesynchroniseerde Drive-map zijn)")
    ap.add_argument("--only", nargs="*", help="Enkel deze opticienId's exporteren")
    ap.add_argument("--workers", type=int, default=3, help="Aantal sites tegelijk (standaard 3)")
    ap.add_argument("--all-media", action="store_true", help="Ook URL's van PDF's/documenten/video uit de mediabibliotheek oplijsten")
    ap.add_argument("--no-originals", action="store_true", help="Verwijs naar de verkleinde beeld-URL zoals in de content i.p.v. het origineel")
    ap.add_argument("--max-pages", type=int, default=2000, help="Max. pagina's in sitemap-fallback")
    ap.add_argument("--no-huisstijl", action="store_true", help="Huisstijl (kleuren, fonts, logo) niet ophalen")
    ap.add_argument("--huisstijl-only", action="store_true", help="Enkel de huisstijl ophalen, geen content")
    ap.add_argument("--upload", action="store_true", help="Upload naar Google Drive (DRIVE_PARENT_FOLDER_ID)")
    ap.add_argument("--drive-folder", default=os.getenv("DRIVE_PARENT_FOLDER_ID"), help="ID van de Drive-hoofdmap")
    ap.add_argument("--uploader", choices=["rclone", "api"], default=os.getenv("UPLOADER", "rclone"),
                    help="rclone (eigen Google-account, standaard) of api (Cloud Console-credentials)")
    ap.add_argument("--rclone-remote", default=os.getenv("RCLONE_REMOTE", "gdrive"),
                    help="Naam van de rclone-remote (standaard gdrive)")
    ap.add_argument("--keep-local", action="store_true",
                    help="Lokale export niet verwijderen na een geslaagde upload (rclone)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)

    if args.site:
        if not args.opticien_id:
            ap.error("--site vereist --opticien-id")
        sites = [{"opticien_id": args.opticien_id, "url": args.site, "auth": None}]
    else:
        sites = read_sites(args.sites)
    if args.only:
        wanted = {s.strip() for s in args.only}
        sites = [s for s in sites if s["opticien_id"] in wanted]
    if not sites:
        ap.error("Geen sites gevonden om te exporteren")
    if args.upload and not args.drive_folder:
        ap.error("--upload vereist --drive-folder of DRIVE_PARENT_FOLDER_ID")
    if args.no_huisstijl and args.huisstijl_only:
        ap.error("--no-huisstijl en --huisstijl-only gaan niet samen")

    args.out.mkdir(parents=True, exist_ok=True)
    log.info("%d site(s) exporteren naar %s", len(sites), args.out.resolve())

    rclone = None
    if args.upload and args.uploader == "rclone":
        rclone = RcloneUploader(args.rclone_remote, args.drive_folder)

    def process(s: dict) -> dict:
        res = SiteExporter(s["opticien_id"], s["url"], args.out, s["auth"], args.all_media,
                           not args.no_originals, args.max_pages,
                           huisstijl=not args.no_huisstijl, content=not args.huisstijl_only).run()
        if rclone:
            try:
                rclone.upload_dir(res["dir"], res["dir"].name)
            except Exception as e:
                e.export_stage = "upload naar Drive (rclone)"  # type: ignore[attr-defined]
                raise
            log.info("[%s] geüpload naar Drive", s["opticien_id"])
            if not args.keep_local:
                shutil.rmtree(res["dir"], ignore_errors=True)
        return res

    results, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(process, s): s for s in sites}
        for f in as_completed(futs):
            s = futs[f]
            try:
                results.append(f.result())
            except Exception as e:  # noqa: BLE001
                log.error("[%s] export mislukt tijdens '%s': %s: %s", s["opticien_id"],
                          getattr(e, "export_stage", "?"), type(e).__name__, e, exc_info=args.verbose)
                errors.append({
                    "opticien_id": s["opticien_id"],
                    "url": s["url"],
                    "final_url": getattr(e, "export_url", None),
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    **describe_error(e),
                })

    if args.upload and args.uploader == "api" and results:
        drive = DriveUploader()
        for r in sorted(results, key=lambda x: x["opticien_id"]):
            st = drive.upload_tree(r["dir"], args.drive_folder)
            log.info("[%s] Drive: %s", r["opticien_id"], st)

    log.info("Samenvatting: %d gelukt, %d mislukt", len(results), len(errors))
    if errors:
        (args.out / "_errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Fouten weggeschreven naar %s", args.out / "_errors.json")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
