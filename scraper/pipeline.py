"""Scrape one store end-to-end: listing pages -> product pages -> variants."""
from __future__ import annotations

import html as htmllib
import logging
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .extract import RawOffer, extract_offers, is_iphone_title, page_image, product_links
from .fetch import Fetcher, FetchError
from . import marketplace
from .normalize import Variant, classify, is_article_url, is_product_url, parse_colour

log = logging.getLogger(__name__)

ACCESSORY_URL = re.compile(r"case|cover|glass|protector|charger|cable|adapter|magsafe|strap|skin|lens|"
                           r"airpods|watch|ipad|macbook|refurb|used|pre-owned", re.I)


def with_page(url: str, param: str, page: int) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    q[param] = str(page)
    return urlunparse(parts._replace(query=urlencode(q)))


def _root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# ---------------------------------------------------------------- discovery
def _iphone_links_on(html: str, store_url: str) -> dict[str, int]:
    soup = BeautifulSoup(html, "lxml")
    host = urlparse(store_url).netloc.replace("www.", "")
    scored: dict[str, int] = {}
    for a in soup.select("a[href]"):
        href = urljoin(store_url, a["href"]).split("#")[0]
        if host not in urlparse(href).netloc:
            continue
        text = a.get_text(" ", strip=True).lower()
        h = href.lower()
        if "iphone" not in h and not is_iphone_title(text):
            continue
        if ACCESSORY_URL.search(urlparse(h).path) or is_article_url(href):  # search pages are fine as listings
            continue
        # category/brand/search pages first; they list many models at once
        score = 3 if re.search(r"categor|collection|brand|/apple|search|type-|iphone/?$", h) else 1
        scored[href] = max(scored.get(href, 0), score)
    return scored


def discover_start_urls(fetcher: Fetcher, store: dict, limit: int = 8) -> list[str]:
    """Find iPhone category / listing links on the store homepage.
    If the plain HTML has none (JS-rendered shop), retry once in a headless browser."""
    render = store.get("render_js", False)
    html = fetcher.get(store["url"], render_js=render)
    scored = _iphone_links_on(html, store["url"])
    if not scored and not render and store.get("auto_js", True):
        log.info("  homepage has no iPhone links in plain HTML - trying headless browser")
        try:
            scored = _iphone_links_on(fetcher.get(store["url"], render_js=True), store["url"])
        except FetchError as e:
            log.warning("  browser fallback failed: %s", e)
    urls = [u for u, _ in sorted(scored.items(), key=lambda kv: -kv[1])][:limit]
    log.info("  discovered %d iPhone links on homepage", len(urls))
    return urls


def sitemap_product_urls(fetcher: Fetcher, store_url: str, max_maps: int = 12) -> list[str]:
    """iPhone product URLs from the store's sitemap(s) (robots.txt `Sitemap:` + /sitemap.xml)."""
    root = _root(store_url)
    queue = []
    try:
        robots = fetcher.get(root + "/robots.txt")
        queue += [l.split(":", 1)[1].strip() for l in robots.splitlines() if l.lower().startswith("sitemap:")]
    except FetchError:
        pass
    queue += [root + "/sitemap.xml", root + "/sitemap_index.xml"]
    seen: set[str] = set()
    found: list[str] = []
    while queue and len(seen) < max_maps:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            xml = fetcher.get(sm)
        except FetchError:
            continue
        locs = [htmllib.unescape(u) for u in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\s\]]+)", xml)]
        if "<sitemapindex" in xml:
            # product sitemaps first; skip blog/news/image ones
            kids = [u for u in locs if not re.search(r"blog|news|post|image|video|author|tag", u, re.I)]
            kids.sort(key=lambda u: 0 if re.search(r"product|item|catalog", u, re.I) else 1)
            queue = kids + queue
            continue
        for u in locs:
            path = urlparse(u).path.lower()
            if "iphone" in path and not ACCESSORY_URL.search(path) and is_product_url(u):
                found.append(u)
    found = list(dict.fromkeys(found))
    log.info("  sitemap: %d iPhone product URLs (read %d sitemap files)", len(found), len(seen))
    return found


# ---------------------------------------------------------------- scraping
def scrape_store(fetcher: Fetcher, store: dict, defaults: dict,
                 refs: dict | None = None) -> tuple[list[tuple[Variant, RawOffer]], int]:
    """`refs` (cheapest store price per phone) is needed for marketplaces like HamroBazar."""
    cfg = {**defaults, **store}
    market = marketplace.is_marketplace(cfg)
    if market and not refs:
        log.warning("  no store prices to compare with yet; run the stores first")
        return [], 0
    render = cfg.get("render_js", False)
    selectors = cfg.get("selectors")
    hint = cfg.get("product_path")
    offers: list[RawOffer] = []
    product_urls: list[str] = []
    pages = 0

    start_urls = list(cfg.get("start_urls") or [])
    if cfg.get("search_url"):  # e.g. https://hamrobazaar.com/search/product?q={query}: one search per model
        start_urls += marketplace.search_urls(cfg["search_url"], refs or {})
    elif cfg.get("discover") or (not start_urls and not cfg.get("sitemap")):
        start_urls += discover_start_urls(fetcher, cfg)
    use_sitemap = cfg.get("sitemap") or (cfg.get("discover") and not start_urls)
    if use_sitemap and cfg.get("follow_product_pages"):
        product_urls += sitemap_product_urls(fetcher, cfg["url"])

    for start in dict.fromkeys(start_urls):
        max_pages = cfg["max_pages"] if cfg.get("page_param") else 1
        for page in range(1, max_pages + 1):
            url = with_page(start, cfg["page_param"], page) if page > 1 else start
            try:
                html = fetcher.get(url, render_js=render, wait_selector=cfg.get("wait_selector"),
                                   capture_json=cfg.get("capture_json", True))
            except FetchError as e:
                log.warning("  %s", e)
                break
            pages += 1
            found = extract_offers(html, url, selectors, hint)
            links = product_links(html, url, cfg.get("product_link_pattern")) if cfg.get("follow_product_pages") else []
            new_links = [l for l in links if l not in product_urls and l != url]
            log.info("  %s -> %d offers, %d product links", url, len(found), len(new_links))
            offers += found
            product_urls += new_links
            if page > 1 and not found and not new_links:
                break

    if cfg.get("follow_product_pages"):
        offers += _scrape_product_pages(fetcher, cfg, product_urls[: cfg["max_product_pages"]], render, hint)
        pages += min(len(product_urls), cfg["max_product_pages"])

    if market:
        picked = marketplace.select(offers, refs)
        picked.used = marketplace.used_stats(offers, refs)
        return picked, pages
    picked = Picked(pick_best(offers, cfg.get("include_used", False)))
    picked.colours = colour_stock(offers)
    return picked, pages


