"""Send the latest prices to the price-alert service after each scrape.

The alert service (alerts/worker.js, on Cloudflare) keeps the subscriber list and
decides who gets an email. The scraper only tells it, for every phone variant,
the lowest price at a seller that has it in stock. No subscriber data ever
comes back here.

Needs ALERTS_URL (e.g. https://bhaau-alerts.<you>.workers.dev) and ALERTS_TOKEN
in .env / GitHub secrets. Without them this step is skipped.
"""
from __future__ import annotations

import logging
import os
import re

import requests

log = logging.getLogger(__name__)


def _gb(g: int | None) -> str:
    return "" if not g else (f"{g // 1024} TB" if g >= 1024 else f"{g} GB")


def build_payload(latest: dict) -> list[dict]:
    stores = {s["id"]: s["name"] for s in latest["stores"]}
    out = []
    for v in latest["variants"]:
        if not v.get("storage_gb"):  # "size not listed" offers can't be matched to an alert
            continue
        in_stock = [o for o in v["offers"] if o.get("in_stock") is not False and o["store"] in stores]
        best = min(in_stock, key=lambda o: o["price"]) if in_stock else None
        out.append({
            "key": v["key"],
            "label": f"{v['model']} {_gb(v['storage_gb'])}",
            "slug": re.sub(r"[^a-z0-9]+", "-", v["model"].lower()).strip("-"),
            "storage_gb": v["storage_gb"],
            "price": best["price"] if best else None,     # None = nobody has it in stock
            "store": stores[best["store"]] if best else None,
            "url": best["url"] if best else None,
        })
    return out


def push_prices(latest: dict) -> dict | None:
    url, token = os.getenv("ALERTS_URL", "").rstrip("/"), os.getenv("ALERTS_TOKEN", "")
    if not (url and token):
        log.info("Price alerts: ALERTS_URL / ALERTS_TOKEN not set, skipping")
        return None
    if latest.get("demo"):
        log.info("Price alerts: demo data, not sending")
        return None
    payload = build_payload(latest)
    try:
        r = requests.post(f"{url}/notify", json={"variants": payload},
                          headers={"Authorization": f"Bearer {token}"}, timeout=60)
        r.raise_for_status()
        result = r.json()
        log.info("Price alerts: %s variants sent, %s alerts matched, %s emails sent, %s waiting for tomorrow",
                 result.get("variants"), result.get("alerts_matched"), result.get("emails_sent"), result.get("emails_deferred"))
        return result
    except Exception as e:  # noqa: BLE001 - alerts must never break the price update
        log.error("Price alerts: could not reach the alert service: %s", e)
        return None
