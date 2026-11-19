"""Marketplaces such as HamroBazar: many individual sellers, often cheaper because the phone is
sold without a VAT bill (which usually means no official warranty, and possibly a grey import).

They are kept apart from the stores everywhere:
- shown in their own box on a model page, with a warning, never as the "lowest price";
- not in price drops, price history, catalog "From" prices, seller counts or price alerts.

Classified ads are noisy, so a listing is only used when it
- is marked brand new / sealed, and isn't used, a copy, an accessory, a "wanted" post, EMI, rent...,
- is about one phone (not "18 pro max 256 / 18 pro 256/512 gb"), names a model we recognise (an ad without storage is filed under the model's base storage,
  marked "storage not stated", and only if its price fits that storage), and
- costs between 70% and 105% of the cheapest real store price for that exact phone.
  (Much cheaper is almost always bait, a copy, or a deposit/EMI figure.)

Seller phone numbers are removed from listing titles; no seller names or contact details are stored.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from urllib.parse import quote, urlparse

from .extract import RawOffer
from .normalize import Variant, classify, parse_model

log = logging.getLogger(__name__)

MIN_SHARE, MAX_SHARE = 0.70, 1.05

EXCLUDE = re.compile(
    r"\b(used|second[\s-]?hand|2nd[\s-]?hand|refurb\w*|renewed|like[\s-]?new|almost[\s-]?new|open[\s-]?box"
    r"|box\s*only|only\s*box|empty\s*box|copy|clone|replica|first\s*copy|master\s*copy|dummy|duplicate|fake"
    r"|wanted|want\s*to\s*buy|buying|exchange|swap|emi|installments?|on\s*rent|rent|hire"
    r"|parts?|display|screen|motherboard|icloud|locked|bypass|battery\s*health|scratch\w*|crack\w*|broken|repair"
    r"|case|cover|glass|protector|charger|cable|adapter|airpods|watch)\b"
    r"|\b1\s*:\s*1\b|\bbh\s*\d{2}|\b\d{2}\s*(?:%|bh\b|battery)",   # battery health = used
    re.I)
NEW_HINT = re.compile(r"\b(brand[\s-]?new|sealed|seal\s*pack(?:ed)?|box[\s-]?pack(?:ed)?|unopened|unused|new)\b", re.I)
PHONE_NO = re.compile(r"(?:\+?977[\s-]?)?\b9[678]\d[\s-]?\d{3}[\s-]?\d{4}\b")


def mixed_ad(title: str) -> bool:
    """True when one ad lists several phones or sizes ("18 pro max 256 / 18 pro 256/512 gb"):
    its single price can't be matched to one of them."""
    models = set()
    for part in re.split(r"\s*(?:[/|,&+]|\band\b|\bor\b)\s*", title, flags=re.I):
        if re.search(r"i\s*-?\s*phone", part, re.I):
            parsed = parse_model(part)
        elif models and re.match(r"\d{1,2}(?:e\b|\s*(?:pro|plus|mini|max)\b)", part.strip(), re.I):  # "...and 17 Pro Max"
            parsed = parse_model("iPhone " + part.strip())
        else:
            parsed = None
        if parsed:
            models.add(parsed[0])
    sizes = {int(n) * (1024 if (u or "").lower() == "tb" else 1)
             for n, u in re.findall(r"(?<![\d.,%])\b(64|128|256|512|1|2)\s*(gb|tb)?\b(?![\d.,%])", title, re.I)
             if u or n in ("64", "128", "256", "512")}
    return len(models) > 1 or len(sizes) > 1


def is_marketplace(store: dict) -> bool:
    return (store.get("kind") or "").lower() == "marketplace"


def clean_title(title: str) -> str:
    """Listing title without phone numbers, tidied and shortened."""
    return re.sub(r"\s+", " ", PHONE_NO.sub("", title)).strip(" -|,")[:140]


def reference_prices(db, store_ids: set[str], days: int = 3) -> dict[str, dict]:
    """Cheapest recent in-stock (or stock unknown) price per exact phone, from real stores only."""
    rows = db.q(f"""SELECT p.variant_key, p.model, p.storage_gb, p.price FROM price_points p
                    JOIN (SELECT store_id, variant_key, MAX(id) AS id FROM price_points GROUP BY store_id, variant_key) l
                      ON l.id = p.id
                    WHERE p.store_id IN ({",".join("?" * len(store_ids)) or "''"})
                      AND p.last_seen >= datetime('now', ?) AND (p.in_stock IS NULL OR p.in_stock = 1)
                      AND p.storage_gb IS NOT NULL""", *sorted(store_ids), f"-{days} days")
    refs: dict[str, dict] = {}
    for r in rows:
        if r["variant_key"] not in refs or r["price"] < refs[r["variant_key"]]["price"]:
            refs[r["variant_key"]] = {"model": r["model"], "storage_gb": r["storage_gb"], "price": r["price"]}
    return refs


def search_urls(template: str, refs: dict[str, dict]) -> list[str]:
    """One search per model that real stores sell right now, newest first."""
    models = sorted({r["model"] for r in refs.values()}, key=lambda m: (-int((re.findall(r"\d+", m) or ["0"])[0]), m))
    return [template.format(query=quote(m.lower())) for m in models]


class Selected(list):
    """The kept listings; `.listings` is how many listings the pages had before filtering
    (0 means the pages couldn't be read, which is a real problem; few kept is normal)."""
    listings = 0


def select(offers: list[RawOffer], refs: dict[str, dict]) -> Selected:
    """The cheapest believable brand-new listing per exact phone."""
    best: dict[str, tuple[Variant, RawOffer]] = {}
    skipped: Counter = Counter()
    seen: set = set()
    for o in offers:
        key = (o.url.split("?")[0], o.price)
        if key in seen:
            continue
        seen.add(key)
        path = urlparse(o.url).path.lower()
        if "/accessories/" in path:
            skipped["accessory"] += 1
            continue
        # the site's condition badge beats guessing from the card; the ad's address often says "used" or "95-bh"
        condition = o.extra.get("condition")
        slug_words = re.sub(r"[-_/]+", " ", path)
        text = f"{o.title} {condition if condition is not None else o.extra.get('card_text', '')} {slug_words}"
        if EXCLUDE.search(text):
            skipped["used, copy, accessory or not for sale"] += 1
            continue
        if not NEW_HINT.search(condition if condition else text):
            skipped["not marked brand new"] += 1
            continue
        if mixed_ad(o.title):
            skipped["one ad for several phones or sizes"] += 1
            continue
        v = classify(o.title)
        if v is None:
            skipped["model unclear"] += 1
            continue
        if not v.storage_gb:
            sizes = sorted(r["storage_gb"] for r in refs.values() if r["model"] == v.model)
            if not sizes:
                skipped["no store price to compare with"] += 1
                continue
            v = Variant(model=v.model, storage_gb=sizes[0], series=v.series, condition=v.condition)
            o.title = f"{o.title} (storage not stated)"
        ref = refs.get(v.key)
        if not ref:
            skipped["no store price to compare with"] += 1
            continue
        if not MIN_SHARE * ref["price"] <= o.price <= MAX_SHARE * ref["price"]:
            skipped["price not believable"] += 1
            continue
        o.title, o.in_stock, o.old_price = clean_title(o.title), True, None
        if v.key not in best or o.price < best[v.key][1].price:
            best[v.key] = (v, o)
    if skipped:
        log.info("  marketplace: kept %d listings; skipped %s", len(best),
                 ", ".join(f"{n} {why}" for why, n in skipped.most_common()))
    out = Selected(best.values())
    out.listings = len(seen)
    return out
