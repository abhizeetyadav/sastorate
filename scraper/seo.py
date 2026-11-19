"""Search-engine pages, written after every export.

Google indexes real addresses, not #hash views, and reads prices best when
they are in the HTML. So for every iPhone model this writes

    web/<model-slug>/index.html   e.g. web/iphone-17-pro/index.html

with its own <title>, description, canonical URL, Open Graph tags, a
schema.org Product + AggregateOffer block, and the price table already in
the page. The interactive site (app.js) still loads on top of it.

It also fills a plain price list into web/index.html (between the
<!--seo:...--> markers), writes the privacy page (web/privacy/), the footer
links on every page, sitemap.xml and robots.txt.

CONTACT_EMAIL (optional) is the public address shown in the footer, on the
privacy page and in "Report a wrong price" links.

Set SITE_URL (e.g. https://bhaau.com.np, or https://you.github.io/repo) in
.env or as a GitHub Actions variable so canonical URLs and the sitemap use
full addresses. Without it, pages are still written but without them.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)
MARKER_FILE = ".bhaau-page"  # only folders carrying this are ever deleted
KTM = timezone(timedelta(hours=5, minutes=45))  # Nepal time, no daylight saving


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _gb(g: int | None) -> str:
    return "" if not g else (f"{g // 1024} TB" if g >= 1024 else f"{g} GB")


def _npr(n: int) -> str:
    """1234567 -> '12,34,567' (Nepali/Indian grouping)."""
    s = str(int(n))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    return f"{head},{tail}"


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _jsonld(obj) -> str:
    return '<script type="application/ld+json">' + json.dumps(obj, ensure_ascii=False).replace("</", "<\\/") + "</script>"


def _replace(block: str, name: str, content: str) -> str:
    return re.sub(rf"<!--seo:{name}-->.*?<!--/seo:{name}-->",
                  lambda _: f"<!--seo:{name}-->{content}<!--/seo:{name}-->", block, flags=re.S)


def _set_head(page: str, title: str, description: str) -> str:
    page = re.sub(r"<title>.*?</title>", f"<title>{_e(title)}</title>", page, count=1, flags=re.S)
    return re.sub(r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{_e(description)}">',
                  page, count=1)


EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[A-Za-z]{2,}$")
PRIVACY_UPDATED = "18 November 2026"   # change this when you change the privacy text below
PRIVACY_UPDATED_ANALYTICS = "19 November 2026"  # the version that mentions visitor stats


def analytics_token() -> str | None:
    """Cloudflare Web Analytics site token (CF_ANALYTICS_TOKEN): cookie-free visit counts."""
    t = (os.getenv("CF_ANALYTICS_TOKEN") or "").strip()
    if t and not re.fullmatch(r"[0-9a-f]{32}", t):
        log.warning("CF_ANALYTICS_TOKEN doesn't look like a Cloudflare Web Analytics token; ignoring it")
        return None
    return t or None


def _beacon(token: str | None) -> str:
    return (f"<script defer src=\"https://static.cloudflareinsights.com/beacon.min.js\" "
            f"data-cf-beacon='{{\"token\": \"{token}\"}}'></script>") if token else ""


def contact_email() -> str | None:
    """The public contact address from CONTACT_EMAIL, or None if unset/invalid."""
    e = (os.getenv("CONTACT_EMAIL") or "").strip()
    if e and not EMAIL_RE.match(e):
        log.warning("CONTACT_EMAIL %r doesn't look like an email address; ignoring it", e)
        return None
    return e or None


def _mailto(email: str, subject: str = "") -> str:
    from urllib.parse import quote
    return f"mailto:{email}" + (f"?subject={quote(subject)}" if subject else "")


def _footer_links(root: str, contact: str | None, report_subject: str = "Wrong price on Bhaau") -> str:
    links = [f'<a href="{root}privacy/">Privacy</a>']
    if contact:
        links += [f'<a href="{_e(_mailto(contact, report_subject))}">Report a wrong price</a>',
                  f'<a href="{_e(_mailto(contact))}">Contact</a>']
    return f'<nav class="foot-links" aria-label="About Bhaau">{"".join(links)}</nav>'


def _privacy_body(contact: str | None, alerts_on: bool, analytics_on: bool = False) -> str:
    mail = (f'<a href="{_e(_mailto(contact))}">{_e(contact)}</a>') if contact else None
    parts = [
        '<ol class="crumbs"><li><a href="../">Home</a></li><li>Privacy</li></ol>',
        '<article class="doc">',
        '<h1>Privacy</h1>',
        f'<p class="updated">Last updated {PRIVACY_UPDATED_ANALYTICS if analytics_on else PRIVACY_UPDATED}</p>',
        "<p>Bhaau is an independent website that compares iPhone prices in Nepal. This page explains what we keep about you and why. "
        "You can use the whole site without telling us anything"
        + (". If you sign up for a price alert, we keep your email address only to send that alert.</p>" if alerts_on else ".</p>"),

        "<h2>Using the website</h2><ul>"
        "<li>There are no accounts and no sign-in.</li>"
        + ("<li>We count visits with Cloudflare Web Analytics, which doesn't use cookies or similar storage in your "
           "browser and doesn't track you across sites. It tells us which pages are popular, not who you are. "
           "We don't use advertising trackers, and we don't set cookies.</li>"
           if analytics_on else "<li>We don't use advertising or analytics trackers, and we don't set cookies.</li>")
        + "<li>If you pick the Light or Dark theme, that choice is saved in your own browser. It is never sent to us.</li>"
        "<li>Like every website, our hosting provider receives your IP address and browser details when you open a page, "
        "and may keep them in its server logs for a short time for security.</li>"
        "<li>Fonts are loaded from Google Fonts, so Google also receives your IP address when a page loads.</li></ul>",

        "<h2>Prices</h2><p>Prices, stock and product links come from sellers' public websites. "
        "We don't collect any information about sellers' customers.</p>",
    ]
    if alerts_on:
        parts.append(
            "<h2>Price alerts</h2><p>When you create a price alert, we store:</p><ul>"
            "<li>your email address</li>"
            "<li>the iPhone and storage size you chose, and your target price if you set one</li>"
            "<li>when you signed up, confirmed, and were last emailed</li>"
            "<li>a scrambled code made from your connection's IP address (not the address itself), used only to stop "
            "too many sign-ups from one connection</li></ul>"
            "<p>We use this only to email you when that iPhone's price drops. We don't send newsletters or advertising, "
            "and we never sell or share your email address.</p>"
            "<p>Nothing is switched on until you click the link in the confirmation email. "
            "If you don't click it, your sign-up is deleted after 7 days.</p>"
            "<p>To stop, use the <b>Stop alerts</b> link at the bottom of any alert email. You can stop one phone or all of them. "
            "Stopping deletes the alert straight away; we don't keep a copy.</p>"
            "<p>Two services handle alerts for us: Cloudflare stores the alert list and Resend sends the emails. "
            "Their servers may be outside Nepal, and they use the data only to provide this service to us.</p>")
    parts.append("<h2>Your choices</h2>" + (
        f"<p>You can ask what we hold about you, ask us to correct it, or ask us to delete it. Email {mail} and we'll take care of it.</p>"
        if mail else
        "<p>You can ask what we hold about you, ask us to correct it, or ask us to delete it, by replying to any email we sent you."
        + (" The Stop alerts link deletes your alert immediately." if alerts_on else "") + "</p>"))
    parts.append("<h2>Changes</h2><p>If this page changes, the date at the top changes too.</p>")
    if mail:
        parts.append(f"<h2>Contact</h2><p>Questions about this page, a wrong price, or a seller who wants a listing corrected: {mail}</p>")
    parts.append("</article>")
    return "".join(parts)


def _best(offers: list[dict]) -> dict:
    return next((o for o in offers if o.get("in_stock") is not False), offers[0])


def _availability(o: dict) -> str:
    return "https://schema.org/OutOfStock" if o.get("in_stock") is False else "https://schema.org/InStock"


def build_site(latest: dict, web_dir: str | Path, site_url: str | None = None) -> int:
    web_dir = Path(web_dir)
    shell_path = web_dir / "index.html"
    if not shell_path.exists():
        return 0
    shell = shell_path.read_text(encoding="utf-8")
    if "<!--seo:head-->" not in shell:
        log.warning("web/index.html has no <!--seo:...--> markers; skipping search-engine pages")
        return 0
    site = (site_url or os.getenv("SITE_URL") or "").rstrip("/")
    contact = latest.get("contact_email") or contact_email()
    beacon = _beacon(analytics_token())  # visit counts on every public page (never on the status page)
    if not contact:
        log.warning("CONTACT_EMAIL is not set: footer shows only the Privacy link (no Contact / Report a wrong price)")
    stores = {s["id"]: s for s in latest["stores"]}
    updated = datetime.fromisoformat(latest["generated_at"]).astimezone(KTM)
    month = updated.strftime("%B %Y")
    stamp = f"{updated.day} {updated.strftime('%B %Y')}"

    # group storage variants under their model, keeping the export's order (newest first)
    models: dict[str, dict] = {}
    for v in latest["variants"]:
        m = models.setdefault(v["model"], {"model": v["model"], "slug": _slug(v["model"]), "variants": [], "image": None})
        m["variants"].append(v)
        m["image"] = m["image"] or v.get("image")

    # ------------------------------------------------------------ one page per model
    written = set()
    for m in models.values():
        variants = sorted(m["variants"], key=lambda v: v["storage_gb"] or 10**9)
        all_offers = [(v, o) for v in variants for o in v["offers"] if o["store"] in stores]
        if not all_offers:
            continue
        cheapest_v, cheapest = min(((v, _best(v["offers"])) for v in variants if v["offers"]), key=lambda x: x[1]["price"])
        sellers = sorted({o["store"] for _, o in all_offers})
        seller_names = [stores[s]["name"] for s in sellers]
        prices = [o["price"] for _, o in all_offers]
        name = f"Apple {m['model']}"
        url = f"{site}/{m['slug']}/" if site else ""

        title = f"{name} Price in Nepal ({month}): from Rs {_npr(cheapest['price'])} | Bhaau"
        sizes = ", ".join(_gb(v["storage_gb"]) for v in variants if v["storage_gb"])
        desc = (f"{name} price in Nepal starts at Rs {_npr(cheapest['price'])} at {stores[cheapest['store']]['name']}. "
                f"Compare {len(sellers)} sellers" + (f" for {sizes}" if sizes else "") +
                f", see stock and price history. Updated {stamp}.")

        offers_ld = [{
            "@type": "Offer", "price": o["price"], "priceCurrency": "NPR", "availability": _availability(o),
            "url": o["url"], "name": f"{name} {_gb(v['storage_gb'])}".strip(),
            "seller": {"@type": "Organization", "name": stores[o["store"]]["name"]},
        } for v, o in all_offers]
        product_ld = {
            "@context": "https://schema.org", "@type": "Product", "name": name,
            "brand": {"@type": "Brand", "name": "Apple"},
            "description": desc,
            "offers": {"@type": "AggregateOffer", "priceCurrency": "NPR", "lowPrice": min(prices),
                       "highPrice": max(prices), "offerCount": len(offers_ld), "offers": offers_ld},
        }
        image_url = m["image"] if re.match(r"^https?://", m["image"] or "") else (
            f"{site}/{m['image']}" if site and m["image"] else None)   # own images are stored inside web/
        if image_url:
            product_ld["image"] = [image_url]
        if url:
            product_ld["url"] = url
        crumbs_ld = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", **({"item": f"{site}/"} if site else {})},
            {"@type": "ListItem", "position": 2, "name": name, **({"item": url} if url else {})}]}

        head = "".join([
            f'<link rel="canonical" href="{_e(url)}">' if url else "",
            f'<meta property="og:type" content="product"><meta property="og:title" content="{_e(title)}">',
            f'<meta property="og:description" content="{_e(desc)}">',
            f'<meta property="og:url" content="{_e(url)}">' if url else "",
            f'<meta property="og:image" content="{_e(image_url)}">' if image_url else "",
            _jsonld(product_ld), _jsonld(crumbs_ld),
        ])

        rows = "".join(
            f'<tr><td>{_e(_gb(v["storage_gb"]) or "Size not listed")}</td><td>{_e(stores[o["store"]]["name"])}</td>'
            f'<td class="r">Rs {_npr(o["price"])}</td><td>{"Out of stock" if o.get("in_stock") is False else "In stock" if o.get("in_stock") else ""}</td>'
            f'<td><a href="{_e(o["url"])}" rel="nofollow noopener" target="_blank">Go to store</a></td></tr>'
            for v, o in all_offers)
        body = (
            f'<ol class="crumbs"><li><a href="../">Home</a></li><li>{_e(m["model"])}</li></ol>'
            f'<div class="page-head"><h1>{_e(name)} price in Nepal</h1>'
            f'<p>From Rs {_npr(cheapest["price"])} at {_e(stores[cheapest["store"]]["name"])}'
            f'{" for " + _gb(cheapest_v["storage_gb"]) if cheapest_v["storage_gb"] else ""}. '
            f'Compared across {_e(", ".join(seller_names))}. Updated {stamp}.</p></div>'
            f'<div class="panel" style="margin-top:16px"><table class="price-list"><thead><tr><th>Storage</th><th>Seller</th>'
            f'<th class="r">Price</th><th>Stock</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>')

        page = _set_head(shell, title, desc)
        page = _replace(page, "head", head)
        page = _replace(page, "body", "")
        page = _replace(page, "product", body)
        page = _replace(page, "foot", _footer_links("../", contact, f"Wrong price: {m['model']}"))
        page = _replace(page, "analytics", beacon)
        page = page.replace('<div id="product" hidden>', '<div id="product">', 1)
        page = page.replace('<div id="catalog">', '<div id="catalog" hidden>', 1)
        page = page.replace("<body>", f'<body data-root="../" data-slug="{_e(m["slug"])}">', 1)
        page = page.replace('href="styles.css"', 'href="../styles.css"').replace('src="app.js"', 'src="../app.js"')

        folder = web_dir / m["slug"]
        folder.mkdir(exist_ok=True)
        (folder / "index.html").write_text(page, encoding="utf-8")
        (folder / MARKER_FILE).write_text("generated by scraper/seo.py\n", encoding="utf-8")
        written.add(m["slug"])

    # ------------------------------------------------------------ privacy page
    privacy = _set_head(shell, "Privacy | Bhaau",
                        "What Bhaau keeps about you: nothing while you browse, and your email address only if you sign up for a price alert.")
    privacy = _replace(privacy, "head", f'<link rel="canonical" href="{site}/privacy/">' if site else "")
    privacy = _replace(privacy, "body", "")
    privacy = _replace(privacy, "product", _privacy_body(contact, bool(latest.get("alerts_url")), bool(beacon)))
    privacy = _replace(privacy, "analytics", beacon)
    privacy = _replace(privacy, "foot", _footer_links("../", contact))
    privacy = privacy.replace('<div id="product" hidden>', '<div id="product">', 1)
    privacy = privacy.replace('<div id="catalog">', '<div id="catalog" hidden>', 1)
    privacy = privacy.replace("<body>", '<body data-root="../" data-page="privacy">', 1)
    privacy = privacy.replace('href="styles.css"', 'href="../styles.css"').replace('src="app.js"', 'src="../app.js"')
    (web_dir / "privacy").mkdir(exist_ok=True)
    (web_dir / "privacy" / "index.html").write_text(privacy, encoding="utf-8")
    (web_dir / "privacy" / MARKER_FILE).write_text("generated by scraper/seo.py\n", encoding="utf-8")

    # ------------------------------------------------------------ 404 page
    # Cloudflare Pages serves 404.html for unknown addresses. Without it, it would answer every
    # unknown address with the home page and a 200, which Google treats as duplicate content.
    # It can be shown at any depth, so it links from the site root.
    from urllib.parse import urlparse
    base = (urlparse(site).path.rstrip("/") + "/") if site else "/"
    nf_body = (f'<div class="doc"><h1>Page not found</h1>'
               f'<p>This page doesn\'t exist, or the phone is no longer sold in Nepal.</p>'
               f'<p><a href="{base}">See all iPhone prices</a></p></div>')
    nf = _set_head(shell, "Page not found | Bhaau", "This page doesn't exist on Bhaau.")
    nf = _replace(nf, "head", '<meta name="robots" content="noindex">')
    nf = _replace(nf, "body", "")
    nf = _replace(nf, "product", nf_body)
    nf = _replace(nf, "foot", _footer_links(base, contact))
    nf = _replace(nf, "analytics", beacon)
    nf = nf.replace('<div id="product" hidden>', '<div id="product">', 1)
    nf = nf.replace('<div id="catalog">', '<div id="catalog" hidden>', 1)
    nf = nf.replace("<body>", f'<body data-root="{base}" data-page="404">', 1)
    nf = nf.replace('href="styles.css"', f'href="{base}styles.css"').replace('src="app.js"', f'src="{base}app.js"')
    (web_dir / "404.html").write_text(nf, encoding="utf-8")

    # remove pages for models that are no longer sold anywhere
    for d in web_dir.iterdir():
        if d.is_dir() and (d / MARKER_FILE).exists() and d.name not in written and d.name != "privacy":
            shutil.rmtree(d)
            log.info("removed page for %s (no longer listed)", d.name)

    # ------------------------------------------------------------ home page: plain price list for crawlers
    home_rows = []
    for m in models.values():
        if m["slug"] not in written:
            continue
        v0 = min(m["variants"], key=lambda v: v["storage_gb"] or 10**9)
        b = _best(v0["offers"])
        home_rows.append(f'<tr><td><a href="{m["slug"]}/">Apple {_e(m["model"])}</a> {_e(_gb(v0["storage_gb"]))}</td>'
                         f'<td>{_e(stores[b["store"]]["name"])}</td><td class="r">Rs {_npr(b["price"])}</td></tr>')
    live = sum(1 for s in latest["stores"] if s.get("offers"))
    home_title = f"iPhone Price in Nepal ({month}): compare {live} sellers | Bhaau"
    home_desc = (f"Compare iPhone prices in Nepal at {live} sellers including Apple authorized resellers. "
                 f"Lowest price for every model and storage, stock status and price history. Updated {stamp}.")
    home_body = (f'<div class="page-head" style="padding-top:18px"><h1>Apple iPhone prices in Nepal</h1>'
                 f'<p>Updated {stamp}. Comparing {len(written)} iPhone models at {live} sellers.</p></div>'
                 f'<div class="panel" style="margin-top:16px"><table class="price-list"><thead><tr><th>iPhone model</th>'
                 f'<th>Available at</th><th class="r">Lowest price</th></tr></thead><tbody>{"".join(home_rows)}</tbody></table></div>')
    home_head = "".join([
        f'<link rel="canonical" href="{site}/">' if site else "",
        f'<meta property="og:type" content="website"><meta property="og:title" content="{_e(home_title)}">',
        f'<meta property="og:description" content="{_e(home_desc)}">',
        _jsonld({"@context": "https://schema.org", "@type": "WebSite", "name": "Bhaau", **({"url": f"{site}/"} if site else {})}),
    ])
    home = _set_head(shell, home_title, home_desc)
    home = _replace(home, "head", home_head)
    home = _replace(home, "body", home_body)
    home = _replace(home, "product", "")
    home = _replace(home, "foot", _footer_links("", contact))
    home = _replace(home, "analytics", beacon)
    shell_path.write_text(home, encoding="utf-8")

    # ------------------------------------------------------------ sitemap + robots
    robots = "User-agent: *\nAllow: /\n"
    if site:
        lastmod = updated.date().isoformat()
        urls = [f"{site}/"] + [f"{site}/{s}/" for s in sorted(written)] + [f"{site}/privacy/"]
        sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
                   "".join(f"  <url><loc>{_e(u)}</loc><lastmod>{lastmod}</lastmod><changefreq>daily</changefreq></url>\n" for u in urls) +
                   "</urlset>\n")
        (web_dir / "sitemap.xml").write_text(sitemap, encoding="utf-8")
        robots += f"Sitemap: {site}/sitemap.xml\n"
    else:
        log.warning("SITE_URL is not set: model pages written without canonical URLs, and no sitemap.xml")
    (web_dir / "robots.txt").write_text(robots, encoding="utf-8")
    log.info("Wrote %d model pages%s", len(written), " + sitemap.xml" if site else "")
    return len(written)
