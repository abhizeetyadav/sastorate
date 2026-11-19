# Bhaau: iPhone prices across Nepal

This project tracks iPhone prices at Apple sellers in Nepal and shows them on a website. A scraper collects prices through **Webshare.io** proxies and saves the price history in SQLite. A static website then compares prices for each model and storage size and shows how they change over time. GitHub Actions runs everything for free: it scrapes every 6 hours and publishes the site on GitHub Pages.

```
nepal-iphone-tracker/
├── scraper/                 Python scraper
│   ├── config/stores.yaml   the stores to track. Edit this file to add a store or tune one
│   ├── proxies.py           Webshare: rotating endpoint or API proxy list
│   ├── fetch.py             retries, proxy rotation, robots.txt, headless-browser fallback
│   ├── extract.py           JSON-LD → Next.js/embedded JSON → CSS selectors → heuristics
│   ├── normalize.py         "Apple iPhone 17 Pro Max 12GB/256GB" → iPhone 17 Pro Max · 256GB
│   ├── pipeline.py          listing pages → product pages → cheapest offer per variant
│   ├── db.py / export.py    SQLite history that only stores changes → web/data/*.json
│   └── __main__.py          CLI
├── web/                     the website (plain HTML/CSS/JS, no build step)
│   └── data/                latest.json + history.json (currently DEMO data)
├── scripts/                 demo data generator, single-file HTML bundler
├── alerts/                  price-drop email service (Cloudflare Worker + D1)
├── tests/                   extractor + parser tests
└── .github/workflows/       scheduled scrape + Pages deploy
```

## Stores tracked

| Store | Type | Notes |
|---|---|---|
| Evo Store | Apple authorized | OpenCart. URLs and selectors were checked against the live site |
| Oliz Store | Apple authorized | Finds its iPhone pages automatically |
| Joy Store | Apple authorized | Finds its iPhone pages automatically |
| Fatafat Sewa | Authorized reseller | Next.js. The category URLs were checked against the live site |
| Hukut | Retailer | JavaScript site. Reads its product sitemap, and uses the headless browser when a page needs it |
| CG Digital | Retailer | JavaScript site. Apple category `/type-apple-124`, opened in the headless browser |
| Dursachar | Retailer | **Disabled.** Its robots.txt blocks automated visitors, and it refused proxy connections |
| Daraz | Marketplace | **Disabled.** Its robots.txt disallows catalog pages |

## 1. Run it locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium                  # only needed for JS-heavy stores

cp .env.example .env                                   # add your Webshare credentials
python -m scraper proxies                              # check: prints exit IPs
python -m scraper run                                  # scrape all stores and export JSON
python -m http.server -d web 8000                      # open http://localhost:8000
```

Useful flags:

```bash
python -m scraper run --store evostore --dry-run       # one store, print results, don't save
python -m scraper run --no-proxy                       # connect directly without Webshare
python -m scraper -v run                               # debug logging
```

## 2. Webshare setup

There are two modes. Set the mode with `WEBSHARE_MODE` in `.env` (or as a GitHub variable).

**`rotating` (recommended).** Every request goes out from a new IP.
In the Webshare dashboard, open **Proxy List → Rotating Proxy Endpoint** and copy the username (it ends in `-rotate`) and the password into `WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD`. The host defaults to `p.webshare.io:80`.

**`list`.** The scraper downloads your proxy list through the Webshare API and rotates through it itself. Proxies that fail repeatedly are retired.
Create a key under **API → API Keys** and set `WEBSHARE_API_KEY`. To keep only proxies from certain countries, set `WEBSHARE_COUNTRIES=IN,SG`.

Both modes log in with username and password. That matters on GitHub Actions, where the runner's IP changes every run, so Webshare's IP authorization won't work there.

The free Webshare plan (10 datacenter proxies) is enough for this volume: about 8 stores × ~70 pages, 4 times a day. If a store starts blocking datacenter IPs, switch to residential proxies. No code changes are needed.

## Admin panel

```powershell
python -m scraper admin
```

This opens the panel in your browser at `http://127.0.0.1:8765`. It runs only on your computer: nothing is put on the internet, and only this computer can reach it. Close it with **Ctrl+C** in the terminal.