class Picked(list):
    """The best offer per phone, plus which colours the shop has (`.colours`)."""
    colours: list = []


def colour_stock(offers: list[RawOffer]) -> list[dict]:
    """Per model + storage + colour: is it in stock at this shop? Only for listings that name a colour."""
    rank = {True: 2, False: 1, None: 0}  # in stock beats out of stock beats unknown
    acc: dict[tuple, dict] = {}
    for o in offers:
        v = classify(o.title)
        colour = parse_colour(o.title)
        if v is None or not v.storage_gb or not colour or not is_product_url(o.url):
            continue
        k = (v.model, v.storage_gb, colour)
        cur = acc.get(k)
        if (cur is None or rank[o.in_stock] > rank[cur["in_stock"]]
                or (o.in_stock == cur["in_stock"] and o.price < cur["price"])):
            acc[k] = {"model": v.model, "storage_gb": v.storage_gb, "colour": colour,
                      "in_stock": o.in_stock, "price": o.price, "url": o.url}
    return list(acc.values())


def _scrape_product_pages(fetcher: Fetcher, cfg: dict, urls: list[str], render: bool, hint) -> list[RawOffer]:
    """Fetch product pages. If plain HTML yields nothing, try the headless browser on
    the first couple of pages and, if that works, use it for the rest of the store."""
    out: list[RawOffer] = []
    js_tries = 0 if (render or not cfg.get("auto_js", True)) else 2
    empty = 0
    for url in urls:
        try:
            html = fetcher.get(url, render_js=render, wait_selector=cfg.get("wait_selector"),
                                   capture_json=cfg.get("capture_json", True))
        except FetchError as e:
            log.warning("  %s", e)
            continue
        found = extract_offers(html, url, cfg.get("product_selectors"), hint)
        if not found and js_tries:
            js_tries -= 1
            try:
                found = extract_offers(fetcher.get(url, render_js=True), url, cfg.get("product_selectors"), hint)
            except FetchError as e:
                log.warning("  browser fallback failed: %s", e)
                js_tries = 0
            if found:
                log.info("  product pages need JavaScript - using headless browser for the rest of %s", cfg["name"])
                render, js_tries = True, 0
        if found and not all(o.image for o in found):
            img = page_image(BeautifulSoup(html, "lxml"), url)
            for o in found:
                o.image = o.image or img
        empty += not found
        log.info("  product %s -> %d offers", url, len(found))
        out += found
    if urls and empty == len(urls):
        log.warning("  none of %d product pages had a readable price - run `python -m scraper probe %s --js`",
                    len(urls), urls[0])
    return out


def _page_slug(url: str) -> str:
    return url.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1].lower()


def _fill_stock_from_same_page(offers: list[RawOffer]) -> None:
    """A list page often shows a product without stock info, while its product page
    says "Out Of Stock". Both point at the same page (sometimes by different paths,
    e.g. /apple/iphone/iphoneAir and /iphoneAir), so share the known status."""
    known: dict[tuple, set] = {}
    for o in offers:
        if o.in_stock is not None:
            known.setdefault((_page_slug(o.url), o.price), set()).add(o.in_stock)
    for o in offers:
        if o.in_stock is None:
            vals = known.get((_page_slug(o.url), o.price))
            if vals and len(vals) == 1:
                o.in_stock = next(iter(vals))


def pick_best(offers: list[RawOffer], include_used: bool) -> list[tuple[Variant, RawOffer]]:
    """Classify every raw offer and keep the cheapest in-stock offer per variant.

    Listing pages often show "from" prices without storage; product pages give
    exact storages. A variant without storage is dropped when the same model
    is also seen with explicit storages.
    """
    _fill_stock_from_same_page(offers)
    best: dict[str, tuple[Variant, RawOffer]] = {}
    for o in offers:
        v = classify(o.title, include_used)
        if v is None:
            continue
        cur = best.get(v.key)
        rank = (o.in_stock is False, o.price)
        if cur is None or rank < (cur[1].in_stock is False, cur[1].price):
            best[v.key] = (v, o)
    models_with_storage = {v.model for v, _ in best.values() if v.storage_gb}
    return [(v, o) for v, o in best.values() if v.storage_gb or v.model not in models_with_storage]
