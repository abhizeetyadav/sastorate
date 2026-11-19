"""Pull product offers out of a store page.

Stores in Nepal run on very different stacks (OpenCart, WooCommerce, Shopify,
Next.js, custom Laravel...). Instead of one brittle parser per site, every page
goes through a stack of extractors and the results are merged:

  1. JSON-LD  (schema.org Product / ItemList / ProductGroup)  - most reliable
  2. Embedded app state (Next.js __NEXT_DATA__, RSC flight data, Nuxt, inline JSON)
  3. CSS selectors from stores.yaml                             - per-store tuning
  4. Heuristic card detection (iPhone link + nearby "Rs./NPR" price)

Run `python -m scraper probe <url>` to see which extractor finds what.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .normalize import is_product_url, parse_price

log = logging.getLogger(__name__)

CURRENCY_RE = re.compile(r"(?:rs\.?|npr|रू|रु)\s*[\d०-९][\d०-९,]*", re.I)
NAME_KEYS = ("name", "title", "product_name", "productName", "display_name", "label")
PRICE_KEYS = ("sale_price", "salePrice", "selling_price", "sellingPrice", "special_price", "specialPrice",
              "offer_price", "offerPrice", "final_price", "finalPrice", "discounted_price", "discountedPrice",
              "price", "current_price", "currentPrice", "amount")
OLD_PRICE_KEYS = ("regular_price", "regularPrice", "mrp", "original_price", "originalPrice", "old_price",
                  "oldPrice", "compare_at_price", "compareAtPrice", "market_price", "marketPrice", "list_price")
URL_KEYS = ("url", "href", "permalink", "link", "product_url", "productUrl", "slug", "handle")
VARIANT_KEYS = ("variants", "variations", "skus", "product_variants", "productVariants", "options", "hasVariant")
# how a variant names itself, e.g. Oliz: {"option_name": "Sky Blue/256GB"}
VARIANT_NAME_KEYS = ("option_name", "optionName", "variant_name", "variantName", "variant_title", "variantTitle",
                     "options_text", "attributes_text")
IMAGE_KEYS = ("image", "image_url", "imageUrl", "thumbnail", "thumbnail_url", "featured_image", "featuredImage",
              "main_image", "mainImage", "images", "photo", "picture")
STOCK_KEYS = ("in_stock", "inStock", "is_in_stock", "available", "availability", "stock_status", "stock", "quantity",
              "inventory", "inventoryQuantity", "inventory_quantity", "stockQuantity", "stock_quantity", "availableQuantity")


@dataclass
class RawOffer:
    title: str
    price: int
    url: str
    old_price: int | None = None
    in_stock: bool | None = None
    method: str = ""
    extra: dict = field(default_factory=dict)
    image: str | None = None

    def dedupe_key(self) -> tuple:
        return (self.url.split("?")[0].rstrip("/"), re.sub(r"\s+", " ", self.title.lower()), self.price)


# ============================================================== entry point
def extract_offers(html: str, page_url: str, selectors: dict | None = None,
                   product_path_hint: str | None = None) -> list[RawOffer]:
    soup = BeautifulSoup(html, "lxml")
    if is_article_page(soup):
        log.info("  skipped %s: it's an article, not a product page", page_url)
        return []
    found: list[RawOffer] = []
    for fn in (from_jsonld, from_embedded_json):
        try:
            found += fn(soup, page_url, product_path_hint)
        except Exception as e:  # noqa: BLE001 - one broken extractor must not kill the page
            log.debug("%s failed on %s: %s", fn.__name__, page_url, e)
    if selectors:
        found += from_selectors(soup, page_url, selectors)
    if not found:  # heuristics are noisy: only when nothing structured matched
        found += from_heuristics(soup, page_url)
        found += from_product_page(soup, page_url)
    # Offers for this very page with no stock info: read the stock text shown
    # next to the product title and price ("Out Of Stock", "Add to Bag"...).
    here = page_url.split("?")[0].rstrip("/")
    unknown = [o for o in found if o.in_stock is None and o.url.split("?")[0].rstrip("/") == here]
    if unknown:
        stock = page_stock(soup)
        for o in unknown:
            o.in_stock = stock
    return dedupe(found)


ARTICLE_TYPES = re.compile(r'"@type"\s*:\s*\[?\s*"(?:Article|BlogPosting|NewsArticle|Report|TechArticle)"')


def is_article_page(soup: BeautifulSoup) -> bool:
    """Blog posts and news pages mention prices too ("expected to cost Rs ...") but sell nothing."""
    ld = " ".join(s.string or "" for s in soup.find_all("script", type="application/ld+json"))
    if '"Product"' in ld or "'Product'" in ld:
        return False
    og = soup.find("meta", attrs={"property": "og:type"})
    if og and (og.get("content") or "").strip().lower() in ("article", "blog", "news"):
        return True
    return bool(ARTICLE_TYPES.search(ld))


def page_stock(soup: BeautifulSoup) -> bool | None:
    """Stock status printed in the product's own info block (title + price),
    not in "similar items" further down the page."""
    h1 = soup.find("h1")
    if h1 is None or not is_iphone_title(_text(h1)):
        return None
    node = h1
    for _ in range(6):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            return None
        txt = _text(node)
        if CURRENCY_RE.search(txt):  # smallest block holding the title and a price
            if OUT_OF_STOCK_RE.search(txt):
                return False
            if re.search(r"add to (cart|bag|basket)|buy now|in stock", txt, re.I):
                return True
            return None
    return None


def dedupe(offers: Iterable[RawOffer]) -> list[RawOffer]:
    seen, out = set(), []
    for o in offers:
        k = o.dedupe_key()
        if k not in seen:
            seen.add(k)
            out.append(o)
    return out


def is_iphone_title(t: str | None) -> bool:
    return bool(t) and "iphone" in t.lower().replace("i-phone", "iphone").replace(" ", "")


def image_url(v, base: str) -> str | None:
    """Normalise the many shapes an image field takes (str, list, {url: ...})."""
    for _ in range(3):
        if isinstance(v, list):
            v = v[0] if v else None
        elif isinstance(v, dict):
            v = v.get("url") or v.get("src") or v.get("contentUrl") or v.get("original") or v.get("large")
        else:
            break
    if not isinstance(v, str) or not v.strip() or v.startswith("data:"):
        return None
    return urljoin(base, v.strip())


def page_image(soup: BeautifulSoup, base: str) -> str | None:
    for sel in ('meta[property="og:image"]', 'meta[name="twitter:image"]', 'link[rel="image_src"]'):
        el = soup.select_one(sel)
        if el and (el.get("content") or el.get("href")):
            return urljoin(base, el.get("content") or el.get("href"))
    return None


# ============================================================== 1. JSON-LD
def _iter_jsonld(soup: BeautifulSoup) -> Iterable[dict]:
    for s in soup.find_all("script", type=lambda v: v and "ld+json" in v):
        raw = s.string or s.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            try:  # some themes emit trailing commas / multiple objects
                data = json.loads("[" + re.sub(r",\s*([}\]])", r"\1", raw.strip()).rstrip(";") + "]")
            except json.JSONDecodeError:
                continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                for k in ("@graph", "itemListElement", "item", "hasVariant", "mainEntity"):
                    if k in node:
                        stack.append(node[k])


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return {x.lower() for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)}


def from_jsonld(soup: BeautifulSoup, page_url: str, _hint=None) -> list[RawOffer]:
    out: list[RawOffer] = []
    for node in _iter_jsonld(soup):
        if not _types(node) & {"product", "productgroup"}:
            continue
        name = node.get("name") or ""
        if not is_iphone_title(name):
            continue
        url = urljoin(page_url, node.get("url") or page_url)
        offers = node.get("offers") or []
        offers = offers if isinstance(offers, list) else [offers]
        if not offers:
            continue
        for off in offers:
            if not isinstance(off, dict):
                continue
            price = parse_price(off.get("price") or off.get("lowPrice")
                                or (off.get("priceSpecification") or {}).get("price"))
            if not price:
                continue
            title = name
            item = off.get("itemOffered") if isinstance(off.get("itemOffered"), dict) else {}
            vname = off.get("name") or item.get("name") or off.get("sku") or ""
            if vname and vname.lower() not in name.lower():
                title = f"{name} {vname}"
            avail = str(off.get("availability", "")).lower()
            out.append(RawOffer(
                title=title, price=price, url=urljoin(page_url, off.get("url") or url),
                in_stock=None if not avail else ("instock" in avail or "limitedavailability" in avail),
                method="jsonld", image=image_url(node.get("image"), page_url)))
            if off.get("@type", "").lower() == "aggregateoffer" and off.get("highPrice"):
                out[-1].extra["high_price"] = parse_price(off.get("highPrice"))
    return out


# ============================================================== 2. embedded JSON
def _embedded_blobs(soup: BeautifulSoup) -> Iterable[Any]:
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        yield json.loads(nd.string)
    nuxt = soup.find("script", id="__NUXT_DATA__")
    if nuxt and nuxt.string:
        yield json.loads(nuxt.string)
    # Next.js app router (RSC flight data): self.__next_f.push([1,"...json-ish..."])
    flight = []
    for s in soup.find_all("script"):
        txt = s.string or ""
        for m in re.finditer(r'self\.__next_f\.push\(\[\d+,\s*("(?:[^"\\]|\\.)*")\]\)', txt):
            try:
                flight.append(json.loads(m.group(1)))
            except json.JSONDecodeError:
                pass
        if s.get("type") == "application/json" and txt.strip().startswith(("{", "[")):
            try:
                yield json.loads(txt)
            except json.JSONDecodeError:
                pass
    if flight:
        yield from _objects_around_iphone("".join(flight))


def _objects_around_iphone(text: str) -> Iterable[dict]:
    """Recover JSON objects embedded in an RSC/JS stream that mention an iPhone name."""
    dec = json.JSONDecoder()
    done: set[int] = set()
    for m in re.finditer(r'"(?:name|title|product_name)"\s*:\s*"[^"]*i-?phone', text, re.I):
        pos = m.start()
        for _ in range(6):  # try a few enclosing "{" going outwards
            pos = text.rfind("{", 0, pos)
            if pos < 0 or pos in done:
                break
            try:
                obj, _ = dec.raw_decode(text, pos)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                done.add(pos)
                yield obj
                break


def _first(d: dict, keys: Iterable[str]):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _json_price(v) -> int | None:
    """Prices in app data are sometimes whole rupees and sometimes paisa (x100),
    e.g. 24249900 = Rs 2,42,499. A raw number too large to be a phone's rupee
    price but valid after dividing by 100 is read as paisa."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return parse_price(v) or (parse_price(v / 100) if v % 100 == 0 else None)
    return parse_price(v)