| Section | What you do there |
|---|---|
| **Status** | See how each shop's last scrape went and what needs attention. **Scrape now** updates one shop's prices on this computer; **Test a page** checks whether Bhaau can read a page. A live log shows the output. |
| **Shops** | Edit name, website, Apple authorized, on/off, logo, details (warranty, EMI, delivery, returns, hours, phone) and branches by city. **Add a shop** with just a name and website; it's found automatically, then test it with Scrape now. |
| **Prices** | Every shop's latest price for a model and the page it came from. **Hide** a wrong price (one size, the whole model, or everything from a shop, optionally until a date) and **Show again** later. |
| **Images** | Set a model's image from an image address or a file. The same checks as the command line (no banners or dark backgrounds) apply, with **Use it anyway** if you're sure. |
| **Publish** | Puts your changes live (`scripts/deploy.py`). Tick **Also update prices now** to refresh prices on GitHub at the same time. |

Each save rebuilds the site on your computer. **Preview the site**, in the panel's sidebar, shows it before you publish.

**Where changes are saved:** the panel writes `scraper/config/shop_info.yaml`, which is laid over `stores.yaml`, so your hand-written scraping settings and comments are never rewritten. It also writes `overrides.yaml`, `models.yaml`, and the files in `web/logos/` and `web/images/`. Everything is committed when you publish, so every change can be undone with git.

## 3. Deploy (GitHub Actions + Cloudflare Pages, free)

- **GitHub Actions** runs the scraper every 6 hours and keeps the price history in the repository.
- **Cloudflare Pages** hosts the website. Its free plan allows commercial use (affiliate links, paid listings, ads) and has no bandwidth cap, unlike GitHub Pages and Vercel's free plan.

### One command

Install once (PowerShell), then open a **new** terminal:

```powershell
winget install --id Git.Git
winget install --id GitHub.cli
winget install OpenJS.NodeJS.LTS
```

Sign up at dash.cloudflare.com (free). Then, in the project folder with `.venv` active:

```powershell
python scripts/deploy.py --dry-run    # see what it will do; changes nothing
python scripts/deploy.py              # deploy
python scripts/deploy.py --alerts     # deploy + the price-alert service
```

It does, in order, and is safe to run again:

1. **Checks:** git, gh and Node installed, you're signed in to GitHub (opens the browser if not), Webshare details in `.env`, `stores.yaml` valid, tests pass. Makes a `STATUS_KEY` if you don't have one.
2. **Git:** creates the repository if needed and refuses to continue if any file to be published contains one of your secrets. `.env`, `.venv`, saved probe pages and update zips are never published.
3. **GitHub:** creates the repository (public: free, unlimited Actions minutes) and pushes.
4. **Cloudflare Pages:** asks for a Cloudflare API token the first time (it shows you how to make one: *Custom token → Account / Cloudflare Pages / Edit*), creates the Pages project and saves the site address (`https://bhaau.pages.dev` or similar) as `SITE_URL`. If `SITE_URL` in `.env` is your own domain, it adds the domain to the project and tells you which DNS record to create.
5. **Alerts** (`--alerts` only): creates the Cloudflare database, deploys the Worker, stores its secrets, saves `ALERTS_URL`.
6. **Settings:** copies secrets from `.env` to GitHub **Secrets** (sent privately, never on the command line) and the rest to **Variables**.
7. **First run:** starts the scrape and follows it live (10–20 minutes). The last job uploads `web/` to Cloudflare Pages.
8. **Live check:** loads the home page, a model page, `latest.json`, the privacy page, sitemap and status page, checks that unknown addresses return a real 404, and tests the alert service.

Check the live site any time: `python scripts/deploy.py --check`

