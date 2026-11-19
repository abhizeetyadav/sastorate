"""The local admin panel: security rules and every action, against a real server on a free port."""
import base64
import io
import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw

from scraper import admin as adm
from scraper import shopconfig as cfgmod
from scraper.db import DB
from scraper.normalize import classify

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def panel(tmp_path):
    web = tmp_path / "web"
    (web / "data").mkdir(parents=True)
    shutil.copy(ROOT / "web" / "index.html", web / "index.html")
    (web / "secret.txt").write_text("inside web")
    (tmp_path / "outside.txt").write_text("outside web")
    stores_yaml = tmp_path / "stores.yaml"
    stores_yaml.write_text(yaml.safe_dump({"defaults": {"stale_after_days": 3}, "stores": [
        {"id": "hukut", "name": "Hukut", "url": "https://hukut.com", "authorized": False, "enabled": True,
         "start_urls": ["https://hukut.com/iphone"]},
        {"id": "evostore", "name": "Evo Store", "url": "https://evostore.com.np", "authorized": True}]}))
    db = DB(tmp_path / "prices.db")
    db.record("hukut", classify("iPhone 17 256GB"), "Apple iPhone 17 256GB", "https://hukut.com/iphone-17", 149999, None, True)
    db.record("evostore", classify("iPhone 17 256GB"), "iPhone 17 256GB", "https://evostore.com.np/iphone-17", 152000, None, True)
    db.commit()
    a = adm.Admin(tmp_path / "prices.db", web, stores_yaml, tmp_path / "shop_info.yaml",
                  tmp_path / "overrides.yaml", tmp_path / "models.yaml")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), adm.Handler)
    httpd.admin, httpd.token = a, "test-token"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def call(path, body=None, token="test-token", host=None, origin=None, ctype="application/json"):
        headers = {"Host": host or f"127.0.0.1:{port}"}
        if token:
            headers["X-Admin-Token"] = token
        if origin:
            headers["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = ctype
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw.decode())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode(errors="replace")

    yield call, a, tmp_path, port
    httpd.shutdown()


