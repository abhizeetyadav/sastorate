"""Admin panel that runs on your own computer:  python -m scraper admin

Opens http://127.0.0.1:8765 in your browser. Nothing is put on the internet:
- it only listens on 127.0.0.1 (this computer),
- every change needs a secret token that's made fresh each time the panel starts,
- requests for any other host name are refused (stops "DNS rebinding" tricks from websites you visit).

It edits the same files the scraper uses (shop_info.yaml, overrides.yaml, models.yaml, web/logos, web/images)
and rebuilds the local site after each change. Publish puts your changes live with scripts/deploy.py.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import webbrowser
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import shopconfig as cfgmod
from . import health, model_images, overrides
from .db import DB
from .export import DETAIL_TEXT, export, store_details
from .normalize import is_product_url, parse_model

log = logging.getLogger("scraper.admin")

ROOT = cfgmod.ROOT
WEB = ROOT / "web"
PAGE = Path(__file__).resolve().parent / "admin_page.html"
MAX_BODY = 25_000_000
LOGO_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
SVG_DANGER = re.compile(rb"<\s*script|javascript:|\son[a-z]+\s*=|<\s*foreignObject|<\s*iframe|<\s*embed", re.I)


class Problem(Exception):
    """Something the person should fix; shown in the panel as is."""

    def __init__(self, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.status, self.extra = status, extra or {}


# ================================================================ data
class Admin:
    def __init__(self, db_path: Path, web_dir: Path = WEB, config: Path | None = None, shop_info: Path | None = None,
                 overrides_path: Path | None = None, models_file: Path | None = None):
        self.db_path, self.web = Path(db_path), Path(web_dir)
        self.config, self.shop_info = config or cfgmod.CONFIG, shop_info or cfgmod.SHOP_INFO
        self.overrides_path = overrides_path or overrides.DEFAULT_PATH
        self.models_file = models_file or model_images.MODELS_FILE
        self.lock = threading.Lock()
        self.job: dict | None = None

    # ---- helpers
    def stores(self) -> tuple[dict, list[dict]]:
        return cfgmod.load_config(self.config, self.shop_info)

    def db(self) -> DB:
        return DB(self.db_path)

    def rebuild(self) -> str:
        """Re-export so the local preview shows the change straight away."""
        if not self.db_path.exists():
            return "saved (no price database yet, so the preview isn't rebuilt)"
        defaults, stores = self.stores()
        try:
            rules = overrides.load_rules(self.overrides_path, known_stores={s["id"] for s in stores})
            export(self.db(), stores, self.web / "data", defaults.get("stale_after_days", 3), rules=rules)
            return "saved"
        except Exception as e:  # noqa: BLE001 - the change is saved even if the preview can't be rebuilt
            log.exception("rebuild failed")
            return f"saved, but the preview couldn't be rebuilt: {e}"

    # ---- overview
    def state(self) -> dict:
        defaults, stores = self.stores()
        db = self.db() if self.db_path.exists() else None
        problems = health.check(db, stores) if db else []
        out_stores = []
        for s in stores:
            run = db.q("SELECT * FROM runs WHERE store_id=? ORDER BY id DESC LIMIT 1", s["id"]) if db else []
            out_stores.append({
                "id": s["id"], "name": s.get("name", s["id"]), "url": s.get("url", ""),
                "authorized": bool(s.get("authorized")), "enabled": bool(s.get("enabled", True)),
                "kind": s.get("kind") or "store", "logo": s.get("logo"), "details": s.get("details") or {},
                "added_in_admin": bool(s.get("added_in_admin")),
                "run": dict(run[0]) if run else None,
                "problems": [p.message for p in problems if p.store == s["id"]],
            })
        models = []
        if db:
            models = [r["model"] for r in db.q(
                "SELECT model, MAX(last_seen) AS seen FROM price_points GROUP BY model ORDER BY seen DESC")]
            models.sort(key=_model_sort)
        return {"stores": out_stores, "models": models, "rules": self.rules(), "has_db": bool(db),
                "site_url": os.getenv("SITE_URL", ""), "changes": self.unpublished(), "job": self.job_view(0)}

    def unpublished(self) -> dict:
        """Files changed since the last publish (from git), so the panel can say what Publish will send."""
        try:
            out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return {"known": False, "files": []}
        if out.returncode != 0:
            return {"known": False, "files": []}
        files = [line[3:].strip().strip('"') for line in out.stdout.splitlines() if line.strip()]
        mine = [f for f in files if f.startswith(("scraper/config/", "web/logos/", "web/images/"))]
        return {"known": True, "files": mine}

    # ---- shops
    def save_shop(self, p: dict) -> dict:
        sid = str(p.get("id") or "").strip().lower()
        _, stores = self.stores()
        exists = any(s["id"] == sid for s in stores)
        if not exists:
            if not cfgmod.ID_RE.match(sid):
                raise Problem("Shop id: 2 to 31 lowercase letters, digits or dashes, e.g. 'nepalmobile'.")
        info = cfgmod.read_shop_info(self.shop_info)
        entry = dict(info.get(sid) or {})
        name = _clean(p.get("name"), 60)
        url = str(p.get("url") or "").strip()
        if name:
            entry["name"] = name
        if url:
            if not re.match(r"^https://[a-z0-9.-]+\.[a-z]{2,}(/.*)?$", url, re.I):
                raise Problem("Website must be a full address starting with https://")
            entry["url"] = url.rstrip("/")
        if not exists and not (entry.get("name") and entry.get("url")):
            raise Problem("A new shop needs a name and a website.")
        for flag in ("authorized", "enabled"):
            if flag in p:
                entry[flag] = bool(p[flag])
        if "details" in p:
            details = _details_from(p.get("details") or {})
            if details:
                entry["details"] = details
            else:
                entry.pop("details", None)
        info[sid] = entry
        cfgmod.write_shop_info(info, self.shop_info)
        return {"message": self.rebuild(), "id": sid, "new": not exists}

    def save_logo(self, p: dict) -> dict:
        sid = str(p.get("id") or "")
        if not any(s["id"] == sid for s in self.stores()[1]):
            raise Problem("Unknown shop.")
        ext = Path(str(p.get("filename") or "")).suffix.lower()
        if ext not in LOGO_TYPES:
            raise Problem("Logo must be .svg, .png, .webp or .jpg")
        data = _b64(p.get("data"), 2_000_000, "logo")
        if ext == ".svg" and (SVG_DANGER.search(data) or b"<svg" not in data.lower()):
            raise Problem("That SVG contains scripts or isn't an SVG image. Export it again, or use a PNG.")
        folder = self.web / "logos"
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob(f"{sid}.*"):
            old.unlink()
        (folder / f"{sid}{'.jpg' if ext == '.jpeg' else ext}").write_bytes(data)
        info = cfgmod.read_shop_info(self.shop_info)
        info.setdefault(sid, {})["logo"] = f"logos/{sid}{'.jpg' if ext == '.jpeg' else ext}"
        cfgmod.write_shop_info(info, self.shop_info)
        return {"message": self.rebuild()}

    def remove_logo(self, sid: str) -> dict:
        for old in (self.web / "logos").glob(f"{sid}.*"):
            if old.name != "README.txt":
                old.unlink()
        info = cfgmod.read_shop_info(self.shop_info)
        if sid in info:
            info[sid].pop("logo", None)
            cfgmod.write_shop_info(info, self.shop_info)
        return {"message": self.rebuild()}

    # ---- prices and hiding
    def rules(self) -> list[dict]:
        today = date.today()
        out = []
        for i, r in enumerate(overrides.read_raw(self.overrides_path)):
            until = str(r.get("until") or "")[:10]
            out.append({"index": i, "store": r.get("store"), "model": r.get("model") or "", "storage": str(r.get("storage") or ""),
                        "until": until, "reason": r.get("reason") or "",
                        "active": not until or until >= today.isoformat()})
        return out

    def prices(self, model_name: str) -> dict:
        parsed = parse_model(model_name if "iphone" in model_name.lower() else f"iPhone {model_name}")
        if not parsed or not self.db_path.exists():
            return {"model": model_name, "rows": []}
        model = parsed[0]
        _, stores = self.stores()
        names = {s["id"]: s.get("name", s["id"]) for s in stores}
        rules = overrides.load_rules(self.overrides_path)
        rows = self.db().q("""SELECT p.* FROM price_points p
                              JOIN (SELECT store_id, variant_key, MAX(id) AS id FROM price_points
                                    WHERE model = ? GROUP BY store_id, variant_key) l ON l.id = p.id
                              ORDER BY p.storage_gb, p.price""", model)
        out = []
        for r in rows:
            hidden = None
            if not is_product_url(r["url"] or ""):
                hidden = "Not a product page (article, news or search results)"
            else:
                rule = overrides.is_hidden(rules, r["store_id"], r["model"], r["storage_gb"])
                if rule:
                    hidden = f"Hidden by you{': ' + rule.reason if rule.reason else ''}"
            out.append({"store": r["store_id"], "store_name": names.get(r["store_id"], r["store_id"]),
                        "storage_gb": r["storage_gb"], "price": r["price"],
                        "in_stock": None if r["in_stock"] is None else bool(r["in_stock"]),
                        "last_seen": r["last_seen"], "title": r["title"], "url": r["url"], "hidden": hidden})
        return {"model": model, "rows": out}

    def hide(self, p: dict) -> dict:
        store = str(p.get("store") or "")
        if not any(s["id"] == store for s in self.stores()[1]):
            raise Problem("Unknown shop.")
        rule = {"store": store, "reason": _clean(p.get("reason"), 200)}
        if p.get("model"):
            rule["model"] = _clean(p["model"], 40)
        if p.get("storage_gb"):
            gb = int(p["storage_gb"])
            rule["storage"] = f"{gb // 1024} TB" if gb >= 1024 else f"{gb} GB"
        if p.get("until"):
            try:
                rule["until"] = date.fromisoformat(str(p["until"])[:10]).isoformat()
            except ValueError:
                raise Problem("'Until' must be a date.")
        rules = overrides.read_raw(self.overrides_path) + [rule]
        overrides.write_raw(rules, self.overrides_path)
        return {"message": self.rebuild()}

    def unhide(self, index: int) -> dict:
        rules = overrides.read_raw(self.overrides_path)
        if not 0 <= index < len(rules):
            raise Problem("That rule no longer exists. Reload the page.")
        rules.pop(index)
        overrides.write_raw(rules, self.overrides_path)
        return {"message": self.rebuild()}

    # ---- model images
    def images(self) -> dict:
        own = model_images._read(self.models_file)
        latest = {}
        try:
            data = json.loads((self.web / "data" / "latest.json").read_text(encoding="utf-8"))
            for v in data.get("variants", []):
                latest.setdefault(v["model"], v.get("image"))
        except (OSError, ValueError):
            pass
        models = self.state()["models"] if self.db_path.exists() else sorted(latest)
        out = []
        for m in models:
            e = own.get(m) or {}
            path = str(e.get("image") or "")
            out.append({"model": m, "own": path if path and (self.web / path).is_file() else None,
                        "credit": e.get("credit") or "", "store_image": None if path else latest.get(m)})
        return {"models": out}

    def set_image(self, p: dict) -> dict:
        model, credit, force = str(p.get("model") or ""), _clean(p.get("credit"), 80) or None, bool(p.get("force"))
        tmp = None
        try:
            if p.get("data"):
                ext = Path(str(p.get("filename") or "x.png")).suffix.lower() or ".png"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                tmp.write(_b64(p["data"], 15_000_000, "image"))
                tmp.close()
                source = tmp.name
            elif p.get("url"):
                source = str(p["url"]).strip()
                if not re.match(r"^https?://", source, re.I):
                    raise Problem("Paste the image's address (it starts with https://).")
            else:
                raise Problem("Choose a file or paste an image address.")
            try:
                m, rel = model_images.import_image(model, source, credit, self.web, self.models_file, force=force)
            except model_images.ImageProblem as e:
                raise Problem("This image would look wrong on the site.", 409, {"problems": e.problems})
            except ValueError as e:
                raise Problem(f"Couldn't use that image: {e}")
            except Exception as e:  # noqa: BLE001 - network errors and the like
                raise Problem(f"Couldn't download that image: {e}")
        finally:
            if tmp:
                Path(tmp.name).unlink(missing_ok=True)
        return {"message": self.rebuild(), "model": m, "image": rel}

    def remove_image(self, model: str) -> dict:
        if not model_images.remove_image(model, self.web, self.models_file):
            raise Problem("No image set for that model.")
        return {"message": self.rebuild()}

    # ---- background jobs (one at a time): scrape a shop, test a page, publish
    def start_job(self, p: dict) -> dict:
        kind = p.get("kind")
        py = [sys.executable]
        if kind == "scrape":
            sid = str(p.get("store") or "")
            if not any(s["id"] == sid for s in self.stores()[1]):
                raise Problem("Unknown shop.")
            cmd, title = py + ["-m", "scraper", "run", "--store", sid], f"Scraping {sid}"
        elif kind == "probe":
            url, sid = str(p.get("url") or "").strip(), str(p.get("store") or "")
            if not re.match(r"^https?://", url):
                raise Problem("Paste a full page address, starting with https://")
            cmd = py + ["-m", "scraper", "probe", url] + (["--store", sid] if sid else [])
            title = "Testing a page"
        elif kind == "publish":
            cmd = py + [str(ROOT / "scripts" / "deploy.py"), "--yes"] + (["--no-watch"] if p.get("update_prices") else ["--no-run"])
            title = "Publishing"
        else:
            raise Problem("Unknown job.")
        with self.lock:
            if self.job and not self.job["done"]:
                raise Problem(f"Wait for '{self.job['title']}' to finish first.", 409)
            job = {"id": secrets.token_hex(4), "title": title, "lines": [], "done": False, "code": None,
                   "started": datetime.now(timezone.utc).isoformat()}
            self.job = job
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

        def run():
            try:
                proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
                for line in proc.stdout:
                    job["lines"].append(line.rstrip("\n")[:2000])
                    del job["lines"][:-3000]
                job["code"] = proc.wait()
            except Exception as e:  # noqa: BLE001
                job["lines"].append(f"Couldn't start: {e}")
                job["code"] = -1
            finally:
                job["done"] = True
        threading.Thread(target=run, daemon=True).start()
        return {"job": self.job_view(0)}

    def job_view(self, since: int) -> dict | None:
        j = self.job
        if not j:
            return None
        return {"id": j["id"], "title": j["title"], "done": j["done"], "code": j["code"],
                "lines": j["lines"][since:], "total": len(j["lines"])}


# ================================================================ helpers
def _clean(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _b64(v, limit: int, what: str) -> bytes:
    try:
        data = base64.b64decode(str(v or "").split(",")[-1], validate=True)
    except (binascii.Error, ValueError):
        raise Problem(f"That {what} file couldn't be read.")
    if not data:
        raise Problem(f"That {what} file is empty.")
    if len(data) > limit:
        raise Problem(f"That {what} is too big (max {limit // 1_000_000} MB).")
    return data


def _details_from(d: dict) -> dict:
    """Keep what export.store_details() accepts, in the shape stores.yaml uses."""
    raw = {k: d.get(k) for k in (*DETAIL_TEXT, "phone")}
    raw["locations"] = [{k: (loc or {}).get(k) for k in ("name", "city", "address", "phone", "map")}
                        for loc in (d.get("locations") or []) if _clean((loc or {}).get("name"), 80)]
    clean = store_details({"id": "x", "details": raw}) or {}
    return clean


def _model_sort(m: str):
    n = re.findall(r"\d+", m)
    return (-int(n[0]) if n else 0, m)


# ================================================================ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "BhaauAdmin"

    # quiet console: only errors
    def log_message(self, fmt, *args):  # noqa: N802
        pass

    @property
    def admin(self) -> Admin:
        return self.server.admin  # type: ignore[attr-defined]

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return self._send(403, b"Forbidden", "text/plain")
        u = urlparse(self.path)
        path = unquote(u.path)
        if path in ("/", "/index.html"):
            html = PAGE.read_text(encoding="utf-8").replace("__ADMIN_TOKEN__", self.server.token)  # type: ignore[attr-defined]
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; img-src 'self' https: data:; "
                                                          "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                                          "frame-ancestors 'none'"})
        if path == "/preview" or path.startswith("/preview/"):
            return self._static(path[len("/preview"):] or "/")
        if path.startswith("/api/"):
            if not self._token_ok():
                return self._json(403, {"error": "Reload the admin page."})
            q = parse_qs(u.query)
            try:
                if path == "/api/state":
                    return self._json(200, self.admin.state())
                if path == "/api/prices":
                    return self._json(200, self.admin.prices((q.get("model") or [""])[0]))
                if path == "/api/images":
                    return self._json(200, self.admin.images())
                if path == "/api/job":
                    return self._json(200, {"job": self.admin.job_view(int((q.get("since") or ["0"])[0]))})
            except Problem as e:
                return self._json(e.status, {"error": str(e), **e.extra})
        return self._send(404, b"Not found", "text/plain")

    def do_POST(self):  # noqa: N802
        if not self._host_ok():
            return self._send(403, b"Forbidden", "text/plain")
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self._json(403, {"error": "Not allowed from another website."})
        if not self._token_ok() or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json(403, {"error": "Reload the admin page."})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "That file is too big."})
        try:
            p = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "Bad request."})
        a = self.admin
        routes = {
            "/api/shop": lambda: a.save_shop(p),
            "/api/shop/logo": lambda: a.save_logo(p),
            "/api/shop/logo/remove": lambda: a.remove_logo(str(p.get("id") or "")),
            "/api/hide": lambda: a.hide(p),
            "/api/unhide": lambda: a.unhide(int(p.get("index", -1))),
            "/api/image": lambda: a.set_image(p),
            "/api/image/remove": lambda: a.remove_image(str(p.get("model") or "")),
            "/api/job": lambda: a.start_job(p),
        }
        fn = routes.get(urlparse(self.path).path)
        if not fn:
            return self._json(404, {"error": "Not found."})
        try:
            return self._json(200, fn())
        except Problem as e:
            return self._json(e.status, {"error": str(e), **e.extra})
        except Exception as e:  # noqa: BLE001
            log.exception("admin action failed")
            return self._json(500, {"error": f"Something went wrong: {e}"})

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Admin-Token", ""), self.server.token)  # type: ignore[attr-defined]

    def _static(self, rel: str):
        """The local site, so you can preview changes before publishing."""
        target = (self.admin.web / rel.lstrip("/")).resolve()
        web = self.admin.web.resolve()
        if web not in target.parents and target != web:
            return self._send(404, b"Not found", "text/plain")
        if target.is_dir():
            if not rel.endswith("/"):
                return self._send(301, b"", "text/plain", {"Location": f"/preview{rel}/"})
            target = target / "index.html"
        if not target.is_file():
            nf = web / "404.html"
            return self._send(404, nf.read_bytes() if nf.exists() else b"Not found", "text/html; charset=utf-8")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return self._send(200, target.read_bytes(), ctype)


def serve(db_path: Path, port: int = 8765, open_browser: bool = True) -> None:
    admin = Admin(db_path)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port busy: pick a free one
    httpd.admin = admin  # type: ignore[attr-defined]
    httpd.token = secrets.token_urlsafe(24)  # type: ignore[attr-defined]
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"\n  Bhaau admin is open at {url}\n  Only this computer can reach it. Press Ctrl+C here to close it.\n", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Admin closed.")
    finally:
        httpd.server_close()