**Your own domain:** set `SITE_URL=https://bhaau.com.np` in `.env` and run the script again. A bare domain like `bhaau.com.np` needs its DNS on Cloudflare (change the nameservers at your registrar to the two Cloudflare gives you); a `www.` subdomain can use a CNAME at any provider.

**GitHub Pages instead:** `python scripts/deploy.py --host github`. Note that GitHub Pages doesn't allow sites mainly aimed at commercial transactions.

### Re-deploying

After editing code, `stores.yaml` or unzipping an update, run `python scripts/deploy.py` again. Once the site is live, **GitHub's copy of the price database is the real one** (the bot adds to it every 6 hours), so the script first copies it to your computer, keeping your old local copy as `data/prices.<date>.backup.db`, and then pushes your changes on top.

### Day-to-day operations

| Task | How |
|---|---|
| Run a scrape now | GitHub → Actions → **Scrape prices & deploy site** → Run workflow, or `gh workflow run scrape.yml` |
| Pause all scraping | `gh workflow disable scrape.yml` (`enable` to resume) |
| See why a run failed | `gh run list --limit 5`, then `gh run view <id> --log-failed` |
| Change a secret (e.g. new Webshare password) | edit `.env`, run `python scripts/deploy.py --no-run` |
| Undo a bad change | `git revert <commit>` then `python scripts/deploy.py` |
| Hide a wrong price | see **Keeping it healthy** below |

### Doing it by hand instead

1. Push this folder to a new **public** GitHub repo.
2. In Cloudflare: **Workers & Pages → Create → Pages → Upload assets**, name the project `bhaau` (you can upload the `web/` folder once to try it). Create an API token with **Account / Cloudflare Pages / Edit**, and copy your **Account ID** (shown on the Workers & Pages overview).
3. In GitHub, **Settings → Secrets and variables → Actions**:
   - Secrets: `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `WEBSHARE_PROXY_USERNAME`, `WEBSHARE_PROXY_PASSWORD` (or `WEBSHARE_API_KEY`), plus optional `STATUS_KEY`, `OWNER_EMAIL`, `RESEND_API_KEY`, `ALERTS_TOKEN`
   - Variables: `SITE_URL`, `CF_PAGES_PROJECT` (if not `bhaau`), and optionally `WEBSHARE_MODE`, `WEBSHARE_COUNTRIES`, `CONTACT_EMAIL`, `ALERTS_URL`, `MAIL_FROM`
4. **Actions → "Scrape prices & deploy site" → Run workflow**

The site is static, so other hosts work too: point them at the `web/` folder and delete the two "Publish" jobs from `.github/workflows/scrape.yml`.

## 4. Fix or add a store

**A price looks wrong?** See exactly where it came from:

```powershell
python -m scraper inspect "iPhone 17"
```

It lists each store's latest price for that model with the page title and address it was read from. If the title names a different model, or the page is a blog post or search page, that's the culprit. Articles, news, comparisons, guides and search results are never read as a single product, and prices read from them earlier are hidden.


Store sites change. When a store suddenly shows 0 prices, probe one of its pages:

```bash
python -m scraper probe https://evostore.com.np/apple/iphone --store evostore
python -m scraper probe https://www.olizstore.com/ --js --save oliz.html   # render JS, keep the HTML
```

The probe prints every offer it found, which extractor found it, and how the offer was classified. It also lists the product links it would follow. Then, in `stores.yaml`:

- **Nothing found and the HTML is tiny?** The page is rendered by JavaScript. Set `render_js: true`.
- **Wrong or missing prices?** Add `selectors:` (`card`, `name`, `link`, `price`, `old_price`). Copy the CSS classes from the saved HTML or from your browser's dev tools.
- **Wrong pages crawled?** Set `start_urls:` explicitly and/or `product_link_pattern:` (a regex).
- **Pagination?** Use `page_param: page`, which adds `?page=2`, `?page=3` and so on.

A new store only needs `id`, `name`, `url` and `discover: true` to start with.

### Model images (optional)

Every model shows the photo from a seller's listing, or a drawn phone if there is none. To use your own image for a model everywhere instead:

```powershell
python -m scraper image "iPhone 17 Pro" https://example.com/iphone-17-pro.png --credit "Image: Apple"
python -m scraper image "iPhone 17" C:\Users\you\Pictures\iphone-17.jpg --credit "Photo: Bhaau"
python -m scraper image --list                      # what you've set
python -m scraper image "iPhone 17 Pro" --remove    # back to the store's photo
```

For a web image, right-click it and choose **Copy image address**; a link to a web page won't work. The image is downloaded once, trimmed of empty borders, resized and saved as `web/images/models/<model>.webp`, so the site serves it itself and it never breaks when a store changes. The credit appears under the image on the model page. Then run `python -m scraper export` to see it, and `python scripts/deploy.py` to publish. You choose every image, so check you're allowed to use it.

### Marketplaces: HamroBazar (optional)

Classified sites have many individual sellers, often cheaper because the phone is sold without a VAT bill. Bhaau shows them **apart from shops**: in their own box on each model page, with a warning about bills, warranty and NTA IMEI registration. They're never the "lowest price", and never in price drops, price history, seller counts or price alerts.

Add to `scraper/config/stores.yaml` under `stores:`:

```yaml
  - id: hamrobazar
    name: HamroBazar
    url: https://hamrobazaar.com
    kind: marketplace            # individual sellers: own box with a warning, never the "lowest price"
    authorized: false
    render_js: true              # its listings load with JavaScript
    capture_json: false          # its /api/ is off-limits in robots.txt: read only the page
    follow_product_pages: false  # don't open each seller's ad
    search_url: "https://hamrobazaar.com/search/product?q={query}"
    selectors:                   # HamroBazar's ad cards
      card: '[data-testid="product-card"]'
      name: '[data-testid="product-card-title"]'
      price: '[data-testid="product-card-price"]'
      link: '[data-testid="product-card-title"]'
      condition: '[data-testid="product-condition-badge"]'
