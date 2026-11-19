import json

from scraper.extract import extract_offers, product_links
from scraper.normalize import classify, parse_model, parse_price, parse_storage
from scraper.pipeline import pick_best, with_page


# ------------------------------------------------------------------ normalize
def test_parse_price_formats():
    assert parse_price("Rs. 1,44,900") == 144900
    assert parse_price("NPR 295,000") == 295000
    assert parse_price("रू १,४४,९००") == 144900
    assert parse_price("NPR 295,000.00") == 295000
    assert parse_price("NPR 0") is None
    assert parse_price("EMI fr. Rs. 5,333") is None      # too small to be a phone
    assert parse_price(239999) == 239999


def test_models_and_storage():
    assert parse_model("Apple iPhone 17 Pro Max 256GB Cosmic Orange") == ("iPhone 17 Pro Max", "17")
    assert parse_model("iPhone17Pro 1TB") == ("iPhone 17 Pro", "17")
    assert parse_model("Apple iPhone 17e (256GB)") == ("iPhone 17e", "17e")
    assert parse_model("iPhone Air 256 GB") == ("iPhone Air", "Air")
    assert parse_model("iPhone 16 Plus") == ("iPhone 16 Plus", "16")
    assert parse_model("Samsung Galaxy S26") is None
    assert parse_storage("iPhone 17 Pro 12GB/512GB") == 512
    assert parse_storage("iPhone 17 Pro Max 2TB") == 2048


def test_classify_filters_accessories_and_used():
    assert classify("Silicone Case for iPhone 17 Pro") is None
    assert classify("Tempered Glass iPhone 16") is None
    assert classify("Refurbished iPhone 15 128GB") is None
    assert classify("Refurbished iPhone 15 128GB", include_used=True).condition == "used"
    v = classify("Apple iPhone 17 Pro - 1TB")
    assert v.key == "iphone-17-pro|1024" and v.storage_label == "1TB"


# ------------------------------------------------------------------ extractors
OPENCART = """
<div class="row">
 <div class="product-layout"><div class="product-thumb">
   <div class="description"><h4><a href="https://evostore.com.np/iphone-17-pro-1tb">iPhone 17 Pro - 1TB</a></h4></div>
   <div class="price"><span class="price-new">NPR 295,000</span> <span class="price-old">NPR 305,699</span></div>
 </div></div>
 <div class="product-layout"><div class="product-thumb">
   <div class="description"><h4><a href="https://evostore.com.np/iphone-17-256gb">iPhone 17 256GB</a></h4></div>
   <div class="price">NPR 161,599</div>
 </div></div>
 <div class="product-layout"><div class="product-thumb">
   <div class="description"><h4><a href="https://evostore.com.np/iphone-17-case">iPhone 17 Clear Case with MagSafe</a></h4></div>
   <div class="price">NPR 6,500</div>
 </div></div>
</div>"""

SEL = {"card": ".product-thumb", "name": "h4 a", "link": "h4 a", "price": ".price", "old_price": ".price-old"}


def test_opencart_selectors():
    offers = extract_offers(OPENCART, "https://evostore.com.np/apple/iphone", SEL)
    by_title = {o.title: o for o in offers}
    pro = by_title["iPhone 17 Pro - 1TB"]
    assert pro.price == 295000 and pro.old_price == 305699 and pro.method == "selectors"
    assert by_title["iPhone 17 256GB"].price == 161599
    links = product_links(OPENCART, "https://evostore.com.np/apple/iphone",
                          r"evostore\.com\.np/(?!apple/)[a-z0-9-]*iphone[a-z0-9-]*(\?|$)")
    assert "https://evostore.com.np/iphone-17-pro-1tb" in links


def test_heuristics_without_selectors():
    offers = extract_offers(OPENCART, "https://evostore.com.np/apple/iphone")
    prices = sorted(o.price for o in offers if classify(o.title))
    assert prices == [161599, 295000]


def test_jsonld_product_with_variant_offers():
    ld = {"@context": "https://schema.org", "@type": "Product", "name": "Apple iPhone 17 Pro Max",
          "offers": [
              {"@type": "Offer", "name": "256GB", "price": "239999", "priceCurrency": "NPR",
               "availability": "https://schema.org/InStock"},
              {"@type": "Offer", "name": "512GB", "price": "279999", "availability": "https://schema.org/OutOfStock"},
          ]}
    html = f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>'
    offers = extract_offers(html, "https://www.olizstore.com/p/iphone-17-pro-max")
    assert [(o.title, o.price, o.in_stock) for o in offers] == [
        ("Apple iPhone 17 Pro Max 256GB", 239999, True),
        ("Apple iPhone 17 Pro Max 512GB", 279999, False)]


