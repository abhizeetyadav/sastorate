"""Build the static JSON the website reads (web/data/latest.json + history.json)."""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import logging

from .db import DB, now_iso
from .normalize import is_product_url
from .overrides import is_hidden, load_rules
from .seo import build_site, contact_email
from . import model_images, status_page

log = logging.getLogger(__name__)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


NAMED_SERIES = {"Air": 17, "Fold": 18, "Duo": 18, "SE": 0}  # where number-less lines sit


def _series_rank(series: str) -> tuple:
    m = re.match(r"(\d+)", series or "")
    n = int(m.group(1)) if m else NAMED_SERIES.get(series, 0)
    return (-n, series or "")


MODEL_ORDER = ["pro max", "pro", "air", "plus", "", "e", "mini"]


def _model_rank(model: str) -> int:
    low = model.lower()
    for i, s in enumerate(MODEL_ORDER):
        if s and low.endswith(s):
            return i
    return 4


BASE_PRICE_TOLERANCE = 0.015  # 1.5%


def _place_sizeless_offers(variants: dict, history: dict, drops: list) -> None:
    """Some stores (Evo for out-of-stock phones, one-page-per-model shops) show a
    single price without a storage size. That price is the phone's starting price,
    i.e. its smallest storage. If it matches (within 1.5%) what other stores charge
    for the smallest storage, file it there and flag it as assumed; otherwise it
    stays under "size not listed"."""
    by_model: dict[str, list] = defaultdict(list)
    for v in variants.values():
        by_model[v["model"]].append(v)
    for model, vs in by_model.items():
        sizeless = next((v for v in vs if not v["storage_gb"]), None)
        sized = [v for v in vs if v["storage_gb"]]
        if not sizeless or not sized:
            continue
        base = min(sized, key=lambda v: v["storage_gb"])
        sellers_at_base = {o["store"] for o in base["offers"]}
        ref = [o["price"] for o in base["offers"]]
        keep = []
        for o in sizeless["offers"]:
            match = any(abs(o["price"] - r) <= r * BASE_PRICE_TOLERANCE for r in ref)
            if match and o["store"] not in sellers_at_base:
                o["storage_assumed"] = True
                base["offers"].append(o)
                if o["store"] in history.get(sizeless["key"], {}):
                    history.setdefault(base["key"], {})[o["store"]] = history[sizeless["key"]].pop(o["store"])
                for d in drops:
                    if d["variant"] == sizeless["key"] and d["store"] == o["store"]:
                        d["variant"], d["storage_gb"] = base["key"], base["storage_gb"]
            else:
                keep.append(o)
        sizeless["offers"] = keep
        if not keep:
            del variants[sizeless["key"]]


LOGO_TYPES = {".svg", ".png", ".webp", ".jpg", ".jpeg"}
DETAIL_TEXT = ("warranty", "emi", "delivery", "returns", "hours", "note")


def _vslug(model: str) -> str:
    """Same model slug as variant keys ('iPhone 17 Pro Max' -> 'iphone-17-pro-max')."""
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def store_details(store: dict) -> dict | None:
    """The shop's own details from stores.yaml (`details:`), tidied and checked. Only what you entered is shown."""
    raw = store.get("details") or {}
    if not isinstance(raw, dict):
        log.warning("%s: details should be a list of fields (warranty:, emi:, ...)", store["id"])
        return None
    txt = lambda x: re.sub(r"\s+", " ", str(x or "")).strip()[:300]
    phone = lambda x: re.sub(r"[^0-9+ ,/()-]", "", str(x or "")).strip()[:60]
    out = {k: txt(raw[k]) for k in DETAIL_TEXT if txt(raw.get(k))}
    if phone(raw.get("phone")):
        out["phone"] = phone(raw["phone"])
    locs = []
    for loc in (raw.get("locations") or [])[:30]:
        if not isinstance(loc, dict) or not txt(loc.get("name")):
            continue
        item = {"name": txt(loc["name"])[:80]}
        if txt(loc.get("city")):
            item["city"] = txt(loc["city"])[:40]
        if txt(loc.get("address")):
            item["address"] = txt(loc["address"])
        if phone(loc.get("phone")):
            item["phone"] = phone(loc["phone"])
        if re.match(r"^https://", str(loc.get("map") or "")):
            item["map"] = str(loc["map"])[:300]
        locs.append(item)
    if locs:
        out["locations"] = locs
    return out or None


