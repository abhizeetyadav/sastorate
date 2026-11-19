"""Generate clearly-labelled DEMO data so the website can be previewed before the
first real scrape. Uses the real DB + export code, so it doubles as a smoke test.

    python scripts/make_demo_data.py          -> web/data/latest.json (demo: true)
"""
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from scraper.db import DB  # noqa: E402
from scraper.export import export  # noqa: E402
from scraper.normalize import classify  # noqa: E402

random.seed(7)
BASE = {  # rough reference prices (NPR) used only to shape the demo
    "iPhone 18 Pro Max": {256: 279900, 512: 322900, 1024: 407900, 2048: 534900},
    "iPhone 18 Pro": {256: 254900, 512: 296900, 1024: 379900},
    "iPhone 17 Pro Max": {256: 239999, 512: 279999, 1024: 319999, 2048: 397999},
    "iPhone 17 Pro": {256: 220999, 512: 259999, 1024: 299999},
    "iPhone Air": {256: 189999, 512: 229999, 1024: 269999},
    "iPhone 17": {256: 161599, 512: 200899},
    "iPhone 17e": {256: 127799, 512: 168999},
    "iPhone 16": {128: 146599, 256: 166599},
    "iPhone 16e": {128: 105999, 256: 125999},
    "iPhone 15": {128: 119499},
}


def main():
    cfg = yaml.safe_load((ROOT / "scraper/config/stores.yaml").read_text(encoding="utf-8"))
    stores = [s for s in cfg["stores"] if s.get("enabled", True)]
    db_path = ROOT / "data" / "demo.db"
    db_path.unlink(missing_ok=True)
    db = DB(db_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    days = 60
    for s in stores:
        bias = random.uniform(-0.02, 0.03) if not s.get("authorized") else random.uniform(-0.005, 0.01)
        for model, sizes in BASE.items():
            if random.random() < 0.15:  # not every store carries every model
                continue
            for gb, base in sizes.items():
                label = f"{gb // 1024}TB" if gb >= 1024 else f"{gb}GB"
                title = f"Apple {model} {label}"
                v = classify(title)
                price = round(base * (1 + bias + random.uniform(-0.01, 0.01)), -2) - 1
                old = None
                for d in range(days, -1, -1):
                    if random.random() < 0.04:  # occasional price move
                        price = round(price * random.choice([0.96, 0.97, 0.98, 1.01, 1.02]), -2) - 1
                        old = price + random.choice([0, 0, 5000, 8000]) or None
                    when = (now - timedelta(days=d, hours=random.randint(0, 5))).isoformat()
                    db.record(s["id"], v, title, f"{s['url']}/demo/{v.key.replace('|', '-')}", int(price),
                              old, random.random() > 0.08, seen_at=when)
        rid = db.start_run(s["id"])
        db.finish_run(rid, "ok", 1, 1)
    db.commit()
    out = ROOT / "web" / "data"
    latest = export(db, cfg["stores"], out, cfg["defaults"].get("stale_after_days", 3))
    latest["demo"] = True
    latest["alerts_url"] = "preview"   # show the alert form; the preview explains it isn't connected
    (out / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    from scraper.seo import build_site
    build_site(latest, ROOT / "web")
    print(f"demo: {len(latest['variants'])} variants, {len(latest['drops'])} drops -> {out}")


if __name__ == "__main__":
    main()