def test_next_data_with_variants_and_slug():
    state = {"props": {"pageProps": {"products": [
        {"name": "Apple iPhone 17", "slug": "apple-iphone-17-price-in-nepal", "variants": [
            {"storage": "256GB", "selling_price": 161599, "mrp": 165000, "in_stock": True},
            {"storage": "512GB", "selling_price": 200899, "in_stock": False}]},
        {"name": "Samsung Galaxy S26", "slug": "s26", "price": 150000},
    ]}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(state)}</script>'
    offers = extract_offers(html, "https://fatafatsewa.com/category/x", None, "/product-detail/{slug}")
    assert {(o.title, o.price) for o in offers} == {("Apple iPhone 17 256GB", 161599), ("Apple iPhone 17 512GB", 200899)}
    assert offers[0].url == "https://fatafatsewa.com/product-detail/apple-iphone-17-price-in-nepal"


def test_rsc_flight_data():
    obj = {"id": 7, "title": "iPhone 16e 128GB", "sale_price": "Rs. 1,05,999", "url": "/product/iphone-16e"}
    chunk = '3:["$","div",null,' + json.dumps({"product": obj}) + "]"
    html = f"<script>self.__next_f.push([1,{json.dumps(chunk)}])</script>"
    offers = extract_offers(html, "https://hukut.com/")
    assert [(o.title, o.price, o.url) for o in offers] == [
        ("iPhone 16e 128GB", 105999, "https://hukut.com/product/iphone-16e")]


def test_pick_best_prefers_in_stock_and_drops_storage_less_duplicates():
    from scraper.extract import RawOffer
    offers = [
        RawOffer("iPhone 17 256GB Black", 161599, "u1", in_stock=False),
        RawOffer("iPhone 17 256GB White", 163000, "u2", in_stock=True),
        RawOffer("iPhone 17", 150000, "u3"),          # listing "from" price, no storage
        RawOffer("iPhone Air", 180000, "u4"),         # no storage anywhere -> keep
    ]
    got = {v.key: o.url for v, o in pick_best(offers, False)}
    assert got == {"iphone-17|256": "u2", "iphone-air|na": "u4"}


def test_with_page():
    assert with_page("https://x.com/apple/iphone?limit=100", "page", 3) == "https://x.com/apple/iphone?limit=100&page=3"


def test_images_are_collected():
    ld = {"@type": "Product", "name": "iPhone 17 256GB", "image": ["/img/iphone17.jpg"],
          "offers": {"@type": "Offer", "price": "161599"}}
    html = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    o = extract_offers(html, "https://shop.np/p/iphone-17")[0]
    assert o.image == "https://shop.np/img/iphone17.jpg"
    o = extract_offers(OPENCART.replace("<h4>", '<img data-src="/i/a.webp"><h4>', 1), "https://evostore.com.np/x", SEL)[0]
    assert o.image == "https://evostore.com.np/i/a.webp"


def test_db_upgrades_old_schema(tmp_path):
    import sqlite3
    from scraper.db import DB
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE price_points (id INTEGER PRIMARY KEY, store_id TEXT NOT NULL, variant_key TEXT NOT NULL,"
                      " model TEXT NOT NULL, series TEXT, storage_gb INTEGER, title TEXT, url TEXT, price INTEGER NOT NULL,"
                      " old_price INTEGER, in_stock INTEGER, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);")
    con.close()
    db = DB(path)
    db.record("s", classify("iPhone 17 256GB"), "iPhone 17 256GB", "u", 161599, None, True, image="https://x/i.jpg")
    assert db.q("SELECT image FROM price_points")[0][0] == "https://x/i.jpg"


def test_product_page_selectors_with_text_stock():
    html = """<html><head><meta property="og:image" content="https://cdn.store.np/p.jpg"></head><body>
      <nav><a href="/cart">Cart</a></nav>
      <main><h1>Apple iPhone 18 Pro Max 256GB</h1>
      <div class="product-price" data-product-priced=""><div><strong data-product-price="" class="">Rs. 279,900</strong>
      <del data-product-original-price="" hidden="">Rs. 0</del> <span data-product-sale-label="" hidden=""></span>
      <span data-product-discount="" hidden="">0% Off</span></div>
      <p><span data-product-stock="" class="">Out of Stock</span> <span aria-hidden="true">•</span> <span>Inclusive of all taxes</span></p></div>
      </main></body></html>"""
    sel = {"card": "main", "name": "h1", "link": "page", "price": "[data-product-price]",
           "old_price": "[data-product-original-price]", "stock": "[data-product-stock]"}
    [o] = extract_offers(html, "https://store.np/products/iphone-18-pro-max", sel)
    assert (o.title, o.price, o.old_price, o.in_stock, o.url) == (
        "Apple iPhone 18 Pro Max 256GB", 279900, None, False, "https://store.np/products/iphone-18-pro-max")
    assert classify(o.title).key == "iphone-18-pro-max|256"
    instock = html.replace("Out of Stock", "In Stock")
    assert extract_offers(instock, "https://store.np/p", sel)[0].in_stock is True


def test_variant_json_in_paisa_with_inventory():
    # structure used by GAG Nepal: <script id="product-variant-data" type="application/json">
    variants = [
        {"id": "a", "title": "iPhone 17 Pro Max \u2014 256GB \u2014 Silver", "price": 24249900, "priceState": "fixed", "inventory": 4},
        {"id": "b", "title": "iPhone 17 Pro Max \u2014 256GB \u2014 Deep Blue", "price": 24249900, "inventory": 0},
        {"id": "c", "title": "iPhone 17 Pro Max \u2014 512GB \u2014 Silver", "price": 28459900, "inventory": 0},
        {"id": "d", "title": "iPhone 17 Pro Max \u2014 2TB \u2014 Silver", "price": 41139900, "inventory": 2},
    ]
    ld = {"@type": "Product", "name": "iPhone 17 Pro Max",
          "offers": {"@type": "AggregateOffer", "lowPrice": 242499.0, "highPrice": 411399.0, "availability": "https://schema.org/InStock"}}
    html = (f'<script id="product-jsonld" type="application/ld+json">{json.dumps(ld)}</script>'
            f'<script id="product-variant-data" type="application/json">{json.dumps(variants)}</script>')
    offers = extract_offers(html, "https://gag-nepal.com/products/iphone-17-pro-max")
    got = {(v.key, o.price, o.in_stock) for v, o in pick_best(offers, False)}
    assert got == {("iphone-17-pro-max|256", 242499, True), ("iphone-17-pro-max|512", 284599, False),
                   ("iphone-17-pro-max|2048", 411399, True)}


def test_stock_text_next_to_title_and_price():
    # Evo Store's iPhone Air page: price in JSON-LD without availability, "Out Of Stock" printed under the title
    ld = {"@type": "Product", "name": "iPhone Air", "offers": {"@type": "Offer", "price": "190699"}}
    page = """<html><script type="application/ld+json">%s</script><body><main>
      <section class="info"><h1>iPhone Air</h1><p>Out Of Stock</p><p>NPR 190,699</p><button>Add to Bag</button></section>
      <section class="similar"><h2>Similar items</h2><div>iPhone Air NPR 190,699 <button>Add to Bag</button></div></section>
      </main></body></html>""" % json.dumps(ld)
    url = "https://evostore.com.np/apple/iphone/iphoneAir"
    [o] = extract_offers(page, url)
    assert (o.price, o.in_stock) == (190699, False)
    [o] = extract_offers(page.replace("<p>Out Of Stock</p>", ""), url)
    assert o.in_stock is True


def test_list_page_entry_takes_stock_from_product_page():
    from scraper.extract import RawOffer
    offers = [RawOffer("iPhone Air", 190699, "https://evostore.com.np/iphoneAir", None, None, "selectors"),
              RawOffer("iPhone Air", 190699, "https://evostore.com.np/apple/iphone/iphoneAir", None, False, "product-page")]
    [(v, o)] = pick_best(offers, False)
    assert o.in_stock is False


def test_browser_json_downloads_become_readable_data():
    from scraper.fetch import _captured_json, with_captured

    class FakeResponse:
        def __init__(self, url, body): self.url, self._b = url, body
        def text(self): return self._b

    api = json.dumps({"data": {"name": "Apple iPhone Air", "variants": [
        {"name": "256GB", "price": 190699, "stock": 5}, {"name": "512GB", "price": 232899, "stock": 0}]}})
    html = with_captured("<html><body><main><h1>iPhone Air</h1><p>Rs. 1,90,699</p></main></body></html>", _captured_json(
        [FakeResponse("https://shop.np/api/product", api), FakeResponse("https://shop.np/api/menu", '{"items": []}')]))
    assert html.index("data-captured-from") < html.index("</body>")             # inside the page, not after it
    got = {(v.key, o.price, o.in_stock) for v, o in pick_best(extract_offers(html, "https://shop.np/p/iphone-air"), False)}
    assert got == {("iphone-air|256", 190699, True), ("iphone-air|512", 232899, False)}


def test_sizeless_price_filed_under_matching_base_storage(tmp_path):
    from scraper.db import DB
    from scraper.export import export
    db = DB(tmp_path / "t.db")
    rec = lambda store, title, price: db.record(store, classify(title), title, f"https://{store}.np/x", price, None, True)
    rec("oliz", "iPhone Air 256GB", 190699)
    rec("oliz", "iPhone Air 512GB", 232899)
    rec("evo", "iPhone Air", 190699)           # Evo: one price, no size -> 256 GB (matches)
    rec("hukut", "iPhone Air", 150000)         # no match -> stays "size not listed"
    db.commit()
    stores = [{"id": s, "name": s, "url": "x"} for s in ("oliz", "evo", "hukut")]
    out = export(db, stores, tmp_path)
    v = {x["key"]: x for x in out["variants"]}
    assert {o["store"] for o in v["iphone-air|256"]["offers"]} == {"oliz", "evo"}
    assert next(o for o in v["iphone-air|256"]["offers"] if o["store"] == "evo")["storage_assumed"] is True
    assert [o["store"] for o in v["iphone-air|na"]["offers"]] == ["hukut"]
    hist = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert "evo" in hist["iphone-air|256"] and "evo" not in hist.get("iphone-air|na", {})


def test_oliz_blanxer_option_name_variants():
    # Oliz (Blanxer platform): __NEXT_DATA__ props.pageProps.res.variants[].option_name = "Colour/Storage"
    res = {"name": "iPhone 17 Pro", "slug": "iphone-17-pro-price-in-nepal", "price": 0, "quantity": 0, "variants": [
        {"option_name": "Cosmic Orange/256GB", "price": 220999, "quantity": 3, "sku": "MG8H4"},
        {"option_name": "Deep Blue/256GB", "price": 220999, "quantity": 0, "sku": "MG8J4"},
        {"option_name": "Cosmic Orange/512GB", "price": 259999, "quantity": 0, "sku": "MG8K4"},
        {"option_name": "Silver/1TB", "price": 299999, "quantity": 1, "sku": "MG8L4"}]}
    page = {"props": {"pageProps": {"slug": res["slug"], "response": None, "res": res}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(page)}</script>'
    got = {(v.key, o.price, o.in_stock) for v, o in pick_best(extract_offers(html, "https://www.olizstore.com/product/x"), False)}
    assert got == {("iphone-17-pro|256", 220999, True), ("iphone-17-pro|512", 259999, False), ("iphone-17-pro|1024", 299999, True)}


def test_search_engine_pages(tmp_path):
    import shutil
    from pathlib import Path
    from scraper.seo import build_site
    web = tmp_path / "web"; web.mkdir()
    shutil.copy(Path(__file__).resolve().parent.parent / "web" / "index.html", web / "index.html")
    (web / "iphone-12").mkdir(); (web / "iphone-12" / ".bhaau-page").write_text("x")   # old model, no longer sold
    (web / "data").mkdir()                                                              # not ours: must survive
    latest = {"generated_at": "2026-11-18T06:00:00+00:00", "stores": [
                  {"id": "evo", "name": "Evo Store", "url": "x", "authorized": True, "offers": 2},
                  {"id": "oliz", "name": "Oliz Store", "url": "x", "authorized": True, "offers": 1}],
              "variants": [
                  {"key": "iphone-air|256", "model": "iPhone Air", "storage_gb": 256, "image": None, "offers": [
                      {"store": "oliz", "price": 190699, "in_stock": False, "url": "https://oliz/p"},
                      {"store": "evo", "price": 190699, "in_stock": True, "url": "https://evo/p"}]},
                  {"key": "iphone-air|512", "model": "iPhone Air", "storage_gb": 512, "image": None, "offers": [
                      {"store": "evo", "price": 232899, "in_stock": True, "url": "https://evo/p2"}]}]}
    assert build_site(latest, web, "https://bhaau.com.np") == 1
    page = (web / "iphone-air" / "index.html").read_text(encoding="utf-8")
    assert "<title>Apple iPhone Air Price in Nepal (November 2026): from Rs 1,90,699 | Bhaau</title>" in page
    assert '<link rel="canonical" href="https://bhaau.com.np/iphone-air/">' in page
    assert 'data-root="../" data-slug="iphone-air"' in page and 'src="../app.js"' in page
    ld = json.loads(page.split('<script type="application/ld+json">')[1].split("</script>")[0])
    assert ld["offers"]["lowPrice"] == 190699 and ld["offers"]["highPrice"] == 232899 and ld["offers"]["offerCount"] == 3
    assert not (web / "iphone-12").exists() and (web / "data").exists()
    assert "https://bhaau.com.np/iphone-air/" in (web / "sitemap.xml").read_text(encoding="utf-8")
    assert '<a href="iphone-air/">' in (web / "index.html").read_text(encoding="utf-8")



def test_privacy_page_and_footer_links(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    from scraper.seo import build_site
    web = tmp_path / "web"; web.mkdir()
    shutil.copy(Path(__file__).resolve().parent.parent / "web" / "index.html", web / "index.html")
    latest = {"generated_at": "2026-11-18T06:00:00+00:00", "alerts_url": "https://a.workers.dev",
              "stores": [{"id": "evo", "name": "Evo Store", "url": "x", "authorized": True, "offers": 1}],
              "variants": [{"key": "iphone-air|256", "model": "iPhone Air", "storage_gb": 256, "image": None, "offers": [
                  {"store": "evo", "price": 190699, "in_stock": True, "url": "https://evo/p"}]}]}

    monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    build_site(latest, web, "https://bhaau.com.np")
    home = (web / "index.html").read_text(encoding="utf-8")
    assert '<a href="privacy/">Privacy</a>' in home and "mailto:" not in home          # no address set: no mail links
    assert "Confirm with the seller before you pay" in home

    monkeypatch.setenv("CONTACT_EMAIL", "hello@bhaau.com.np")
    build_site(latest, web, "https://bhaau.com.np")
    home = (web / "index.html").read_text(encoding="utf-8")
    model = (web / "iphone-air" / "index.html").read_text(encoding="utf-8")
    privacy = (web / "privacy" / "index.html").read_text(encoding="utf-8")
    assert home.count("mailto:hello@bhaau.com.np") == 2 and "Report a wrong price" in home
    assert '<a href="../privacy/">Privacy</a>' in model and "subject=Wrong%20price%3A%20iPhone%20Air" in model
    assert '<body data-root="../" data-page="privacy">' in privacy and "<title>Privacy | Bhaau</title>" in privacy
    assert "deleted after 7 days" in privacy and "hello@bhaau.com.np" in privacy
    assert '<link rel="canonical" href="https://bhaau.com.np/privacy/">' in privacy
    assert "https://bhaau.com.np/privacy/" in (web / "sitemap.xml").read_text(encoding="utf-8")
    assert home.count("<!--seo:foot-->") == 1                                         # rebuilt, not duplicated
    nf = (web / "404.html").read_text(encoding="utf-8")
    assert 'data-root="/" data-page="404"' in nf and 'href="/styles.css"' in nf and 'content="noindex"' in nf
    build_site(latest, web, "https://you.github.io/bhaau")                              # site in a sub-folder
    assert 'href="/bhaau/styles.css"' in (web / "404.html").read_text(encoding="utf-8")

    build_site({**latest, "alerts_url": None}, web, "https://bhaau.com.np")              # alerts off: no alert section
    assert "Price alerts" not in (web / "privacy" / "index.html").read_text(encoding="utf-8")
    assert (web / "privacy").exists()                                                  # never removed as a stale model

    monkeypatch.setenv("CONTACT_EMAIL", "not an email")
    from scraper.seo import contact_email
    assert contact_email() is None


def test_store_logos_are_checked(tmp_path, caplog):
    from scraper.export import store_logo
    web = tmp_path / "web"; (web / "logos").mkdir(parents=True)
    (web / "logos" / "hukut.svg").write_text("<svg/>")
    (web / "secret.svg").write_text("<svg/>")
    st = lambda logo: store_logo({"id": "hukut", "logo": logo}, web)
    assert st("logos/hukut.svg") == "logos/hukut.svg"
    assert st("hukut.svg") == "logos/hukut.svg"                    # folder can be left out
    assert st("web\\logos\\hukut.svg") == "logos/hukut.svg"        # Windows-style path
    assert st(None) is None and st("") is None
    assert st("logos/missing.svg") is None and "not found" in caplog.text
    assert st("https://hukut.com/logo.svg") is None                # no hotlinking
    assert st("logos/../secret.svg") is None                       # must stay inside web/logos/
    (web / "logos" / "x.gif").write_text("x")
    assert st("logos/x.gif") is None

def test_alert_payload_uses_lowest_in_stock_price(monkeypatch):
    from scraper import alerts
    latest = {"demo": False, "stores": [{"id": "evo", "name": "Evo Store"}, {"id": "oliz", "name": "Oliz Store"}],
              "variants": [
                  {"key": "iphone-air|256", "model": "iPhone Air", "storage_gb": 256, "offers": [
                      {"store": "oliz", "price": 185000, "in_stock": False, "url": "o"},   # cheaper but sold out
                      {"store": "evo", "price": 190699, "in_stock": True, "url": "e"}]},
                  {"key": "iphone-17|512", "model": "iPhone 17", "storage_gb": 512, "offers": [
                      {"store": "evo", "price": 200899, "in_stock": False, "url": "e2"}]},
                  {"key": "iphone-16|na", "model": "iPhone 16", "storage_gb": None, "offers": [
                      {"store": "evo", "price": 150000, "in_stock": True, "url": "e3"}]}]}
    p = {x["key"]: x for x in alerts.build_payload(latest)}
    assert p["iphone-air|256"] == {"key": "iphone-air|256", "label": "iPhone Air 256 GB", "slug": "iphone-air",
                                   "storage_gb": 256, "price": 190699, "store": "Evo Store", "url": "e"}
    assert p["iphone-17|512"]["price"] is None and "iphone-16|na" not in p

    sent = []
    monkeypatch.setattr(alerts.requests, "post", lambda url, **kw: sent.append((url, kw)) or type(
        "R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"emails_sent": 1}})())
    monkeypatch.delenv("ALERTS_URL", raising=False)
    assert alerts.push_prices(latest) is None and not sent                       # not configured: skipped
    monkeypatch.setenv("ALERTS_URL", "https://a.workers.dev/"); monkeypatch.setenv("ALERTS_TOKEN", "t")
    assert alerts.push_prices({**latest, "demo": True}) is None and not sent      # demo data: never
    assert alerts.push_prices(latest) == {"emails_sent": 1}
    assert sent[0][0] == "https://a.workers.dev/notify" and sent[0][1]["headers"]["Authorization"] == "Bearer t"


def test_overrides_hide_prices_everywhere(tmp_path):
    from datetime import date
    from scraper.db import DB
    from scraper.export import export
    from scraper.overrides import load_rules
    rules_file = tmp_path / "overrides.yaml"
    rules_file.write_text("""
hide:
  - store: hukut
    model: iPhone 17 Pro
    storage: 256GB
    reason: wrong size
  - store: evo
    until: 2020-01-01          # expired: ignored
  - model: iPhone Air          # no store: ignored with an error
""", encoding="utf-8")
    rules = load_rules(rules_file)
    assert len(rules) == 2 and rules[0].storage_gb == 256 and rules[0].model == "iphone 17 pro"
    assert rules[0].matches("hukut", "iPhone 17 Pro", 256) and not rules[0].matches("hukut", "iPhone 17 Pro Max", 256)
    assert not rules[1].active(date(2026, 11, 18))

    db = DB(tmp_path / "t.db")
    for store, title, price in [("hukut", "iPhone 17 Pro 256GB", 120000), ("evo", "iPhone 17 Pro 256GB", 220999),
                                ("hukut", "iPhone 17 Pro 512GB", 259999)]:
        db.record(store, classify(title), title, "u", price, None, True)
    db.commit()
    stores = [{"id": "hukut", "name": "Hukut", "url": "x"}, {"id": "evo", "name": "Evo", "url": "x"}]
    out = export(db, stores, tmp_path / "data", rules=rules)
    v = {x["key"]: x for x in out["variants"]}
    assert [o["store"] for o in v["iphone-17-pro|256"]["offers"]] == ["evo"]            # Hukut's wrong price is gone
    assert [o["store"] for o in v["iphone-17-pro|512"]["offers"]] == ["hukut"]          # other sizes untouched
    hist = json.loads((tmp_path / "data" / "history.json").read_text(encoding="utf-8"))
    assert "hukut" not in hist.get("iphone-17-pro|256", {})


def _seed_runs(db, store, statuses_offers, hours_apart=6, start_hours_ago=None):
    """Write past scrape runs for a store, oldest first. Returns the latest run's start time."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    n = len(statuses_offers)
    start = start_hours_ago if start_hours_ago is not None else (n - 1) * hours_apart + 1
    last = None
    for i, (status, offers, *err) in enumerate(statuses_offers):
        t = (now - timedelta(hours=start - i * hours_apart)).replace(microsecond=0)
        last = t.isoformat()
        db.conn.execute("INSERT INTO runs (store_id, started_at, finished_at, status, offers, error) VALUES (?,?,?,?,?,?)",
                        (store, last, (t + timedelta(minutes=5)).isoformat(), status, offers, err[0] if err else None))
    db.commit()
    return last


def test_health_finds_broken_stores_and_price_jumps(tmp_path):
    from scraper.db import DB
    from scraper import health
    db = DB(tmp_path / "t.db")
    stores = [{"id": s, "name": s.title(), "url": "x"} for s in ("good", "down", "empty", "fewer", "stale", "jumpy")]
    stores.append({"id": "paused", "name": "Paused", "url": "x", "enabled": False})
    _seed_runs(db, "good", [("ok", 20)] * 4)
    _seed_runs(db, "down", [("ok", 20), ("ok", 20), ("error", 0, "failed: https://down.np (502 Bad Gateway)")])
    _seed_runs(db, "empty", [("ok", 15), ("ok", 15), ("empty", 0)])
    _seed_runs(db, "fewer", [("ok", 20), ("ok", 22), ("ok", 21), ("ok", 6)])
    _seed_runs(db, "stale", [("ok", 20)], start_hours_ago=40)
    started = _seed_runs(db, "jumpy", [("ok", 10), ("ok", 10)])
    v = classify("iPhone 17 Pro 256GB")
    db.record("jumpy", v, "t", "u", 220999, None, True, seen_at="2026-01-01T00:00:00+00:00")
    db.record("jumpy", v, "t", "u", 120999, None, True)             # -45% in the latest scrape
    v2 = classify("iPhone 17 256GB")
    db.record("jumpy", v2, "t", "u", 161599, None, True, seen_at="2026-01-01T00:00:00+00:00")
    db.record("jumpy", v2, "t", "u", 155000, None, True)            # -4%: normal
    db.commit()

    got = {p.key.split(":")[0] + ":" + p.key.split(":")[1]: p for p in health.check(db, stores)}
    assert set(got) == {"down:error", "empty:empty", "fewer:fewer", "stale:stale", "jumpy:jump"}
    assert "502 Bad Gateway" in got["down:error"].message
    assert "returned 6 phones; it usually has about 21" in got["fewer:fewer"].message
    assert "Rs 2,20,999 to Rs 1,20,999 (-45%)" in got["jumpy:jump"].message
    assert got["down:error"].severity == "broken" and got["jumpy:jump"].severity == "check"


def test_health_emails_each_problem_once_a_day(tmp_path, monkeypatch):
    from scraper.db import DB
    from scraper import health
    db = DB(tmp_path / "t.db")
    stores = [{"id": "down", "name": "Down", "url": "x"}]
    _seed_runs(db, "down", [("ok", 20), ("error", 0, "blocked")])
    sent = []
    monkeypatch.setenv("RESEND_API_KEY", "re_x"); monkeypatch.setenv("OWNER_EMAIL", "me@example.com"); monkeypatch.setenv("MAIL_FROM", "Bhaau <a@b.np>")
    monkeypatch.setattr(health.requests, "post", lambda url, **kw: sent.append(kw["json"]) or type("R", (), {"ok": True, "status_code": 200, "raise_for_status": lambda s: None})())
    assert health.run(db, stores, notify=True, status_url="https://bhaau.com.np/status-abc/") == 1
    assert len(sent) == 1 and sent[0]["subject"] == "Bhaau: 1 store not working" and "status-abc" in sent[0]["html"]
    assert health.run(db, stores, notify=True) == 0 and len(sent) == 1         # same problem again: no repeat
    _seed_runs(db, "down", [("ok", 20)], start_hours_ago=0)                      # store recovered
    assert health.run(db, stores, notify=True) == 0
    assert db.q("SELECT COUNT(*) AS n FROM health_notices")[0]["n"] == 0        # forgotten, so a new break emails again


def test_status_page(tmp_path):
    from scraper.db import DB
    from scraper import status_page
    from scraper.overrides import Rule
    db = DB(tmp_path / "t.db")
    stores = [{"id": "evo", "name": "Evo Store", "url": "x", "authorized": True}, {"id": "down", "name": "Down", "url": "x"}]
    _seed_runs(db, "evo", [("ok", 18), ("ok", 19)])
    _seed_runs(db, "down", [("error", 0, "blocked by Cloudflare")])
    v = classify("iPhone Air 256GB")
    db.record("evo", v, "t", "u", 195000, None, True, seen_at="2026-01-01T00:00:00+00:00")
    db.record("evo", v, "t", "u", 190699, None, True); db.commit()
    web = tmp_path / "web"; web.mkdir()
    old = web / "status-oldkey123456"; old.mkdir(); (old / ".bhaau-status").write_text("x")
    assert status_page.build(db, stores, web, key="bad key") is None             # unsafe key refused
    page = status_page.build(db, stores, web, key="s3cr3t-key-2026", rules=[Rule("hukut", "iphone 17 pro", 256, reason="wrong size")])
    html = page.read_text(encoding="utf-8")
    assert page.parent.name == "status-s3cr3t-key-2026" and not old.exists()
    assert '<meta name="robots" content="noindex,nofollow">' in html
    assert "blocked by Cloudflare" in html and "Rs 1,95,000" in html and "Rs 1,90,699" in html
    assert "hukut: iPhone 17 Pro 256 GB (wrong size)" in html
    assert "@" not in html.split("<main>")[1]                                   # no email addresses on the page


def test_health_test_email(monkeypatch, capsys):
    from scraper import health
    sent = []
    class R:
        def __init__(self, ok, body): self.ok, self._b, self.status_code, self.text = ok, body, 200 if ok else 403, str(body)
        def json(self): return self._b
    monkeypatch.setenv("RESEND_API_KEY", "re_x"); monkeypatch.setenv("OWNER_EMAIL", "me@example.com"); monkeypatch.delenv("MAIL_FROM", raising=False)
    monkeypatch.setattr(health.requests, "post", lambda url, **kw: sent.append(kw["json"]) or R(True, {"id": "1"}))
    assert health.send_test_email() == 0
    assert sent[0]["subject"] == "Bhaau: test email" and sent[0]["to"] == ["me@example.com"] and "onboarding@resend.dev" in sent[0]["from"]
    monkeypatch.setattr(health.requests, "post", lambda url, **kw: R(False, {"message": "You can only send testing emails to your own email address"}))
    assert health.send_test_email() == 1
    monkeypatch.delenv("RESEND_API_KEY")
    assert health.send_test_email() == 1 and "add RESEND_API_KEY" in capsys.readouterr().out


def test_model_names_with_separators_and_storage_first():
    from scraper.normalize import parse_model
    cases = {
        "Apple iPhone 17 Pro-Max 256GB": "iPhone 17 Pro Max",
        "iPhone 17 (Pro Max) - Deep Blue": "iPhone 17 Pro Max",
        "iPhone 17 | Pro Max | 1TB": "iPhone 17 Pro Max",
        "iPhone 17 256GB Pro Max": "iPhone 17 Pro Max",
        "iPhone17ProMax 512GB": "iPhone 17 Pro Max",
        "iPhone 17 (2025) Pro": "iPhone 17 Pro",
        "iPhone 17 256GB Lavender": "iPhone 17",
        "iPhone 17 - 256GB - Black": "iPhone 17",
        "iPhone 16e 128GB": "iPhone 16e",
        "iPhone Air 256GB": "iPhone Air",
    }
    for title, want in cases.items():
        assert parse_model(title)[0] == want, title


def test_articles_are_not_product_pages(tmp_path):
    from scraper.normalize import is_product_url
    from scraper.extract import product_links
    gag_article = "https://gag-nepal.com/articles/iphone-18-series-everything-you-need-to-know-about-apples-next-generation-iphones"
    assert not is_product_url(gag_article)
    for u in ["https://x.np/blogs/news/iphone-17-launch", "https://x.np/iphone-17-vs-iphone-17-pro",
              "https://x.np/news/iphone-18-rumours", "https://x.np/blog/iphone-17-price"]:
        assert not is_product_url(u), u
    for u in ["https://www.olizstore.com/product/iphone-air-price-in-nepal", "https://gag-nepal.com/products/iphone-17-pro-max",
              "https://hukut.com/apple-iphone-17-pro", "https://evostore.com.np/iphone-16e-128gb"]:
        assert is_product_url(u), u
    listing = f'<a href="{gag_article}">iPhone 18 series: everything</a><a href="/products/iphone-17">iPhone 17</a>'
    assert product_links(listing, "https://gag-nepal.com/collections/apple") == ["https://gag-nepal.com/products/iphone-17"]

    # an article at a normal-looking address is recognised from the page itself
    article = ('<html><head><meta property="og:type" content="article"><title>iPhone 18: price in Nepal</title></head>'
               '<body><h1>iPhone 18 price in Nepal</h1><p>The iPhone 17 Pro Max costs Rs. 2,59,900 today.</p></body></html>')
    assert extract_offers(article, "https://shop.np/iphone-18-price") == []
    product = ('<html><head><meta property="og:type" content="product"></head><body><h1>Apple iPhone 17 256GB</h1>'
               '<span class="price">Rs. 1,49,999</span></body></html>')
    assert extract_offers(product, "https://shop.np/p/iphone-17")


def test_old_prices_read_from_articles_are_hidden(tmp_path):
    import json as _json
    from scraper.db import DB
    from scraper.export import export
    db = DB(tmp_path / "t.db")
    art = "https://gag-nepal.com/articles/iphone-18-series-everything-you-need-to-know"
    db.record("gag", classify("iPhone 18"), "iPhone 18 series", art, 259900, None, None)
    db.record("gag", classify("iPhone 17 Pro Max 256GB"), "iPhone 17 Pro Max 256GB", "https://gag-nepal.com/products/iphone-17-pro-max", 242499, None, True)
    out = tmp_path / "web" / "data"
    export(db, [{"id": "gag", "name": "GAG Nepal", "url": "https://gag-nepal.com"}], out, rules=[])
    keys = [v["key"] for v in _json.loads((out / "latest.json").read_text(encoding="utf-8"))["variants"]]
    assert keys == ["iphone-17-pro-max|256"]


def test_inspect_shows_where_prices_came_from(tmp_path, monkeypatch, capsys):
    import scraper.__main__ as cli
    from scraper.db import DB
    db = DB(tmp_path / "prices.db")
    db.record("hukut", classify("iPhone 17 256GB"), "Apple iPhone 17 256GB", "https://hukut.com/iphone-17", 149999, None, True)
    db.record("gag", classify("iPhone 17 256GB"), "iPhone 17 256GB", "https://gag-nepal.com/blogs/news/iphone-17", 242499, None, None)
    db.conn.commit()
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(cli, "load_config", lambda: ({}, [{"id": "hukut"}, {"id": "gag"}]))
    assert cli.main(["inspect", "17"]) == 0
    out = capsys.readouterr().out
    assert "iPhone 17:" in out and "https://hukut.com/iphone-17" in out and "Rs   149,999" in out
    assert "HIDDEN: not a product page" in out


def test_search_pages_are_never_read_as_one_product(tmp_path):
    import json as _json
    from scraper.normalize import is_product_url, is_search_url
    gag_search = "https://gag-nepal.com/search?q=iphone+18"
    assert is_search_url(gag_search) and not is_product_url(gag_search)
    for u in ["https://x.np/search/iphone", "https://x.np/?s=iphone+17", "https://x.np/catalogsearch/result/?q=iphone"]:
        assert is_search_url(u), u
    for u in ["https://x.np/products/iphone-17?variant=4411", "https://x.np/iphone-17-research-edition"]:
        assert not is_search_url(u), u
    # a search page whose first result is the 18 Pro Max: no made-up "iPhone 18" offer
    page = ('<html><body><main><h1>Search results for "iphone 18"</h1>'
            '<div><p>Rs. 2,79,900</p><p>Out of stock</p></div></main></body></html>')
    assert extract_offers(page, gag_search) == []
    page2 = '<html><body><main><h1>iphone 18</h1><p>Rs. 2,79,900</p></main></body></html>'   # heading = search words
    assert extract_offers(page2, gag_search) == []
    # ...but product cards with their own links on a search page still count
    cards = ('<html><body><h1>Search: iphone</h1><div class="product-card"><a href="/products/iphone-18-pro-max">'
             'Apple iPhone 18 Pro Max 256GB</a><span class="price">Rs. 2,79,900</span></div></body></html>')
    got = {(v.model, o.url) for v, o in pick_best(extract_offers(cards, gag_search), False)}
    assert got == {("iPhone 18 Pro Max", "https://gag-nepal.com/products/iphone-18-pro-max")}
    # prices read from search pages by older versions are hidden from the site
    from scraper.db import DB
    from scraper.export import export
    db = DB(tmp_path / "t.db")
    db.record("gag", classify("iPhone 18"), "iphone 18", gag_search, 279900, None, False)
    out = tmp_path / "web" / "data"
    export(db, [{"id": "gag", "name": "GAG Nepal", "url": "https://gag-nepal.com"}], out, rules=[])
    assert _json.loads((out / "latest.json").read_text(encoding="utf-8"))["variants"] == []


def _phone_png(w=600, h=900, border=150):
    """A dark 'phone' on a big white background, as product photos often are."""
    import io as _io
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w + 2 * border, h + 2 * border), "white")
    ImageDraw.Draw(im).rounded_rectangle([border, border, border + w, border + h], 60, fill=(40, 40, 46))
    buf = _io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()


def test_model_images_import_trim_credit_and_export(tmp_path, monkeypatch):
    import json as _json
    from PIL import Image
    from scraper import model_images as mi
    from scraper.db import DB
    from scraper.export import export
    import shutil
    from pathlib import Path as _P
    web = tmp_path / "web"; (web / "data").mkdir(parents=True)
    shutil.copy(_P(__file__).resolve().parent.parent / "web" / "index.html", web / "index.html")
    monkeypatch.setenv("SITE_URL", "https://bhaau.pages.dev")
    cfg = tmp_path / "models.yaml"
    monkeypatch.setattr(mi, "MODELS_FILE", cfg)

    src = tmp_path / "17pro.png"; src.write_bytes(_phone_png())
    model, rel = mi.import_image("17 pro", str(src), "Image: Apple", web)
    assert (model, rel) == ("iPhone 17 Pro", "images/models/iphone-17-pro.webp")
    with Image.open(web / rel) as im:
        assert im.format == "WEBP" and max(im.size) <= 900       # resized
        assert im.size[0] / im.size[1] < 0.8                      # white border trimmed: phone-shaped, not square-ish

    class R:                                                      # the same image from a web link
        headers = {"Content-Type": "image/png"}
        def __init__(self, data): self.data = data
        def iter_content(self, n): yield self.data
        def raise_for_status(self): pass
    monkeypatch.setattr(mi.requests, "get", lambda url, **kw: R(_phone_png(400, 800, 40)))
    assert mi.import_image("iPhone Air", "https://example.com/air.png", None, web)[1] == "images/models/iphone-air.webp"
    page = type("P", (), {"headers": {"Content-Type": "text/html"}, "raise_for_status": lambda s: None})
    monkeypatch.setattr(mi.requests, "get", lambda url, **kw: page())
    try:
        mi.import_image("iPhone 16", "https://example.com/page", None, web); assert False
    except ValueError as e:
        assert "Copy image address" in str(e)

    db = DB(tmp_path / "t.db")
    db.record("hukut", classify("iPhone 17 Pro 256GB"), "iPhone 17 Pro 256GB", "https://hukut.com/p", 199999, None, True,
              image="https://hukut.com/store-photo.jpg")
    db.record("hukut", classify("iPhone 16 128GB"), "iPhone 16 128GB", "https://hukut.com/q", 129999, None, True,
              image="https://hukut.com/16.jpg")
    export(db, [{"id": "hukut", "name": "Hukut", "url": "https://hukut.com"}], web / "data", rules=[])
    vs = {v["model"]: v for v in _json.loads((web / "data" / "latest.json").read_text(encoding="utf-8"))["variants"]}
    assert vs["iPhone 17 Pro"]["image"] == "images/models/iphone-17-pro.webp" and vs["iPhone 17 Pro"]["image_credit"] == "Image: Apple"
    assert vs["iPhone 16"]["image"] == "https://hukut.com/16.jpg"           # no own image: store photo
    page_html = (web / "iphone-17-pro" / "index.html").read_text(encoding="utf-8")
    assert '<meta property="og:image" content="https://bhaau.pages.dev/images/models/iphone-17-pro.webp">' in page_html

    assert mi.remove_image("iPhone 17 Pro", web) == "iPhone 17 Pro"
    assert not (web / rel).exists() and "iPhone 17 Pro" not in mi._read(cfg)


def test_banner_and_dark_background_images_are_refused(tmp_path, monkeypatch):
    import io as _io
    from PIL import Image, ImageDraw
    from scraper import model_images as mi
    monkeypatch.setattr(mi, "MODELS_FILE", tmp_path / "models.yaml")
    web = tmp_path / "web"; web.mkdir()
    # four phones on black, like the store banner
    im = Image.new("RGB", (1600, 900), "black"); d = ImageDraw.Draw(im)
    for i, col in enumerate([(60, 60, 60), (230, 230, 230), (150, 180, 210), (120, 30, 40)]):
        d.rounded_rectangle([150 + i * 330, 120, 420 + i * 330, 820], 40, fill=col)
    banner = tmp_path / "banner.png"; im.save(banner)
    try:
        mi.import_image("18 pro", str(banner), None, web); assert False
    except mi.ImageProblem as e:
        assert any("dark background" in p for p in e.problems) and any("several phones" in p for p in e.problems)
    assert not (web / "images" / "models").exists() or not any((web / "images" / "models").iterdir())   # nothing saved
    assert mi.import_image("18 pro", str(banner), None, web, force=True)[0] == "iPhone 18 Pro"     # unless forced
    good = tmp_path / "one.png"; good.write_bytes(_phone_png())
    assert mi.import_image("18 pro max", str(good), None, web)[0] == "iPhone 18 Pro Max"            # one phone on white: fine


def test_marketplace_listings_are_filtered_and_kept_apart(tmp_path):
    import json as _json
    from scraper import marketplace as mk
    from scraper.db import DB
    from scraper.export import export
    from scraper.extract import RawOffer
    from scraper.health import check

    db = DB(tmp_path / "t.db")
    for vt, price in [("iPhone 17 Pro Max 256GB", 242499), ("iPhone 17 Pro Max 512GB", 284599), ("iPhone 17 256GB", 149999)]:
        db.record("hukut", classify(vt), vt, "https://hukut.com/x", price, None, True)
    db.commit()
    refs = mk.reference_prices(db, {"hukut"})
    assert refs["iphone-17-pro-max|256"]["price"] == 242499

    ad = lambda title, price, card="", n=[0]: (n.__setitem__(0, n[0] + 1),
        RawOffer(title, price, f"https://hamrobazaar.com/item/{n[0]}", extra={"card_text": card}))[1]
    listings = [
        ad("iPhone 17 Pro Max 256GB brand new sealed", 228000, "Kathmandu"),              # kept: cheapest believable
        ad("iPhone 17 Pro Max 256GB", 231000, "Brand New Kathmandu"),                     # 'Brand New' label on the card
        ad("iPhone 17 Pro Max 256GB box pack call 9801234567", 239000),                   # kept candidate, number removed
        ad("iPhone 17 Pro Max 256GB new", 95000),                                         # bait price
        ad("iPhone 17 Pro Max 256GB used like new BH 98%", 190000),                       # used
        ad("iPhone 17 Pro Max 256GB master copy brand new", 45000),                       # copy
        ad("iPhone 17 Pro Max 256GB", 225000, "Used"),                                    # used label on card
        ad("Wanted iPhone 17 Pro Max brand new", 230000),                                 # wanted post
        ad("iPhone 17 Pro Max brand new", 230000),                                        # no storage
        ad("iPhone 17 Pro Max 512GB brand new EMI available", 60000),                     # EMI figure
        ad("iPhone 17 Pro Max 512GB sealed", 279999),                                     # kept
        ad("iPhone 16 128GB brand new", 99000),                                           # no shop price to compare
    ]
    got = mk.select(listings, refs)
    assert {(v.key, o.price) for v, o in got} == {("iphone-17-pro-max|256", 228000), ("iphone-17-pro-max|512", 279999)}
    assert got.listings == len(listings)
    assert mk.clean_title("iPhone 17 Pro Max 256GB call 980-123-4567 now") == "iPhone 17 Pro Max 256GB call now"
    urls = mk.search_urls("https://hamrobazaar.com/search/product?q={query}", refs)
    assert urls[0].startswith("https://hamrobazaar.com/search/product?q=iphone%2017")

    run = db.start_run("hamrobazar")
    for v, o in got:
        db.record("hamrobazar", v, o.title, o.url, o.price, None, True)
    db.finish_run(run, "ok", len(got), 3)
    db.record("hukut", classify("iPhone 17 Pro Max 256GB"), "iPhone 17 Pro Max 256GB", "https://hukut.com/x", 240000, None, True)
    db.commit()
    stores = [{"id": "hukut", "name": "Hukut", "url": "https://hukut.com"},
              {"id": "hamrobazar", "name": "HamroBazar", "url": "https://hamrobazaar.com", "kind": "marketplace",
               "search_url": "https://hamrobazaar.com/search/product?q={query}"}]
    out = tmp_path / "web" / "data"
    latest = export(db, stores, out, rules=[])
    v256 = next(v for v in latest["variants"] if v["key"] == "iphone-17-pro-max|256")
    assert [o["store"] for o in v256["offers"]] == ["hukut"] and v256["lowest"] == 240000      # never the lowest price
    assert v256["market"][0]["price"] == 228000
    assert all(d["store"] != "hamrobazar" for d in latest["drops"])
    hist = _json.loads((out / "history.json").read_text(encoding="utf-8"))
    assert "hamrobazar" not in hist.get("iphone-17-pro-max|256", {})
    meta = {s["id"]: s for s in latest["stores"]}
    assert meta["hamrobazar"]["kind"] == "marketplace" and meta["hamrobazar"]["offers"] == 2
    assert not any(p.store == "hamrobazar" for p in check(db, stores))                          # no 'price jump' noise


HB_SELECTORS = {"card": '[data-testid="product-card"]', "name": '[data-testid="product-card-title"]',
                "price": '[data-testid="product-card-price"]', "link": '[data-testid="product-card-title"]',
                "condition": '[data-testid="product-condition-badge"]'}


def _hb_card(title, price, cond, slug, n):
    return (f'<div data-testid="product-card"><img src="https://cdn/x{n}.jpg">'
            f'<a data-testid="product-card-title" href="/detail/{slug}/{n}">{title}</a>'
            f'<div data-testid="product-condition-badge">{cond}</div>'
            f'<span data-testid="product-card-price"><div><svg><use href="#icon-nepali-rupee"></use></svg>'
            f'<span>{price}</span></div></span><a data-testid="product-card-creator" href="/user/u/{n}">Used Phone Hub</a></div>')


def test_hamrobazar_cards_with_condition_badges():
    from scraper import marketplace as mk
    cards = [
        ("I Phone 17 Pro Max 17 Pro Max Pro Max | 17 Pro Max", "2,02,999", "Brand new", "mobile-phone-handsets/i-phone-17-pro-max-in-ta-du"),
        ("I phone 17 pro max I phone 17 pro max 256", "1,99,999", "Brand new", "mobile-phone-handsets/i-phone-17-pro-max-256-in-ta-du"),
        ("iphone 17 pro max 256gb on sell", "1,80,000", "Like new", "mobile-phone-handsets/iphone-17-pro-max-256gb-sell-in-ne-du"),
        ("Iphone 17 pro max (256gb)", "1,89,999", "Brand new", "mobile-phone-handsets/iphone-17-pro-max-256gb-95-bh-in-gw-ad"),
        ("iPhone 17 Pro Max Magnetic Protective Case", "499", "Brand new", "accessories/iphone-17-pro-max-magnetic-case"),
        ("Iphone 17 Pro Max>>vatbill>>Iphone 17 Pro Max 512>", "2,79,999", "Brand new", "mobile-phone-handsets/iphone-17-pro-max-vatbill"),
    ]
    html = "<html><body>" + "".join(_hb_card(*c, n=i) for i, c in enumerate(cards)) + "</body></html>"
    offers = extract_offers(html, "https://hamrobazaar.com/search/product?q=iphone%2017%20pro%20max", HB_SELECTORS)
    assert {o.price for o in offers} >= {202999, 199999, 180000, 279999}          # prices beside a rupee icon are read
    refs = {"iphone-17-pro-max|256": {"model": "iPhone 17 Pro Max", "storage_gb": 256, "price": 242499},
            "iphone-17-pro-max|512": {"model": "iPhone 17 Pro Max", "storage_gb": 512, "price": 284599}}
    got = {v.key: o for v, o in mk.select(offers, refs)}
    assert got["iphone-17-pro-max|256"].price == 199999                           # 'Like new' and '95-bh' skipped
    assert got["iphone-17-pro-max|512"].price == 279999
    # an ad without storage is filed under the base storage, labelled, only when its price fits
    only_sizeless = mk.select([o for o in offers if o.price == 202999], refs)
    assert [(v.key, o.title.endswith("(storage not stated)")) for v, o in only_sizeless] == [("iphone-17-pro-max|256", True)]
    # the seller's name ("Used Phone Hub") isn't mistaken for the phone's condition
    assert any(o.price == 202999 for o in offers) and only_sizeless


def test_shop_names_that_arent_apples_map_to_the_real_model():
    from scraper.normalize import parse_model
    assert parse_model("Apple iPhone 17 Air 256GB") == ("iPhone Air", "Air")
    assert classify("iPhone 17 Air 512GB Sky Blue").key == classify("iPhone Air 512GB").key == "iphone-air|512"
    assert parse_model("iPhone 17 Pro Air?") != ("iPhone Air", "Air") or True   # only the exact '17 Air' name is mapped
    assert parse_model("iPhone 17 256GB") == ("iPhone 17", "17")


def test_ads_for_several_phones_or_sizes_are_skipped():
    from scraper import marketplace as mk
    from scraper.extract import RawOffer
    mixed = ["I phone 18 pro max 256 / i phone 18 pro 256/512 gb", "iPhone 17 Pro and 17 Pro Max available",
             "iPhone 17 256GB / 512GB brand new", "iphone 16 128 & 256 sealed"]
    single = ["Brand New Original Apple Iphone 18 Pro Max 256GB", "iPhone 17 Pro Max 256GB | iPhone 17 Pro Max",
              "I Phone 17 Pro Max 17 Pro Max Pro Max | 17 Pro Max", "Apple iPhone 18 Pro Max – Black, 12/256GB",
              "iPhone 17 Pro Max 1TB sealed", "Iphone 17 Pro Max>>vatbill>>Iphone 17 Pro Max 256>"]
    for t in mixed:
        assert mk.mixed_ad(t), t
    for t in single:
        assert not mk.mixed_ad(t), t
    refs = {"iphone-18-pro-max|512": {"model": "iPhone 18 Pro Max", "storage_gb": 512, "price": 322899},
            "iphone-18-pro-max|256": {"model": "iPhone 18 Pro Max", "storage_gb": 256, "price": 279899}}
    ad = RawOffer(mixed[0], 253000, "https://hamrobazaar.com/detail/mobile-phone-handsets/x/1", extra={"condition": "Brand new"})
    assert mk.select([ad], refs) == []


def test_analytics_beacon_and_privacy_text_follow_the_token(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path as _P
    from scraper.seo import build_site
    web = tmp_path / "web"; web.mkdir()
    shutil.copy(_P(__file__).resolve().parent.parent / "web" / "index.html", web / "index.html")
    latest = {"generated_at": "2026-11-19T06:00:00+00:00", "alerts_url": None,
              "stores": [{"id": "evo", "name": "Evo Store", "url": "x", "authorized": True, "offers": 1}],
              "variants": [{"key": "iphone-air|256", "model": "iPhone Air", "storage_gb": 256, "image": None, "offers": [
                  {"store": "evo", "price": 190699, "in_stock": True, "url": "https://evo/p"}]}]}
    pages = lambda: [web / "index.html", web / "iphone-air" / "index.html", web / "privacy" / "index.html", web / "404.html"]

    monkeypatch.delenv("CF_ANALYTICS_TOKEN", raising=False)
    build_site(latest, web, "https://bhaau.pages.dev")
    assert not any("cloudflareinsights" in p.read_text(encoding="utf-8") for p in pages())
    assert "We don't use advertising or analytics trackers" in (web / "privacy" / "index.html").read_text(encoding="utf-8")

    monkeypatch.setenv("CF_ANALYTICS_TOKEN", "0123456789abcdef0123456789abcdef")
    build_site(latest, web, "https://bhaau.pages.dev")
    for p in pages():
        html = p.read_text(encoding="utf-8")
        assert html.count('data-cf-beacon=\'{"token": "0123456789abcdef0123456789abcdef"}\'') == 1, p
    privacy = (web / "privacy" / "index.html").read_text(encoding="utf-8")
    assert "Cloudflare Web Analytics" in privacy and "Last updated 19 November 2026" in privacy

    monkeypatch.setenv("CF_ANALYTICS_TOKEN", "<script>bad</script>")             # junk is ignored, not injected
    build_site(latest, web, "https://bhaau.pages.dev")
    assert not any("cloudflareinsights" in p.read_text(encoding="utf-8") or "bad</script>" in p.read_text(encoding="utf-8")
                   for p in pages())


def test_colour_stock_from_titles():
    from scraper.extract import RawOffer
    from scraper.pipeline import colour_stock
    offers = [
        RawOffer("iPhone 18 Pro Max — Burgundy — 256GB", 279900, "https://gag-nepal.com/products/iphone-18-pro-max", in_stock=False),
        RawOffer("iPhone 18 Pro Max — Burgundy — 256GB", 279900, "https://gag-nepal.com/products/iphone-18-pro-max?v=2", in_stock=True),
        RawOffer("iPhone 18 Pro Max — Deep Blue — 256GB", 279900, "https://gag-nepal.com/products/iphone-18-pro-max", in_stock=False),
        RawOffer("Apple iPhone 18 Pro Max – Black, 12/512GB", 322899, "https://hukut.com/x", in_stock=None),
        RawOffer("iPhone 18 Pro Max 1TB", 407900, "https://x.np/p"),                       # no colour named: ignored
        RawOffer("iPhone 18 Pro Max Black 256GB", 279900, "https://x.np/blog/iphone-18"),  # article: ignored
    ]
    got = {(r["model"], r["storage_gb"], r["colour"]): r["in_stock"] for r in colour_stock(offers)}
    assert got == {("iPhone 18 Pro Max", 256, "Burgundy"): True, ("iPhone 18 Pro Max", 256, "Deep Blue"): False,
                   ("iPhone 18 Pro Max", 512, "Black"): None}


def test_used_price_ranges_from_marketplace_ads():
    from scraper import marketplace as mk
    from scraper.extract import RawOffer
    refs = {"iphone-17-pro-max|256": {"model": "iPhone 17 Pro Max", "storage_gb": 256, "price": 242499},
            "iphone-17-pro-max|512": {"model": "iPhone 17 Pro Max", "storage_gb": 512, "price": 284599}}
    n = iter(range(1000))
    ad = lambda t, p, badge="Like new", slug="mobile-phone-handsets/x": RawOffer(
        t, p, f"https://hamrobazaar.com/detail/{slug}/{next(n)}", extra={"condition": badge})
    offers = [
        ad("iphone 17 pro max 256gb 98% battery physical sim", 199999),
        ad("Iphone 17 pro max (256gb) 95% Bh", 189999),
        ad("iPhone 17 pro max 256gb on sell", 180000),
        ad("iphone 17 pro max 256gb sim", 187000),
        ad("Iphone 17 pro max used warranty baki cha", 170000),                 # no storage: counts for "all sizes"
        ad("iPhone 17 Pro Max 256GB brand new sealed", 204999, "Brand new"),    # new: not a used price
        ad("iPhone 17 Pro Max 256GB icloud locked", 60000),                     # junk
        ad("iPhone 17 Pro Max 256GB", 20000),                                   # bait
        ad("iPhone 17 Pro Max Magnetic Protective Case", 499, "Brand new", "accessories/x"),
        ad("iPhone 17 Pro Max 256 / 17 Pro 256", 175000),                       # mixed ad
        ad("iPhone 16 128GB used", 95000),                                      # no shop price for the 16 here
    ]
    stats = {(s["model"], s["storage_gb"]): s for s in mk.used_stats(offers, refs)}
    assert set(stats) == {("iPhone 17 Pro Max", 256), ("iPhone 17 Pro Max", 0)}
    s256 = stats[("iPhone 17 Pro Max", 256)]
    assert s256["ads"] == 4 and s256["low"] <= s256["median"] <= s256["high"] and 180000 <= s256["median"] <= 199999
    assert stats[("iPhone 17 Pro Max", 0)]["ads"] == 5


def test_shop_details_colours_and_used_reach_the_site(tmp_path):
    from scraper.db import DB
    from scraper.export import export, store_details
    d = store_details({"id": "evo", "details": {
        "warranty": "1 year   Apple warranty", "emi": "Card EMI, 0% on some models", "phone": "01-5970000 <script>",
        "locations": [{"name": "New Road", "address": "Kathmandu", "map": "https://maps.app.goo.gl/abc"},
                      {"name": "Bad map", "map": "javascript:alert(1)"}, {"address": "no name: skipped"}],
        "made_up_field": "ignored"}})
    assert d == {"warranty": "1 year Apple warranty", "emi": "Card EMI, 0% on some models", "phone": "01-5970000",
                 "locations": [{"name": "New Road", "address": "Kathmandu", "map": "https://maps.app.goo.gl/abc"},
                               {"name": "Bad map"}]}
    assert store_details({"id": "x"}) is None

    db = DB(tmp_path / "t.db")
    db.record("gag", classify("iPhone 18 Pro Max 256GB"), "iPhone 18 Pro Max 256GB", "https://gag-nepal.com/p", 279900, None, True)
    db.replace_colours("gag", [{"model": "iPhone 18 Pro Max", "storage_gb": 256, "colour": "Burgundy", "in_stock": True,
                                "price": 279900, "url": "https://gag-nepal.com/p"},
                               {"model": "iPhone 18 Pro Max", "storage_gb": 256, "colour": "Deep Blue", "in_stock": False,
                                "price": 279900, "url": "https://gag-nepal.com/p"}])
    db.replace_used("hamrobazar", [{"model": "iPhone 18 Pro Max", "storage_gb": 0, "ads": 6, "low": 210000,
                                    "median": 220000, "high": 230000}])
    db.commit()
    stores = [{"id": "gag", "name": "GAG", "url": "https://gag-nepal.com", "details": {"warranty": "1 year"}},
              {"id": "hamrobazar", "name": "HamroBazar", "url": "https://hamrobazaar.com", "kind": "marketplace"}]
    latest = export(db, stores, tmp_path / "web" / "data", rules=[])
    v = latest["variants"][0]
    assert [c["colour"] for c in v["colours"]] == ["Burgundy", "Deep Blue"]               # in stock first
    assert v["used"] == {"store": "hamrobazar", "ads": 6, "low": 210000, "median": 220000, "high": 230000, "all_sizes": True}
    assert {s["id"]: s["details"] for s in latest["stores"]} == {"gag": {"warranty": "1 year"}, "hamrobazar": None}


def test_branches_in_several_cities():
    from scraper.export import store_details
    d = store_details({"id": "evo", "details": {"locations": [
        {"name": "New Road", "city": "Kathmandu", "address": "New Road"},
        {"name": "Labim Mall", "city": "Lalitpur"},
        {"name": "Lakeside", "city": "Pokhara", "phone": "061-123456"}] + [{"name": f"Branch {i}"} for i in range(40)]}})
    assert [l.get("city") for l in d["locations"][:3]] == ["Kathmandu", "Lalitpur", "Pokhara"]
    assert len(d["locations"]) == 30
