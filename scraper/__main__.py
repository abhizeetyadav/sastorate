"""CLI.

  python -m scraper run                 scrape every enabled store, then export JSON
  python -m scraper run --store evostore --no-proxy
  python -m scraper probe <url> [--store id] [--js]   debug what a page yields
  python -m scraper proxies             check Webshare credentials / exit IPs
  python -m scraper export              rebuild web/data/*.json from the DB
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import requests
import yaml

from .alerts import push_prices
from . import health, marketplace
from .db import DB
from .export import export
from .extract import extract_offers, product_links
from .fetch import Fetcher, FetchError
from .normalize import classify
from .pipeline import scrape_store
from .proxies import ProxyPool, check_pool

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(__file__).resolve().parent / "config" / "stores.yaml"
DB_PATH = Path(os.getenv("TRACKER_DB", ROOT / "data" / "prices.db"))
WEB_DATA = Path(os.getenv("TRACKER_WEB_DATA", ROOT / "web" / "data"))

log = logging.getLogger("scraper")


def load_env(path: Path = ROOT / ".env") -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config() -> tuple[dict, list[dict]]:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return cfg.get("defaults", {}), cfg["stores"]


def make_fetcher(defaults: dict, no_proxy: bool) -> Fetcher:
    if no_proxy:
        os.environ["WEBSHARE_MODE"] = "none"
    pool = ProxyPool.from_env()
    return Fetcher(pool, timeout=defaults.get("timeout", 30), retries=defaults.get("retries", 4),
                   delay=tuple(defaults.get("delay_seconds", [2, 5])),
                   respect_robots=defaults.get("respect_robots", True))


def cmd_run(args) -> int:
    defaults, stores = load_config()
    targets = [s for s in stores if (s["id"] in args.store if args.store else s.get("enabled", True))]
    targets.sort(key=marketplace.is_marketplace)  # marketplaces last: they compare with fresh store prices
    shop_ids = {s["id"] for s in stores if not marketplace.is_marketplace(s)}
    if not targets:
        log.error("No stores selected")
        return 1
    db = DB(DB_PATH)
    fetcher = make_fetcher(defaults, args.no_proxy)
    failures = 0
    try:
        for store in targets:
            log.info("== %s", store["name"])
            run_id = db.start_run(store["id"])
            try:
                refs = marketplace.reference_prices(db, shop_ids) if marketplace.is_marketplace(store) else None
                results, pages = scrape_store(fetcher, store, defaults, refs)
                changed = 0
                if not args.dry_run:
                    for variant, offer in results:
                        changed += db.record(store["id"], variant, offer.title, offer.url, offer.price,
                                             offer.old_price, offer.in_stock, image=offer.image)
                    db.commit()
                for variant, offer in sorted(results, key=lambda x: x[0].key):
                    log.info("   %-28s %-6s Rs %9s  [%s]", variant.model, variant.storage_label,
                             f"{offer.price:,}", offer.method)
                status = "ok" if results or getattr(results, "listings", 0) else "empty"
                failures += status != "ok"
                db.finish_run(run_id, status, len(results), pages)
                log.info("   -> %d variants, %d price changes, %d pages", len(results), changed, pages)
            except FetchError as e:  # site unreachable / blocked: no traceback needed
                failures += 1
                log.error("   store skipped: %s", e)
                db.finish_run(run_id, "error", error=str(e))
            except Exception as e:  # noqa: BLE001 - keep going with the other stores
                failures += 1
                log.exception("   store failed: %s", e)
                db.finish_run(run_id, "error", error=str(e))
    finally:
        fetcher.close()
    if not args.dry_run:
        latest = export(db, stores, WEB_DATA, defaults.get("stale_after_days", 3))
        log.info("Exported %d variants to %s", len(latest["variants"]), WEB_DATA)
        push_prices(latest)  # price-drop emails (skipped unless ALERTS_URL / ALERTS_TOKEN are set)
        print("\n" + health.report_text(health.check(db, stores)))
    return 0 if failures < len(targets) else 2


def cmd_probe(args) -> int:
    defaults, stores = load_config()
    store = next((s for s in stores if s["id"] == args.store), {}) if args.store else {}
    fetcher = make_fetcher({**defaults, "respect_robots": False}, args.no_proxy)
    try:
        html = fetcher.get(args.url, render_js=args.js or store.get("render_js", False),
                           capture_json=store.get("capture_json", True))
    except FetchError as e:
        print(f"\nCould not load the page: {e}")
        return 1
    finally:
        fetcher.close()
    if args.save:
        Path(args.save).write_text(html, encoding="utf-8")
        print(f"saved HTML to {args.save}")
    offers = extract_offers(html, args.url, store.get("selectors"), store.get("product_path"))
    print(f"\n{len(html):,} bytes, {len(offers)} raw offers\n")
    for o in offers:
        v = classify(o.title)
        tag = f"{v.model} / {v.storage_label}" if v else "SKIPPED (not a new iPhone)"
        print(f"[{o.method:16}] Rs {o.price:>9,}  {o.title[:60]:60}  -> {tag}")
    links = product_links(html, args.url, store.get("product_link_pattern"))
    print(f"\n{len(links)} candidate product links:")
    for l in links[:25]:
        print("  ", l)
    return 0


def cmd_image(args) -> int:
    """Use your own image for a model: python -m scraper image "iPhone 17 Pro" <link or file> --credit "..." """
    from . import model_images as mi
    web = ROOT / "web"
    if args.list or not args.model:
        entries = mi._read()
        if not entries:
            print("No model images yet. Add one with:\n"
                  '  python -m scraper image "iPhone 17 Pro" <image link or file> --credit "Image: Apple"')
        for model, e in entries.items():
            e = e or {}
            ok = (web / str(e.get("image", ""))).is_file()
            print(f"  {model:<20} web/{e.get('image')}  {e.get('credit') or ''}{'' if ok else '   <- FILE MISSING'}")
        return 0
    if args.remove:
        model = mi.remove_image(args.model, web)
        print(f"Removed the image for {model}; it uses the store's photo again." if model
              else f"No image set for '{args.model}'.")
        return 0 if model else 1
    if not args.source:
        print('Give an image link or file, e.g.  python -m scraper image "iPhone 17 Pro" https://.../photo.png')
        return 1
    try:
        model, rel = mi.import_image(args.model, args.source, args.credit, web, force=args.force)
    except mi.ImageProblem as e:
        print("Not saved: this image would look wrong on the site:")
        for p in e.problems:
            print(f"  - {p}")
        print("Use a different image, or add --force to use it anyway.")
        return 1
    except (ValueError, requests.RequestException) as e:
        print(f"Couldn't use that image: {e}")
        return 1
    print(f"Saved web/{rel} for {model}" + (f" (credit: {args.credit})" if args.credit else ""))
    print("Run  python -m scraper export  to see it locally, then  python scripts/deploy.py  to publish.")
    return 0


def cmd_inspect(args) -> int:
    """Where every price for one model came from: page title, address, when it was last seen."""
    from .normalize import is_product_url, parse_model
    from .overrides import is_hidden, load_rules
    if not DB_PATH.exists():
        print(f"No database yet at {DB_PATH}. Run: python -m scraper run")
        return 1
    parsed = parse_model(args.model if "iphone" in args.model.lower() else f"iPhone {args.model}")
    if not parsed:
        print(f"'{args.model}' isn't an iPhone model I recognise. Try e.g.  python -m scraper inspect \"iPhone 17\"")
        return 1
    model = parsed[0]
    db = DB(DB_PATH)
    rows = db.q("""SELECT p.* FROM price_points p
                   JOIN (SELECT store_id, variant_key, MAX(id) AS id FROM price_points
                         WHERE model = ? GROUP BY store_id, variant_key) l ON l.id = p.id
                   ORDER BY p.storage_gb, p.price""", model)
    if not rows:
        print(f"No prices recorded for {model}.")
        return 0
    _, stores = load_config()
    rules = load_rules(known_stores={st["id"] for st in stores})
    print(f"{model}: latest price from each store and storage size\n")
    for r in rows:
        size = "?" if not r["storage_gb"] else (f"{r['storage_gb'] // 1024} TB" if r["storage_gb"] >= 1024 else f"{r['storage_gb']} GB")
        stock = {1: "in stock", 0: "out of stock"}.get(r["in_stock"], "stock not shown")
        note = ("  <- HIDDEN: not a product page (article, news or search results)" if not is_product_url(r["url"])
                else "  <- HIDDEN by overrides.yaml" if is_hidden(rules, r["store_id"], r["model"], r["storage_gb"]) else "")
        print(f"  {r['store_id']:<12} {size:>6}  Rs {r['price']:>9,}  {stock:<15} last seen {r['last_seen'][:16].replace('T', ' ')}{note}")
        print(f"               title: {r['title']}")
        print(f"               page:  {r['url']}\n")
    print("A wrong price? Open the page above. If the store lists something else there, hide it with")
    print("scraper/config/overrides.yaml, or send me the page so the scraper can be fixed.")
    return 0


def cmd_status() -> int:
    """Summary of what the database holds, and how each store's last run went."""
    if not DB_PATH.exists():
        print(f"No database yet at {DB_PATH}. Run: python -m scraper run")
        return 1
    db = DB(DB_PATH)
    _, stores = load_config()
    total = db.q("SELECT COUNT(*), COUNT(DISTINCT variant_key), MIN(first_seen), MAX(last_seen) FROM price_points")[0]
    runs = db.q("SELECT COUNT(*) FROM runs")[0][0]
    print(f"Database: {DB_PATH}  ({DB_PATH.stat().st_size // 1024} KB)")
    print(f"  {total[0]} price records for {total[1]} iPhone variants, {runs} scrape runs")
    print(f"  first price {(total[2] or '-')[:16]}, latest {(total[3] or '-')[:16]} (UTC)\n")
    print(f"{'Store':16} {'Enabled':8} {'Last run':17} {'Result':7} {'Variants':>8}  Problem")
    for s in stores:
        r = db.q("SELECT * FROM runs WHERE store_id=? ORDER BY id DESC LIMIT 1", s["id"])
        n = db.q("SELECT COUNT(DISTINCT variant_key) FROM price_points WHERE store_id=?", s["id"])[0][0]
        if r:
            r = r[0]
            print(f"{s['name'][:16]:16} {'yes' if s.get('enabled', True) else 'no':8} {(r['finished_at'] or r['started_at'])[:16]:17} "
                  f"{r['status']:7} {n:>8}  {(r['error'] or '')[:70]}")
        else:
            print(f"{s['name'][:16]:16} {'yes' if s.get('enabled', True) else 'no':8} {'never':17} {'-':7} {n:>8}")
    return 0


