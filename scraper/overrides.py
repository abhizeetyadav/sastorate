"""Hide wrong prices without touching the database: scraper/config/overrides.yaml.

    hide:
      - store: hukut               # store id from stores.yaml (required)
        model: iPhone 17 Pro       # optional: only this model
        storage: 256 GB            # optional: only this size (256, "256GB", "1 TB" all work)
        until: 2026-12-01          # optional: the rule stops on this date
        reason: shows the 128 GB price for every size

A matching price disappears from the website, its price history and the
price-drop lists at the next export (`python -m scraper export` takes seconds).
The database keeps everything, so deleting the rule brings it back.
Hidden prices are never sent to the price-alert service either.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

log = logging.getLogger(__name__)
DEFAULT_PATH = Path(__file__).resolve().parent / "config" / "overrides.yaml"


def _norm_model(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", str(s).lower().replace("apple ", "")).strip()
    return s if s.startswith("iphone") else f"iphone {s}"


def _norm_storage(s) -> int | None:
    if s in (None, ""):
        return None
    m = re.match(r"^\s*(\d+)\s*(tb|gb)?\s*$", str(s), re.I)
    if not m:
        raise ValueError(f"storage {s!r} isn't a size like 256, 256GB or 1TB")
    n = int(m.group(1))
    return n * 1024 if (m.group(2) or "").lower() == "tb" or (not m.group(2) and n <= 2) else n


@dataclass
class Rule:
    store: str
    model: str | None = None
    storage_gb: int | None = None
    until: date | None = None
    reason: str = ""

    def active(self, today: date | None = None) -> bool:
        return self.until is None or (today or date.today()) <= self.until

    def matches(self, store: str, model: str, storage_gb: int | None, today: date | None = None) -> bool:
        if not self.active(today) or store != self.store:
            return False
        if self.model and _norm_model(model) != self.model:
            return False
        if self.storage_gb and storage_gb != self.storage_gb:
            return False
        return True

    def describe(self) -> str:
        model = ("iPhone " + " ".join(w if w[0].isdigit() else w.capitalize() for w in self.model.split()[1:])).strip() \
            if self.model else "all iPhones"
        what = " ".join(x for x in [model,
                                     (f"{self.storage_gb // 1024} TB" if self.storage_gb and self.storage_gb >= 1024
                                      else f"{self.storage_gb} GB" if self.storage_gb else "")] if x)
        return f"{self.store}: {what}" + (f" until {self.until}" if self.until else "") + (f" ({self.reason})" if self.reason else "")


def load_rules(path: str | Path | None = None, known_stores: set[str] | None = None) -> list[Rule]:
    path = Path(path) if path else DEFAULT_PATH
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules = []
    for i, r in enumerate(data.get("hide") or [], 1):
        try:
            if not isinstance(r, dict) or not r.get("store"):
                raise ValueError("every rule needs a store")
            until = r.get("until")
            if until and not isinstance(until, date):
                until = date.fromisoformat(str(until))
            rule = Rule(store=str(r["store"]).strip(), model=_norm_model(r.get("model")),
                        storage_gb=_norm_storage(r.get("storage")), until=until, reason=str(r.get("reason") or ""))
        except Exception as e:  # noqa: BLE001 - one bad rule must not stop the export
            log.error("overrides.yaml rule %d ignored: %s", i, e)
            continue
        if known_stores is not None and rule.store not in known_stores:
            log.warning("overrides.yaml rule %d: no store with id %r in stores.yaml", i, rule.store)
        rules.append(rule)
    return rules


def is_hidden(rules: list[Rule], store: str, model: str, storage_gb: int | None, today: date | None = None) -> Rule | None:
    return next((r for r in rules if r.matches(store, model, storage_gb, today)), None)


# ---------------------------------------------------------------- editing (admin panel)
FILE_HEADER = """\
# Hide wrong prices. Managed by the admin panel (python -m scraper admin); you can edit it by hand too.
#
# hide:
#   - store: hukut               # shop id (required)
#     model: iPhone 17 Pro       # optional: only this model
#     storage: 256 GB            # optional: only this size
#     until: 2026-12-01          # optional: the rule stops on this date
#     reason: shows the 128 GB price for every size
"""


def read_raw(path: str | Path | None = None) -> list[dict]:
    """The rules exactly as written, for editing."""
    path = Path(path) if path else DEFAULT_PATH
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [dict(r) for r in (data.get("hide") or []) if isinstance(r, dict)]


def write_raw(rules: list[dict], path: str | Path | None = None) -> None:
    path = Path(path) if path else DEFAULT_PATH
    clean = []
    for r in rules:
        item = {"store": str(r["store"])}
        for k in ("model", "storage", "until", "reason"):
            if r.get(k) not in (None, ""):
                item[k] = str(r[k]) if k != "until" else str(r[k])[:10]
        clean.append(item)
    body = yaml.safe_dump({"hide": clean}, allow_unicode=True, sort_keys=False) if clean else "hide: []\n"
    path.write_text(FILE_HEADER + "\n" + body, encoding="utf-8")
