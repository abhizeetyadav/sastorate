"""Private status page, rebuilt with every export: web/status-<STATUS_KEY>/index.html

Set STATUS_KEY (in .env and as a GitHub secret) to a long random string; the page
lives at https://your-site/status-<that string>/. It isn't linked from the site,
isn't in the sitemap and tells search engines not to index it. It shows store
health, recent scrapes, price changes, hidden prices and alert sign-up counts.
It contains no subscriber emails or other personal data.
"""
from __future__ import annotations

import html
import logging
import os
import re
import shutil
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .db import DB
from .health import _gb, _rs, check

log = logging.getLogger(__name__)
MARKER = ".bhaau-status"
KTM = timezone(timedelta(hours=5, minutes=45))
e = lambda s: html.escape(str(s if s is not None else ""), quote=True)  # noqa: E731


def _ago(iso: str | None, now: datetime) -> str:
    if not iso:
        return "never"
    s = (now - datetime.fromisoformat(iso)).total_seconds()
    if s < 3600:
        return f"{max(1, int(s // 60))} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} days ago"


def _alert_stats() -> dict | None:
    url, token = os.getenv("ALERTS_URL", "").rstrip("/"), os.getenv("ALERTS_TOKEN", "")
    if not (url and token):
        return None
    try:
        r = requests.get(f"{url}/stats", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as ex:  # noqa: BLE001
        log.warning("status page: couldn't read alert stats: %s", ex)
        return None


def build(db: DB, stores: list[dict], web_dir: str | Path, key: str | None = None, rules: list | None = None) -> Path | None:
    key = key or os.getenv("STATUS_KEY", "")
    web_dir = Path(web_dir)
    if not key:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", key):
        log.error("STATUS_KEY must be 12-80 letters, digits, - or _ (it becomes part of the page address)")
        return None
    now = datetime.now(timezone.utc)
    problems = check(db, stores, now)

    # ---------------------------------------------------------------- stores
    rows = []
    for s in stores:
        runs = db.q("SELECT * FROM runs WHERE store_id = ? ORDER BY id DESC LIMIT 12", s["id"])
        last = runs[0] if runs else None
        ok_counts = [r["offers"] for r in runs if r["status"] == "ok"]
        usual = int(statistics.median(ok_counts)) if ok_counts else 0
        peak = max([r["offers"] or 0 for r in runs] + [1])
        bars = "".join(
            f'<i title="{e(_ago(r["finished_at"] or r["started_at"], now))}: {r["offers"]} phones, {e(r["status"])}" '
            f'class="{"bad" if r["status"] != "ok" else ""}" style="height:{max(3, int((r["offers"] or 0) / peak * 26))}px"></i>'
            for r in reversed(runs))
        status = ("off", "Paused") if not s.get("enabled", True) else \
                 ("ok", "Working") if last and last["status"] == "ok" else \
                 ("bad", {"error": "Failed", "empty": "No prices", "running": "Stopped mid-run"}.get(last["status"], "Never run") if last else "Never run")
        rows.append(f"""<tr><td><b>{e(s['name'])}</b>{' <span class="tag">Authorized</span>' if s.get('authorized') else ''}<br>
          <span class="muted">{e(s['id'])}</span></td>
          <td><span class="st {status[0]}">{e(status[1])}</span></td>
          <td>{e(_ago(last and (last['finished_at'] or last['started_at']), now))}</td>
          <td class="r">{last['offers'] if last else '–'}<span class="muted"> / ~{usual}</span></td>
          <td><div class="bars">{bars}</div></td>
          <td class="err">{e((last and last['error']) or '')[:160]}</td></tr>""")

    # ---------------------------------------------------------------- recent price changes
    day_ago = (now - timedelta(hours=24)).isoformat()
    changes = db.q("""SELECT p.*, (SELECT q.price FROM price_points q WHERE q.store_id = p.store_id AND q.variant_key = p.variant_key
                        AND q.id < p.id ORDER BY q.id DESC LIMIT 1) AS prev FROM price_points p
                      WHERE p.first_seen >= ? ORDER BY p.first_seen DESC LIMIT 400""", day_ago)
    names = {s["id"]: s["name"] for s in stores}
    moved = [c for c in changes if c["prev"] and c["prev"] != c["price"]]
    new = [c for c in changes if not c["prev"]]
    change_rows = "".join(
        f'<tr><td>{e(names.get(c["store_id"], c["store_id"]))}</td><td>{e(c["model"])} {e(_gb(c["storage_gb"]))}</td>'
        f'<td class="r muted">{_rs(c["prev"])}</td><td class="r"><b class="{"down" if c["price"] < c["prev"] else "up"}">{_rs(c["price"])}</b></td>'
        f'<td class="r {"down" if c["price"] < c["prev"] else "up"}">{(c["price"] - c["prev"]) / c["prev"] * 100:+.1f}%</td></tr>'
        for c in moved[:40])

    # ---------------------------------------------------------------- hidden prices + alerts
    rules = rules or []
    hidden = "".join(f'<li>{e(r.describe())}{"" if r.active() else " <span class=muted>(expired, no longer hiding)</span>"}</li>' for r in rules)
    stats = _alert_stats()
    if stats:
        watched = "".join(f"<li>{e(w['label'])}: {w['alerts']}</li>" for w in stats.get("most_watched", [])[:8])
        alerts_html = (f'<div class="kpis"><div><b>{stats.get("active_alerts", 0)}</b><span>active alerts</span></div>'
                       f'<div><b>{stats.get("people", 0)}</b><span>people</span></div>'
                       f'<div><b>{stats.get("pending_alerts", 0)}</b><span>waiting for confirmation</span></div></div>'
                       + (f"<h3>Most watched</h3><ul>{watched}</ul>" if watched else ""))
    else:
        alerts_html = '<p class="muted">Price alerts aren\'t set up, or the alert service couldn\'t be reached.</p>'

    problems_html = ("".join(f'<li class="{p.severity}"><b>{"Broken" if p.severity == "broken" else "Check"}:</b> {e(p.message)}</li>'
                             for p in problems) if problems else '<li class="good">All stores look healthy.</li>')
    stamp = now.astimezone(KTM).strftime("%d %b %Y, %I:%M %p").lstrip("0")

    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Bhaau status</title>
<style>
:root{{--ink:#1b1f24;--ink2:#4a5360;--ink3:#6e7784;--line:#e2e6eb;--bg:#f1f3f6;--card:#fff;--g:#0a7a43;--gw:#e6f5ec;--r:#c2271b;--rw:#fdecea;--a:#8a5a00;--aw:#fff3d6}}
@media (prefers-color-scheme:dark){{:root{{--ink:#e9ecf0;--ink2:#b8c0ca;--ink3:#8a94a1;--line:#2b313a;--bg:#111418;--card:#1a1e24;--g:#49c486;--gw:#13301f;--r:#ff8073;--rw:#3a1a17;--a:#f2c46b;--aw:#33290f}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14.5px/1.5 Inter,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:24px 18px 60px}} h1{{font-size:24px;margin:0}} h2{{font-size:17px;margin:28px 0 10px}} h3{{font-size:14px;margin:14px 0 6px}}
.muted{{color:var(--ink3)}} .card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}} th{{text-align:left;font-size:12.5px;color:var(--ink3);font-weight:600;padding:10px 8px;border-bottom:1px solid var(--line)}}
td{{padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:middle}} tr:last-child td{{border-bottom:0}} .r{{text-align:right}}
.st{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12.5px;font-weight:600}} .st.ok{{background:var(--gw);color:var(--g)}} .st.bad{{background:var(--rw);color:var(--r)}} .st.off{{background:var(--line);color:var(--ink2)}}
.tag{{font-size:11px;color:var(--ink3);border:1px solid var(--line);border-radius:4px;padding:0 4px}}
.bars{{display:flex;align-items:flex-end;gap:2px;height:28px}} .bars i{{width:7px;background:var(--g);border-radius:2px;opacity:.75}} .bars i.bad{{background:var(--r)}}
.err{{color:var(--r);font-size:12.5px;max-width:280px}}
ul.problems{{list-style:none;padding:0;margin:0}} ul.problems li{{padding:10px 14px;border-radius:8px;margin-bottom:6px}}
li.broken{{background:var(--rw);color:var(--r)}} li.check{{background:var(--aw);color:var(--a)}} li.good{{background:var(--gw);color:var(--g)}}
.down{{color:var(--g)}} .up{{color:var(--r)}} .kpis{{display:flex;gap:28px;padding:10px 0}} .kpis b{{display:block;font-size:24px}} .kpis span{{color:var(--ink3);font-size:13px}}
</style></head><body><main>
<h1>Bhaau status</h1><p class="muted">Updated {e(stamp)} (Nepal time). This page is private: it isn't linked anywhere, and search engines are told not to list it.</p>
<h2>Problems</h2><ul class="problems">{problems_html}</ul>
<h2>Stores</h2><div class="card"><table><thead><tr><th>Store</th><th>Status</th><th>Last check</th><th class="r">Phones found / usual</th><th>Last 12 checks</th><th>Last error</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<h2>Price changes in the last 24 hours</h2>
<p class="muted">{len(moved)} price change{'s' if len(moved) != 1 else ''}, {len(new)} newly listed phone{'s' if len(new) != 1 else ''}.</p>
{f'<div class="card"><table><thead><tr><th>Store</th><th>iPhone</th><th class="r">Was</th><th class="r">Now</th><th class="r">Change</th></tr></thead><tbody>{change_rows}</tbody></table></div>' if change_rows else ''}
<h2>Hidden prices</h2>{f'<ul>{hidden}</ul>' if hidden else '<p class="muted">None. Add rules to scraper/config/overrides.yaml to hide a wrong price.</p>'}
<h2>Price alerts</h2>{alerts_html}
</main></body></html>"""

    folder = web_dir / f"status-{key}"
    for d in web_dir.glob("status-*"):  # remove pages from an old key
        if d.is_dir() and (d / MARKER).exists() and d != folder:
            shutil.rmtree(d)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(page, encoding="utf-8")
    (folder / MARKER).write_text("generated by scraper/status_page.py\n", encoding="utf-8")
    return folder / "index.html"