def store_logo(store: dict, web_dir: Path) -> str | None:
    """The store's `logo:` from stores.yaml as a site-relative path ("logos/hukut.svg"),
    or None if unset, not inside web/logos/, the wrong file type, or missing."""
    raw = str(store.get("logo") or "").strip().replace("\\", "/")
    if not raw:
        return None
    if re.match(r"^[a-z]+:", raw, re.I) or raw.startswith("//"):
        log.warning("%s: logo must be a file in web/logos/, not a web address (%s)", store["id"], raw)
        return None
    rel = raw.lstrip("./").removeprefix("web/")
    if not rel.startswith("logos/"):
        rel = "logos/" + rel
    path = (web_dir / rel).resolve()
    logos_dir = (web_dir / "logos").resolve()
    if logos_dir not in path.parents:
        log.warning("%s: logo %r must be inside web/logos/", store["id"], raw)
        return None
    if path.suffix.lower() not in LOGO_TYPES:
        log.warning("%s: logo %r should be .svg, .png, .webp or .jpg", store["id"], raw)
        return None
    if not path.is_file():
        log.warning("%s: logo file web/%s not found; showing initials instead", store["id"], rel)
        return None
    return path.relative_to(web_dir.resolve()).as_posix()


def export(db: DB, stores: list[dict], out_dir: str | Path, stale_after_days: int = 3, rules: list | None = None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - timedelta(days=stale_after_days)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()
    store_by_id = {s["id"]: s for s in stores}
    market_ids = {s["id"] for s in stores if (s.get("kind") or "").lower() == "marketplace"}
    market: dict[str, list] = defaultdict(list)

    rows = db.q("SELECT * FROM price_points ORDER BY store_id, variant_key, first_seen, id")
    by_pair: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_pair[(r["store_id"], r["variant_key"])].append(r)

    variants: dict[str, dict] = {}
    history: dict[str, dict[str, list]] = defaultdict(dict)
    drops = []

    if rules is None:  # prices hidden in scraper/config/overrides.yaml
        rules = load_rules(known_stores={s["id"] for s in stores})
    hidden = 0
    for (store_id, vkey), pts in by_pair.items():
        cur = pts[-1]
        if not is_product_url(cur["url"]):  # read from a blog post or news page by an older version
            hidden += 1
            continue
        if is_hidden(rules, store_id, cur["model"], cur["storage_gb"]):
            hidden += 1
            continue
        if store_id in market_ids:  # individual sellers: their own box, nothing else
            if cur["last_seen"] >= stale_cutoff:
                market[vkey].append({"store": store_id, "price": cur["price"], "url": cur["url"],
                                     "title": cur["title"], "last_seen": cur["last_seen"]})
            continue
        # ---- daily history (one point per day = that day's last price)
        daily: dict[str, int] = {}
        for p in pts:
            start, end = _dt(p["first_seen"]).date(), _dt(p["last_seen"]).date()
            d = start
            while d <= end:
                daily[d.isoformat()] = p["price"]
                d += timedelta(days=1)
        history[vkey][store_id] = sorted(daily.items())

        # ---- price drops in the last 14 days
        for prev, nxt in zip(pts, pts[1:]):
            if nxt["price"] < prev["price"] and _dt(nxt["first_seen"]) > now - timedelta(days=14):
                drops.append({"variant": vkey, "model": cur["model"], "storage_gb": cur["storage_gb"],
                              "store": store_id, "from": prev["price"], "to": nxt["price"],
                              "at": nxt["first_seen"]})

        if cur["last_seen"] < stale_cutoff or store_id not in store_by_id:
            continue
        prev_week = next((p["price"] for p in reversed(pts) if p["first_seen"] <= week_ago), None)
        v = variants.setdefault(vkey, {
            "key": vkey, "model": cur["model"], "series": cur["series"], "storage_gb": cur["storage_gb"],
            "offers": []})
        v["offers"].append({
            "store": store_id, "price": cur["price"], "old_price": cur["old_price"],
            "in_stock": None if cur["in_stock"] is None else bool(cur["in_stock"]),
            "url": cur["url"], "title": cur["title"], "last_seen": cur["last_seen"],
            "image": cur["image"] if "image" in cur.keys() else None,
            "week_ago": prev_week,
        })

    _place_sizeless_offers(variants, history, drops)

    vlist = []
    for v in variants.values():
        v["offers"].sort(key=lambda o: o["price"])
        prices = [o["price"] for o in v["offers"]]
        v["lowest"], v["highest"] = min(prices), max(prices)
        v["image"] = next((o["image"] for o in v["offers"] if o.get("image")), None)
        if market.get(v["key"]):
            v["market"] = sorted(market[v["key"]], key=lambda o: o["price"])
        vlist.append(v)
    vlist.sort(key=lambda v: (_series_rank(v["series"])[0], _model_rank(v["model"]), v["model"], v["storage_gb"] or 0))
    own = model_images.load(out_dir.parent)  # your own model images (scraper/config/models.yaml) beat store photos
    for v in vlist:
        if v["model"] in own:
            v["image"], v["image_credit"] = own[v["model"]]["image"], own[v["model"]]["credit"]

    # ---- colours each shop has (from the latest good scrape), and typical used prices
    colours: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in db.q("SELECT * FROM colour_stock WHERE seen_at >= ? ORDER BY colour", stale_cutoff):
        if r["store_id"] not in store_by_id or r["store_id"] in market_ids or not is_product_url(r["url"] or "")\
                or is_hidden(rules, r["store_id"], r["model"], r["storage_gb"]):
            continue
        vkey = f"{_vslug(r['model'])}|{r['storage_gb']}"
        colours[vkey][r["colour"]].append({"store": r["store_id"], "in_stock": None if r["in_stock"] is None
                                           else bool(r["in_stock"]), "price": r["price"], "url": r["url"]})
    used = {(r["model"], r["storage_gb"]): dict(r) for r in
            db.q("SELECT * FROM used_prices WHERE seen_at >= ?", stale_cutoff) if r["store_id"] in market_ids}
    for v in vlist:
        if colours.get(v["key"]):
            v["colours"] = [{"colour": c, "stores": sorted(st, key=lambda x: (x["in_stock"] is not True, x["price"] or 0))}
                            for c, st in sorted(colours[v["key"]].items(),
                                                key=lambda kv: (not any(x["in_stock"] for x in kv[1]), kv[0]))]
        u = used.get((v["model"], v["storage_gb"] or 0)) or used.get((v["model"], 0))
        if u:
            v["used"] = {"store": u["store_id"], "ads": u["ads"], "low": u["low"], "median": u["median"],
                         "high": u["high"], "all_sizes": u["storage_gb"] == 0}

    store_meta = []
    for s in stores:
        last = db.q("SELECT * FROM runs WHERE store_id=? ORDER BY id DESC LIMIT 1", s["id"])
        last_ok = db.q("SELECT finished_at FROM runs WHERE store_id=? AND status='ok' ORDER BY id DESC LIMIT 1", s["id"])
        store_meta.append({
            "id": s["id"], "name": s["name"], "url": s["url"], "authorized": bool(s.get("authorized")),
            "kind": "marketplace" if s["id"] in market_ids else "store",
            "search_url": s.get("search_url") if s["id"] in market_ids else None,
            "logo": store_logo(s, out_dir.parent),
            "details": store_details(s),
            "enabled": bool(s.get("enabled", True)),
            "offers": sum(1 for v in vlist for o in (v.get("market", []) if s["id"] in market_ids else v["offers"])
                          if o["store"] == s["id"]),
            "last_status": last[0]["status"] if last else None,
            "last_error": last[0]["error"] if last else None,
            "last_success": last_ok[0]["finished_at"] if last_ok else None,
        })

    if hidden:
        log.info("Hid %d store prices listed in overrides.yaml", hidden)
    drops.sort(key=lambda d: d["at"], reverse=True)
    latest = {"generated_at": now_iso(), "demo": False, "stores": store_meta, "variants": vlist, "drops": drops[:30],
              # where the website's "email me when the price drops" form sends sign-ups (None hides the form)
              "alerts_url": (os.getenv("ALERTS_URL") or "").rstrip("/") or None,
              # public address for "Report a wrong price" links (None hides them)
              "contact_email": contact_email()}
    (out_dir / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "history.json").write_text(json.dumps(history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    try:
        build_site(latest, out_dir.parent)  # web/<model>/index.html, sitemap.xml, robots.txt
    except Exception as e:  # noqa: BLE001 - never lose an export because of the search pages
        log.exception("search-engine pages failed: %s", e)
    try:
        status_page.build(db, stores, out_dir.parent, rules=rules)  # web/status-<STATUS_KEY>/ (only if STATUS_KEY is set)
    except Exception as e:  # noqa: BLE001
        log.exception("status page failed: %s", e)
    return latest