```

Each run, after the shops, it searches HamroBazar once for every model the shops sell. An ad is used only if HamroBazar's condition badge says **Brand new**, and its title or web address doesn't show it's used ("95% BH", "98% battery", "used"), a copy, an accessory, a "wanted" post, EMI or rent. It must also name a model, and cost between 70% and 105% of the cheapest shop price for that exact phone. An ad without storage is filed under the model's base storage and marked "storage not stated", but only if its price fits that storage. Much cheaper is almost always bait or a copy. The cheapest ads that pass are shown. Seller phone numbers are removed from titles, and no seller names or contact details are stored.

### Shop details (optional, recommended)

When shops charge the same, buyers choose on warranty, EMI, delivery and location. Add what you know to each shop in `scraper/config/stores.yaml`. Every field is optional, and only what you enter is shown:

```yaml
  - id: evostore
    name: Evo Store
    ...
    details:
      warranty: "1 year Apple warranty through the official distributor"
      emi: "Card EMI with partner banks; 0% offers on some models"
      delivery: "Free inside Kathmandu valley; 2-4 days outside"
      returns: "7-day replacement for factory defects"
      hours: "10 am to 7 pm, every day"
      phone: "01-XXXXXXX"
      note: "Exchange offers on older iPhones"
      locations:                             # every branch, up to 30
        - name: "New Road"
          city: "Kathmandu"                    # branches are grouped by city
          address: "New Road, Kathmandu"
          phone: "98XXXXXXXX"
          map: "https://maps.app.goo.gl/..."   # Google Maps "Share" link
        - name: "Lakeside"
          city: "Pokhara"
          address: "Lakeside Road"