def _price_of(d: dict) -> tuple[int | None, int | None]:
    price = None
    for k in PRICE_KEYS:
        v = d.get(k)
        if isinstance(v, dict):  # {"amount": 123} / {"value": ...}
            v = v.get("amount") or v.get("value") or v.get("formatted")
        p = _json_price(v) if v not in (None, "") else None
        if p:
            price = p
            break
    old = None
    for k in OLD_PRICE_KEYS:
        o = _json_price(d.get(k)) if d.get(k) not in (None, "") else None
        if o and (not price or o > price):
            old = o
            break
    return price, old


def _stock_of(d: dict) -> bool | None:
    v = _first(d, STOCK_KEYS)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    s = str(v).lower()
    if any(x in s for x in ("out", "unavailable", "sold")):
        return False
    return any(x in s for x in ("in", "available", "true", "yes"))


def _url_of(d: dict, page_url: str, hint: str | None) -> str:
    v = _first(d, URL_KEYS)
    if not isinstance(v, str):
        return page_url
    if v.startswith(("http", "/")):
        return urljoin(page_url, v)
    # bare slug -> use store's product path template, e.g. "/product-detail/{slug}"
    return urljoin(page_url, (hint or "/{slug}").format(slug=v))


def from_embedded_json(soup: BeautifulSoup, page_url: str, hint: str | None = None) -> list[RawOffer]:
    out: list[RawOffer] = []
    for blob in _embedded_blobs(soup):
        stack = [blob]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            name = _first(node, NAME_KEYS)
            if isinstance(name, str) and is_iphone_title(name):
                price, old = _price_of(node)
                variants = _first(node, VARIANT_KEYS)
                url = _url_of(node, page_url, hint)
                emitted = False
                if isinstance(variants, list):
                    for v in variants:
                        if not isinstance(v, dict):
                            continue
                        vp, vo = _price_of(v)
                        if not vp:
                            continue
                        vname = _first(v, VARIANT_NAME_KEYS) or _first(v, NAME_KEYS) or " ".join(
                            str(x) for x in (v.get("storage"), v.get("size"), v.get("color"), v.get("sku")) if x)
                        title = name if not vname or str(vname).lower() in name.lower() else f"{name} {vname}"
                        out.append(RawOffer(title, vp, url, vo, _stock_of(v), "embedded-variant",
                                            image=image_url(_first(v, IMAGE_KEYS) or _first(node, IMAGE_KEYS), page_url)))
                        emitted = True
                if not emitted and price:
                    out.append(RawOffer(name, price, url, old, _stock_of(node), "embedded",
                                        image=image_url(_first(node, IMAGE_KEYS), page_url)))
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out