def cmd_health(args) -> int:
    if args.test_email:
        site, key = os.getenv("SITE_URL", "").rstrip("/"), os.getenv("STATUS_KEY", "")
        return health.send_test_email(f"{site}/status-{key}/" if site and key else None)
    _, stores = load_config()
    if not DB_PATH.exists():
        print(f"No database yet at {DB_PATH}. Run: python -m scraper run")
        return 1
    site, key = os.getenv("SITE_URL", "").rstrip("/"), os.getenv("STATUS_KEY", "")
    status_url = f"{site}/status-{key}/" if site and key else None
    return health.run(DB(DB_PATH), stores, notify=args.notify, status_url=status_url)


def main(argv: list[str] | None = None) -> int:
    load_env()
    ap = argparse.ArgumentParser(prog="scraper", description="Nepal iPhone price tracker")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="scrape stores and export")
    r.add_argument("--store", action="append", help="only this store id (repeatable)")
    r.add_argument("--no-proxy", action="store_true", help="connect directly, ignore Webshare")
    r.add_argument("--dry-run", action="store_true", help="scrape and print, don't save")

    p = sub.add_parser("probe", help="show what the extractors find on one URL")
    p.add_argument("url")
    p.add_argument("--store")
    p.add_argument("--js", action="store_true", help="render with headless Chromium")
    p.add_argument("--no-proxy", action="store_true")
    p.add_argument("--save", help="write fetched HTML to this file")

    sub.add_parser("proxies", help="test Webshare proxy credentials")
    sub.add_parser("status", help="show what is in the database and how each store's last run went")
    im = sub.add_parser("image", help="use your own image for a model instead of the store's photo")
    im.add_argument("model", nargs="?", help='e.g. "iPhone 17 Pro"')
    im.add_argument("source", nargs="?", help="image link (https://...) or a file on this computer")
    im.add_argument("--credit", help='shown under the image, e.g. "Image: Apple"')
    im.add_argument("--remove", action="store_true", help="go back to the store's photo")
    im.add_argument("--list", action="store_true", help="list the images you've set")
    im.add_argument("--force", action="store_true", help="use the image even if it looks like a banner or has a dark background")
    ins = sub.add_parser("inspect", help="show where each price for one model came from")
    ins.add_argument("model", help='e.g. "iPhone 17" or "17 Pro Max"')
    h = sub.add_parser("health", help="check for broken stores and suspicious prices")
    h.add_argument("--notify", action="store_true", help="email new problems to OWNER_EMAIL; exit code 1 if there are any")
    h.add_argument("--test-email", action="store_true", help="send a test email to OWNER_EMAIL and stop")
    sub.add_parser("export", help="rebuild website JSON from the database")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "probe":
        return cmd_probe(args)
    if args.cmd == "health":
        return cmd_health(args)
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "inspect":
        return cmd_inspect(args)
    if args.cmd == "image":
        return cmd_image(args)
    if args.cmd == "proxies":
        check_pool(ProxyPool.from_env())
        return 0
    if args.cmd == "export":
        defaults, stores = load_config()
        latest = export(DB(DB_PATH), stores, WEB_DATA, defaults.get("stale_after_days", 3))
        print(f"Exported {len(latest['variants'])} variants")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