```

The details appear under each shop's price on every model page, and on the shop's card in "Sellers we compare". Check them with the shop, and keep them current.

### Colours and the three ways to buy

These need no setup. Each run reads colour names from the shops' listings ("iPhone 18 Pro Max — Burgundy — 256GB") and shows which shop has which colour in stock. Shops that don't name colours on their websites are listed as "doesn't show colours". The search box also matches colours ("17 pro max cosmic orange").

With HamroBazar enabled, each model page also starts with **Three ways to buy**:
- **From a shop:** the lowest shop price, with a VAT bill and warranty.
- **New, without VAT bill:** the cheapest believable brand-new ad.
- **Used:** a typical used price from at least 3 ads, with the range most ads fall in. Copies, parts, iCloud-locked phones and bait prices are left out.

### Store logos (optional)

Stores show coloured initials until you add their logo. Ask the store for its logo (or for permission) first, then:

1. Put the file in `web/logos/`, e.g. `web/logos/hukut.svg`. SVG is best; PNG or WebP at least 96 × 96 px also work. Square logos on a transparent or white background look best, because each logo sits on a small white tile in both light and dark mode.
2. Add one line to the store in `scraper/config/stores.yaml`:
   ```yaml
   - id: hukut
     name: Hukut
     logo: logos/hukut.svg
   ```
3. Commit both, and the next run shows it on the price lists and the Sellers section.

If the file is missing, misspelled or fails to load, the store quietly falls back to its initials and the run log says why. Web addresses aren't accepted, so the site never loads images from store servers.

## How it works

- **Extraction.** Every page goes through four extractors, in order. Structured data (schema.org JSON-LD, Next.js `__NEXT_DATA__` and RSC payloads) is preferred. If none is found, the scraper falls back to configured selectors and then to a heuristic that pairs an "iPhone" link with a nearby `Rs./NPR` price. Lakh-style numbers (`1,44,900`) and Devanagari digits (`१,४४,९००`) are parsed.
- **Normalisation.** Titles are mapped to a model plus storage. Accessories ("case for iPhone 17") and refurbished or used listings are dropped. When a title lists both RAM and storage, the larger number is taken as storage.
- **History.** A new row is written only when a price, old price or stock status changes. Otherwise only `last_seen` is updated. This keeps the database small enough to commit.
- **Website.** Search, series chips, storage filter, "authorized only" and "in stock only" toggles, and sort by price, biggest saving or 7-day drop. Each card shows a price ladder across stores and a sparkline. Clicking a card opens a step chart of price history per store. Filters are kept in the URL, so filtered views can be shared.

## Google (search-engine pages)

Every export also writes a real page for each iPhone model, e.g. `web/iphone-17-pro/index.html`, so Google can index it. Each page has:

- its own title and description, e.g. "Apple iPhone 17 Pro Price in Nepal (November 2026): from Rs 2,13,899";
- the price table written into the page;
- schema.org `Product` + `AggregateOffer` data, which Google can use to show prices in results.

The export also writes `sitemap.xml` and `robots.txt`, and puts a plain price list on the homepage that links to every model page.

Set **`SITE_URL`** (in `.env`, and as a GitHub Actions *variable*) to your site's public address. Pages get correct canonical links and the sitemap is written only when it's set.

After launch, add the site to **Google Search Console** (search.google.com/search-console) and submit `https://your-site/sitemap.xml`. Search Console shows which pages are indexed and which searches bring visitors.

Pages for models that are no longer sold are removed automatically. Only folders created by the generator, which contain a `.bhaau-page` file, are ever deleted.

## Price-drop alerts (email)

Visitors can enter their email on any phone's page, optionally with a target price, and get **one email when the price drops**. The system works like this:

- **On each phone's page:** an "Email me when the price drops" form.
- **`alerts/worker.js`:** a small Cloudflare Worker with a D1 (SQLite) database. It stores sign-ups, sends the confirmation email (double opt-in), and handles unsubscribing. Subscriber emails stay on Cloudflare; they never go into GitHub or the website's files.
- **The scraper:** after every run it sends the Worker the lowest **in-stock** price of each phone. The Worker emails everyone whose phone got cheaper than the price they last heard about, and at or below their target, if they set one. Each person gets **one email per run**, listing all their price drops.
- **Unsubscribing:** every email has an unsubscribe link, plus mail apps' built-in one-click unsubscribe.

