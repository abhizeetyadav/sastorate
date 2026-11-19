"""Where the store list comes from.

- scraper/config/stores.yaml   - hand-written: how to scrape each shop (start pages, selectors...).
- scraper/config/shop_info.yaml - written by the admin panel: what you manage about each shop
                                  (name, website, Apple authorized, on/off, logo, details, branches),
                                  plus shops added in the panel.

shop_info.yaml is laid over stores.yaml, so the panel never rewrites your hand-written file or its comments.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "scraper" / "config" / "stores.yaml"
SHOP_INFO = ROOT / "scraper" / "config" / "shop_info.yaml"
EDITABLE = ("name", "url", "authorized", "enabled", "logo", "details")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
SHOP_INFO_HEADER = """\
# Managed by the admin panel (python -m scraper admin). You can edit it by hand too.
# Each entry is laid over the shop with the same id in stores.yaml.
# Shops that only exist here were added in the panel and use automatic discovery.
"""


def read_shop_info(path: Path | None = None) -> dict[str, dict]:
    path = path or SHOP_INFO
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): (v or {}) for k, v in (data.get("shops") or {}).items()}


def write_shop_info(shops: dict[str, dict], path: Path | None = None) -> None:
    path = path or SHOP_INFO
    clean = {k: {f: v for f, v in (e or {}).items() if v not in (None, "", [], {})} for k, e in shops.items()}
    body = yaml.safe_dump({"shops": clean}, allow_unicode=True, sort_keys=False, width=110)
    path.write_text(SHOP_INFO_HEADER + "\n" + body, encoding="utf-8")


def load_config(config: Path | None = None, shop_info: Path | None = None) -> tuple[dict, list[dict]]:
    """(defaults, stores) with shop_info.yaml applied."""
    cfg = yaml.safe_load((config or CONFIG).read_text(encoding="utf-8")) or {}
    stores = [dict(s) for s in cfg.get("stores") or []]
    info = read_shop_info(shop_info)
    by_id = {s["id"]: s for s in stores}
    for sid, extra in info.items():
        if sid in by_id:
            by_id[sid].update({k: v for k, v in extra.items() if k in EDITABLE})
        elif extra.get("name") and extra.get("url"):  # added in the admin panel
            stores.append({"id": sid, "discover": True, "enabled": True,
                           **{k: v for k, v in extra.items() if k in EDITABLE}, "added_in_admin": True})
    return cfg.get("defaults", {}), stores