def _img_src(img: Tag | None, base: str) -> str | None:
    if img is None:
        return None
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        v = img.get(attr)
        if v and not v.startswith("data:"):
            return urljoin(base, v)
    srcset = img.get("srcset") or img.get("data-srcset")
    return urljoin(base, srcset.split(",")[0].split()[0]) if srcset else None


# ============================================================== 3. CSS selectors
def _text(el: Tag | None) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""


OUT_OF_STOCK_RE = re.compile(r"out\s*of\s*stock|sold\s*out|unavailable|not\s*available|pre-?order", re.I)


def from_selectors(soup: BeautifulSoup, page_url: str, sel: dict) -> list[RawOffer]:
    out: list[RawOffer] = []
    cards = soup.select(sel["card"]) if sel.get("card") else []
    for card in cards:
        name_el = card.select_one(sel.get("name", "a"))
        title = _text(name_el) or (name_el.get("title", "") if name_el else "")
        if not is_iphone_title(title):
            continue
        if sel.get("link") == "page":  # product page: the offer's link is the page itself
            url = page_url
        else:
            link_el = card.select_one(sel.get("link", "a[href]"))
            url = urljoin(page_url, link_el["href"]) if link_el and link_el.has_attr("href") else page_url
        price_text = _text(card.select_one(sel["price"])) if sel.get("price") else _text(card)
        old_el = card.select_one(sel["old_price"]) if sel.get("old_price") else None
        if old_el is not None and old_el.has_attr("hidden"):  # theme hides it when there is no discount
            old_el = None
        old = parse_price(_text(old_el)) if old_el else None
        if old_el:  # remove the struck-through price before reading the current one
            price_text = price_text.replace(_text(old_el), " ")
        price = parse_price(price_text)
        if not price:
            continue
        stock = None
        if sel.get("out_of_stock"):  # element that only exists when sold out
            stock = card.select_one(sel["out_of_stock"]) is None
        if sel.get("stock"):  # element whose text says "In Stock" / "Out of Stock"
            st = card.select_one(sel["stock"])
            if st is not None:
                stock = not OUT_OF_STOCK_RE.search(_text(st))
        img = card.select_one(sel["image"]) if sel.get("image") else card.select_one("img")
        out.append(RawOffer(title, price, url, old if old and old > price else None, stock, "selectors",
                            image=_img_src(img, page_url)))
    return out