Everything runs on free plans:
- **Resend:** 3,000 emails a month, at most 100 a day. When there are more price-drop emails than that, the rest go out in the next run. Nothing is lost.
- **Cloudflare Workers:** 100,000 requests a day.

**Viber** isn't supported. New Viber bots need a commercial contract with Viber, at about EUR 100 a month plus a charge per message.

### Setup (one time, about 30 minutes)

You need your own domain, because Resend only sends from domains you have verified.

1. **Resend:**
   1. Sign up at resend.com.
   2. Go to Domains → Add domain (e.g. `bhaau.com.np`) and add the DNS records it shows at your domain provider.
   3. Go to API Keys → Create, and copy the key.
2. **Cloudflare:**
   1. Sign up at dash.cloudflare.com.
   2. Install Node.js from nodejs.org.
   3. Run, in PowerShell:
      ```powershell
      cd alerts
      npx wrangler login
      npx wrangler d1 create bhaau-alerts      # copy the database_id it prints into wrangler.toml
      npx wrangler d1 execute bhaau-alerts --remote --file=schema.sql
      ```
3. **Edit `alerts/wrangler.toml`:**
   - `SITE_URL`: your site, without a slash at the end.
   - `SITE_ORIGINS`: your site. Keep `http://localhost:8000` too if you want to test locally.
   - `MAIL_FROM`: an address on your verified domain.
4. **Add the secrets and deploy:**
   ```powershell
   npx wrangler secret put RESEND_API_KEY    # paste the Resend key
   npx wrangler secret put ALERTS_TOKEN      # make up a long random password
   npx wrangler deploy                       # prints the Worker's address, e.g. https://bhaau-alerts.you.workers.dev
   ```
5. **Tell the scraper where the Worker is.** In `.env`:
   ```
   ALERTS_URL=https://bhaau-alerts.you.workers.dev
   ALERTS_TOKEN=the same password as above
   ```
   On GitHub, add `ALERTS_URL` as a **variable** and `ALERTS_TOKEN` as a **secret** (Settings → Secrets and variables → Actions).
6. **Run the scraper once.** `python -m scraper run` sends today's prices to the Worker, and the form appears on the website.

To see how many people have signed up:
```powershell
curl.exe -H "Authorization: Bearer YOUR_ALERTS_TOKEN" https://bhaau-alerts.you.workers.dev/stats
```

To run the Worker's tests (Node.js 22+): `node --test alerts/test/worker.test.mjs`

**Privacy:** the privacy page (`/privacy/`) is generated for you and describes exactly what the alert service stores. Unconfirmed sign-ups are deleted after 7 days, and unsubscribing deletes the alert. If you change what's stored, update the text in `scraper/seo.py` (`_privacy_body`) and its `PRIVACY_UPDATED` date.

## Privacy page and contact links

Every run writes `web/privacy/index.html` and puts **Privacy**, **Report a wrong price** and **Contact** links in the footer of every page. Model pages also get a "Report a wrong price" link under the price list; it opens an email with the phone, storage and page address already filled in.

Set the address once:

- GitHub: **Settings → Secrets and variables → Actions → Variables**: `CONTACT_EMAIL` = e.g. `hello@yourdomain.com`
- Locally: `CONTACT_EMAIL=` in `.env`

It's shown publicly, so use a dedicated address rather than your personal one. Without it, the footer shows only the Privacy link.

The privacy page only mentions price alerts when `ALERTS_URL` is set. It's written in plain language and matches what the code does; it isn't legal advice, so if you add analytics, ads or anything else that collects data, update it.

## Visitor stats (optional)

Cloudflare Web Analytics counts visits without cookies and without tracking people across sites, and it's free. To turn it on:

