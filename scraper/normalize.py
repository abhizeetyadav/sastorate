"""Turn messy store listings into comparable (model, storage) variants."""
from __future__ import annotations

import re
from dataclasses import dataclass

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Anything that mentions an iPhone but is not a phone.
ACCESSORY_RE = re.compile(
    r"\b(case|cover|protector|tempered|glass|screen\s*guard|cable|charger|adapter|adaptor|magsafe|"
    r"airtag|strap|skin|lens|holder|stand|mount|pouch|wallet|power\s*bank|battery\s*pack|earpods|"
    r"airpods|watch|ipad|macbook|imac|dock|sticker|camera\s*protector|ring|grip|bumper|sleeve|"
    r"for\s+iphone|compatible|replacement|spare|housing|display\s*combo)\b",
    re.I,
)
USED_RE = re.compile(r"\b(refurb\w*|renewed|pre[-\s]?owned|used|second[-\s]?hand|open[-\s]?box|demo\s*unit)\b", re.I)
SUFFIXES = ["pro max", "pro", "plus", "mini", "max", "ultra", "air", "fold", "duo"]
SUFFIX_RES = [(s, re.compile(s.replace(" ", r"\s*") + r"\b")) for s in SUFFIXES]
VALID_STORAGE_GB = [64, 128, 256, 512, 1024, 2048]

MIN_PRICE, MAX_PRICE = 15_000, 1_000_000  # NPR sanity window for a phone

# Pages that talk about iPhones but don't sell one: blog posts, news, comparisons, guides.
# (A GAG Nepal article about the iPhone 18 was once read as a GAG "iPhone 18" listing.)
NON_PRODUCT_URL = re.compile(
    r"/(?:blogs?|articles?|news|posts?|guides?|reviews?|tips|stories|magazine|press|tags?|authors?|faqs?|help|support)(?:/|$)"
    r"|everything-you-need-to-know|-vs-|rumou?rs?|leaks?|release-date|launch-date|expected-price|comparison|compared"
    r"|how-to|what-to-know|should-you-buy|things-to-know",
    re.I)


SEARCH_PARAMS = {"q", "s", "search", "keyword", "keywords", "query", "term", "search_query"}


def is_article_url(url: str) -> bool:
    """True for blog posts, news, comparisons and guides."""
    from urllib.parse import urlparse
    return bool(NON_PRODUCT_URL.search(urlparse(url or "").path))


def is_search_url(url: str) -> bool:
    """True for a store's search results page (/search, ?q=iphone+18 ...): it lists products but is never one.
    (A GAG Nepal search for "iphone 18" was once read as a GAG "iPhone 18" listing.)"""
    from urllib.parse import parse_qs, urlparse
    u = urlparse(url or "")
    return bool(re.search(r"/search(?:/|$|\.)", u.path, re.I)) or bool(SEARCH_PARAMS & set(k.lower() for k in parse_qs(u.query)))


def is_product_url(url: str) -> bool:
    """False for addresses that can't be a single product: articles, news, guides and search results."""
    return not is_article_url(url) and not is_search_url(url)


@dataclass(frozen=True)
class Variant:
    model: str          # "iPhone 17 Pro Max"
    storage_gb: int | None
    series: str         # "17", "Air", "16e" ...
    condition: str = "new"

    @property
    def storage_label(self) -> str:
        if not self.storage_gb:
            return "—"
        return f"{self.storage_gb // 1024}TB" if self.storage_gb >= 1024 else f"{self.storage_gb}GB"

    @property
    def key(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.model.lower()).strip("-")
        return f"{slug}|{self.storage_gb or 'na'}"


def parse_price(text: str | int | float | None) -> int | None:
    """'Rs. 1,44,900' / 'NPR 295,000.00' / 'रू १,४४,९००' / 144900 -> 144900 (None if implausible)."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        v = int(round(text))
        return v if MIN_PRICE <= v <= MAX_PRICE else None
    s = str(text).translate(DEVANAGARI_DIGITS)
    candidates = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d{1,2})?", s):
        raw = m.group().replace(",", "")
        try:
            v = int(float(raw))
        except ValueError:
            continue
        if MIN_PRICE <= v <= MAX_PRICE:
            candidates.append(v)
    return min(candidates) if candidates else None


def parse_storage(text: str) -> int | None:
    sizes = []
    for num, unit in re.findall(r"(\d{1,4})\s*(gb|tb)\b", text, re.I):
        gb = int(num) * (1024 if unit.lower() == "tb" else 1)
        if gb in VALID_STORAGE_GB:
            sizes.append(gb)
    return max(sizes) if sizes else None  # largest = storage, smaller ones are usually RAM


def parse_model(text: str) -> tuple[str, str] | None:
    """Return (display model, series) or None if this isn't recognisably an iPhone."""
    t = re.sub(r"\s+", " ", text.lower().replace("i-phone", "iphone"))
    t = re.sub(r"(iphone)(\d)", r"\1 \2", t)
    t = re.sub(r"[-–—_|/(),:+\[\]]+", " ", t)  # "Pro-Max", "17 (Pro Max)", "17 | Pro Max"
    t = re.sub(r"\s+", " ", t)
    m = re.search(r"\biphone\b\s*(.*)", t)
    if not m:
        return None
    rest = m.group(1)

    num = re.match(r"(\d{1,2})\s*(e\b)?\s*", rest)
    if num:
        n, e = num.group(1), num.group(2)
        if not 6 <= int(n) <= 30:
            return None
        rest = rest[num.end():]
        # storage, "5G" or a year can come before the name: "iPhone 17 256GB Pro Max", "iPhone 17 (2025) Pro"
        rest = re.sub(r"^(?:(?:\d{2,4}\s*(?:gb|tb)|\d\s*tb|5g|20\d\d)\s*)+", "", rest)
        if e:
            return f"iPhone {n}e", f"{n}e"
        suffix = next((s for s, rx in SUFFIX_RES if rx.match(rest)), "")
        name = f"iPhone {n}" + (f" {suffix.title()}" if suffix else "")
        return name, n

    # number-less models: "iPhone Air", "iPhone Air 2", "iPhone Fold", "iPhone Duo"
    named = re.match(r"(air|fold|duo|se)\b\s*(\d)?\b", rest)
    if named:
        word, gen = named.group(1), named.group(2)
        label = "SE" if word == "se" else word.title()
        name = f"iPhone {label}" + (f" {gen}" if gen else "")
        return name, label
    return None


def classify(title: str, include_used: bool = False) -> Variant | None:
    if ACCESSORY_RE.search(title):
        return None
    condition = "used" if USED_RE.search(title) else "new"
    if condition != "new" and not include_used:
        return None
    parsed = parse_model(title)
    if not parsed:
        return None
    model, series = parsed
    return Variant(model=model, storage_gb=parse_storage(title), series=series, condition=condition)