def _png(w, h, bg="white", phone=(40, 40, 46)):
    im = Image.new("RGB", (w, h), bg)
    ImageDraw.Draw(im).rounded_rectangle([w // 3, h // 8, 2 * w // 3, 7 * h // 8], 30, fill=phone)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_security_rules(panel):
    call, a, tmp, port = panel
    code, page = call("/", token=None)
    assert code == 200 and "test-token" in page and "__ADMIN_TOKEN__" not in page
    assert call("/", token=None, host="evil.example:8765")[0] == 403           # DNS rebinding
    assert call("/api/state", token=None)[0] == 403                            # no token
    assert call("/api/state", token="wrong")[0] == 403
    assert call("/api/state")[0] == 200
    assert call("/api/shop", {"id": "hukut"}, origin="https://evil.example")[0] == 403   # another website
    assert call("/api/shop", {"id": "hukut"}, ctype="text/plain")[0] == 403              # form-style post
    assert call("/preview/secret.txt", token=None)[0] == 200
    assert call("/preview/../outside.txt", token=None)[0] == 404              # can't leave web/
    assert call("/preview/%2e%2e/outside.txt", token=None)[0] == 404


def test_shops_details_branches_and_new_shop(panel):
    call, a, tmp, port = panel
    code, r = call("/api/shop", {"id": "hukut", "name": "Hukut", "url": "https://hukut.com", "authorized": True,
                                 "enabled": True, "details": {"warranty": "1 year", "phone": "01-555 <b>",
                                 "locations": [{"name": "New Road", "city": "Kathmandu", "map": "https://maps.app.goo.gl/x"},
                                               {"name": "", "city": "skipped"}, {"name": "Lakeside", "city": "Pokhara"}]}})
    assert code == 200 and r["message"] == "saved"
    defaults, stores = cfgmod.load_config(tmp / "stores.yaml", tmp / "shop_info.yaml")
    hukut = next(s for s in stores if s["id"] == "hukut")
    assert hukut["authorized"] is True and hukut["start_urls"] == ["https://hukut.com/iphone"]   # scraping settings kept
    assert hukut["details"] == {"warranty": "1 year", "phone": "01-555",
                                "locations": [{"name": "New Road", "city": "Kathmandu", "map": "https://maps.app.goo.gl/x"},
                                              {"name": "Lakeside", "city": "Pokhara"}]}
    assert "authorized: false" in (tmp / "stores.yaml").read_text()                            # hand-written file untouched
    latest = json.loads((tmp / "web" / "data" / "latest.json").read_text())                    # preview rebuilt
    assert next(s for s in latest["stores"] if s["id"] == "hukut")["details"]["warranty"] == "1 year"

    assert call("/api/shop", {"id": "Bad Id!", "name": "X", "url": "https://x.np"})[0] == 400
    assert call("/api/shop", {"id": "newshop", "name": "New Shop"})[0] == 400                  # needs a website
    assert call("/api/shop", {"id": "newshop", "name": "New Shop", "url": "http://x.np"})[0] == 400   # https only
    code, r = call("/api/shop", {"id": "newshop", "name": "New Shop", "url": "https://newshop.com.np/"})
    assert code == 200 and r["new"] is True
    new = next(s for s in cfgmod.load_config(tmp / "stores.yaml", tmp / "shop_info.yaml")[1] if s["id"] == "newshop")
    assert new["discover"] is True and new["url"] == "https://newshop.com.np"
    code, state = call("/api/state")
    assert [s["id"] for s in state["stores"]] == ["hukut", "evostore", "newshop"]


def test_logos(panel):
    call, a, tmp, port = panel
    assert call("/api/shop/logo", {"id": "hukut", "filename": "h.png", "data": _png(120, 120)})[0] == 200
    assert (tmp / "web" / "logos" / "hukut.png").exists()
    assert cfgmod.read_shop_info(tmp / "shop_info.yaml")["hukut"]["logo"] == "logos/hukut.png"
    bad_svg = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>').decode()
    assert call("/api/shop/logo", {"id": "hukut", "filename": "h.svg", "data": bad_svg})[0] == 400
    assert call("/api/shop/logo", {"id": "hukut", "filename": "h.exe", "data": bad_svg})[0] == 400
    assert call("/api/shop/logo", {"id": "nobody", "filename": "h.png", "data": _png(10, 10)})[0] == 400
    assert call("/api/shop/logo/remove", {"id": "hukut"})[0] == 200
    assert not (tmp / "web" / "logos" / "hukut.png").exists()


def test_prices_hide_and_show_again(panel):
    call, a, tmp, port = panel
    code, r = call("/api/prices?model=17")
    assert r["model"] == "iPhone 17" and [x["store"] for x in r["rows"]] == ["hukut", "evostore"]
    assert all(x["hidden"] is None for x in r["rows"])
    assert call("/api/hide", {"store": "hukut", "model": "iPhone 17", "storage_gb": 256, "reason": "wrong size",
                              "until": "2099-01-01"})[0] == 200
    r = call("/api/prices?model=iPhone%2017")[1]
    assert r["rows"][0]["hidden"] == "Hidden by you: wrong size"
    rules = call("/api/state")[1]["rules"]
    assert rules == [{"index": 0, "store": "hukut", "model": "iPhone 17", "storage": "256 GB", "until": "2099-01-01",
                      "reason": "wrong size", "active": True}]
    latest = json.loads((tmp / "web" / "data" / "latest.json").read_text())
    assert [o["store"] for o in latest["variants"][0]["offers"]] == ["evostore"]                # gone from the site
    assert call("/api/hide", {"store": "nobody", "reason": "x"})[0] == 400
    assert call("/api/unhide", {"index": 5})[0] == 400
    assert call("/api/unhide", {"index": 0})[0] == 200
    assert call("/api/state")[1]["rules"] == []


def test_images(panel):
    call, a, tmp, port = panel
    code, r = call("/api/image", {"model": "iPhone 17", "filename": "b.png", "data": _png(1600, 700, "black")})
    assert code == 409 and any("dark background" in p for p in r["problems"])                  # refused, nothing saved
    assert not (tmp / "web" / "images").exists() or not any((tmp / "web" / "images").rglob("*.webp"))
    code, r = call("/api/image", {"model": "iPhone 17", "filename": "b.png", "data": _png(1600, 700, "black"), "force": True})
    assert code == 200 and r["image"] == "images/models/iphone-17.webp"
    imgs = call("/api/images")[1]["models"]
    assert imgs == [{"model": "iPhone 17", "own": "images/models/iphone-17.webp", "credit": "", "store_image": None}]
    assert call("/api/image", {"model": "iPhone 17", "url": "file:///etc/passwd"})[0] == 400
    assert call("/api/image/remove", {"model": "iPhone 17"})[0] == 200
    assert call("/api/image/remove", {"model": "iPhone 17"})[0] == 400


def test_jobs(panel, monkeypatch):
    call, a, tmp, port = panel
    assert call("/api/job", {"kind": "scrape", "store": "nobody"})[0] == 400
    assert call("/api/job", {"kind": "probe", "url": "not a url"})[0] == 400
    assert call("/api/job", {"kind": "rm -rf"})[0] == 400
    # a job's output is streamed, and only one job runs at a time
    import subprocess, sys, time
    real = subprocess.Popen
    monkeypatch.setattr(adm.subprocess, "Popen", lambda cmd, **kw: real(
        [sys.executable, "-c", "import time\nfor i in range(3): print('line', i, flush=True); time.sleep(0.2)"], **kw))
    assert call("/api/job", {"kind": "scrape", "store": "hukut"})[0] == 200
    assert call("/api/job", {"kind": "scrape", "store": "hukut"})[0] == 409
    for _ in range(50):
        job = call("/api/job?since=0")[1]["job"]
        if job["done"]:
            break
        time.sleep(0.1)
    assert job["done"] and job["code"] == 0 and job["lines"] == ["line 0", "line 1", "line 2"]
    assert call("/api/job?since=2")[1]["job"]["lines"] == ["line 2"]