# ============================================================== 4. heuristics
OLD_PRICE_CSS = "del, s, strike, .old-price, .price-old, .regular-price, .line-through, [class*=strike], [class*=old]"


def _card_for(a: Tag, max_up: int = 6) -> Tag | None:
    node = a
    for _ in range(max_up):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            return None
        txt = node.get_text(" ", strip=True)
        if CURRENCY_RE.search(txt):
            # too big = we climbed into the whole grid
            if len({x["href"] for x in node.select("a[href]") if is_iphone_title(_text(x) or x.get("title"))}) > 2:
                return None
            return node
    return None


def from_heuristics(soup: BeautifulSoup, page_url: str) -> list[RawOffer]:
    out: list[RawOffer] = []
    for a in soup.select("a[href]"):
        title = _text(a) or a.get("title", "") or (a.img.get("alt", "") if a.img else "")
        if not is_iphone_title(title) or len(title) > 160:
            continue
        card = _card_for(a)
        if card is None:
            continue
        old = None
        card_txt = _text(card)
        for o in card.select(OLD_PRICE_CSS):
            old = parse_price(_text(o)) or old
            card_txt = card_txt.replace(_text(o), " ")
        prices = [parse_price(m.group()) for m in CURRENCY_RE.finditer(card_txt)]
        prices = [p for p in prices if p]
        if not prices:
            continue
        price = min(prices)
        out.append(RawOffer(title, price, urljoin(page_url, a["href"]),
                            old if old and old > price else None, None, "heuristic",
                            image=_img_src(card.select_one("img"), page_url)))
    return out


def from_product_page(soup: BeautifulSoup, page_url: str) -> list[RawOffer]:
    """Last resort for a single product page: <h1> + first price after it."""
    h1 = soup.find("h1")
    title = _text(h1)
    if not is_iphone_title(title):
        return []
    scope = h1.find_parent(["main", "section", "article"]) or soup.body or soup
    m = CURRENCY_RE.search(_text(scope))
    price = parse_price(m.group()) if m else None
    if not price:
        return []
    stock = None
    if re.search(r"out\s+of\s+stock|sold\s+out", _text(scope), re.I):
        stock = False
    return [RawOffer(title, price, page_url, None, stock, "product-page", image=page_image(soup, page_url))]


# ============================================================== link discovery
def product_links(html: str, page_url: str, pattern: str | None = None) -> list[str]:
    """iPhone product URLs on a listing page (used when follow_product_pages is on)."""
    soup = BeautifulSoup(html, "lxml")
    rx = re.compile(pattern) if pattern else None
    links = []
    for a in soup.select("a[href]"):
        href = urljoin(page_url, a["href"]).split("#")[0]
        text = _text(a) or a.get("title", "") or href
        if rx and not rx.search(href):
            continue
        if not is_product_url(href):
            continue
        if not rx and not (is_iphone_title(text) or "iphone" in href.lower()):
            continue
        if href not in links:
            links.append(href)
    return links