1. Cloudflare dashboard → **Analytics & Logs** → **Web Analytics** → **Add a site** → enter your site address.
2. Copy the token from the snippet it shows: the 32 letters and digits after `"token": "`.
3. Add `CF_ANALYTICS_TOKEN=<token>` to `.env` and run `python scripts/deploy.py`. It goes to GitHub as a variable, and every public page gets the beacon. The private status page never does.

The privacy page updates itself to say you count visits this way. Remove the token to switch it off.

## Keeping it healthy

Three small tools so you don't need an admin panel.

### Hide a wrong price: `scraper/config/overrides.yaml`

If a store lists the wrong price (a typo, or a 128 GB page showing the 256 GB price), hide it instead of deleting data:

```yaml
hide:
  - store: joystore            # store id from stores.yaml
    model: iPhone 16           # optional - leave out to hide the whole store
    storage: 128GB             # optional - 128, "128GB" and "1 TB" all work
    until: 2026-12-31          # optional - rule switches itself off after this date
    reason: lists the 256 GB price
```

The price stays in the database but disappears from the website, the model pages, price history and price-drop alerts. Commit the file and the next run (or **Run workflow**) applies it. Delete the rule to show the price again. A rule with a typo is skipped and logged rather than breaking the run.

### "A store broke" email

After every scrape the workflow runs `python -m scraper health --notify`. It emails you when a store:

- failed with an error, or found **no phones** at all
- found **half or fewer** of the phones it usually finds
- hasn't been checked for **30 hours**
- shows a price that moved **more than 30%** in one go (usually a scraping mistake - hide it with an override if so)

Each problem is emailed at most **once a day**, and it's forgotten once it's fixed, so you'll hear again if it comes back. The run is also marked red in GitHub Actions, and GitHub sends its own email for failed runs, which works as a backup. The site still deploys with the other stores' prices.

Setup (**Settings → Secrets and variables → Actions**):

| Name | Where | Value |
|---|---|---|
| `RESEND_API_KEY` | Secret | Your Resend API key (free plan is plenty) |
| `OWNER_EMAIL` | Secret | Where problem emails go |
| `MAIL_FROM` | Variable | e.g. `Bhaau <alerts@yourdomain.com>`; leave empty for Resend's test sender, which only delivers to your own Resend account email |
| `STATUS_KEY` | Secret | A random string, see below |

Without these the check still runs and writes its report to the run's summary page; it just doesn't email.

Run it yourself any time: `python -m scraper health` (prints problems, sends nothing).

### Private status page

Set `STATUS_KEY` (make one with `python -c "import secrets;print(secrets.token_urlsafe(18))"`) and each run publishes:

```
https://<your site>/status-<STATUS_KEY>/
```

It shows current problems, every store's last check and how many phones it found compared with usual, the last 12 checks as coloured bars, the last error, price changes in the last 24 hours, your active hide rules and alert-signup numbers. It is `noindex` and not linked from anywhere, so Google won't list it. Bookmark it on your phone.

It contains no personal data (no emails, only counts), but anyone with the link can open it. **If your GitHub repo is public, the address is visible in the repo's `web/` folder**, so treat it as "unlisted", not secret. To change the address, change `STATUS_KEY`; the old page is removed on the next run.

## Scraping responsibly

- `respect_robots: true` (the default) skips any URL a store's robots.txt disallows.
- The scraper waits a random 2–5 seconds between requests to the same store and runs only every 6 hours. A full run is a few hundred page views, spread across stores.
- Only public prices are collected. No logins are used and no personal data is collected.
- Check each store's terms. If a store asks you to stop, set `enabled: false`.

## Tests

```bash
pytest -q
```

## Demo data

`web/data/` ships with **generated demo prices** so you can see the site right away. A yellow banner marks the site as a demo. The first real `python -m scraper run` overwrites these files. To rebuild the demo: `python scripts/make_demo_data.py`.
