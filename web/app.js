/* Bhaau: iPhone price comparison for Nepal. No dependencies.
 * Views: catalog (#) and product page (#iphone-17-pro-max).
 * Data: data/latest.json + data/history.json (or inlined window.__LATEST__ / __HISTORY__).
 */
(() => {
  "use strict";

  // ================================================================ helpers
  const SELLER_COLORS = ["#2f5da8", "#c2462e", "#1f8a5b", "#b7851f", "#7a4fa3", "#1e8c93", "#8c6d58", "#5f6b7a"];
  const nf = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
  const num = (n) => (n == null ? "" : nf.format(n));
  const rs = (n) => (n == null ? "" : "Rs " + nf.format(n));
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");
  // images: store photos are full web addresses; your own model images are paths inside the site
  const imgSrc = (u) => (/^https?:\/\//i.test(u || "") ? u : u && !/^[a-z]+:|^\/\//i.test(u) ? ROOT + u.replace(/^\/+/, "") : "#");
  const gbLabel = (g) => (!g ? "" : g >= 1024 ? `${g / 1024} TB` : `${g} GB`);
  const slugify = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const ktm = (o) => new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kathmandu", ...o });
  const fmtStamp = (iso) => ktm({ day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit", hour12: true }).format(new Date(iso));
  const fmtDay = (iso) => ktm({ day: "numeric", month: "short" }).format(new Date(iso));
  const monthYear = (iso) => ktm({ month: "long", year: "numeric" }).format(new Date(iso));
  function ago(iso) {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  }
  // A store's logo (web/logos/, set in stores.yaml) on a white tile, or its initials on its colour.
  const sellerMark = (s) => s.logo
    ? `<span class="monogram has-logo" data-i="${esc(initials(s.name))}" data-c="${esc(s.color || "")}"><img src="${esc(ROOT + s.logo)}" alt="" loading="lazy" decoding="async"></span>`
    : `<span class="monogram" style="background:${s.color}">${esc(initials(s.name))}</span>`;
  // If a logo file is missing or broken, quietly fall back to the initials.
  document.addEventListener("error", (e) => {
    const img = e.target;
    if (!(img instanceof HTMLImageElement) || !img.parentElement?.classList.contains("has-logo")) return;
    const m = img.parentElement;
    m.classList.remove("has-logo"); m.style.background = m.dataset.c; m.textContent = m.dataset.i;
  }, true);
  const initials = (name) => name.replace(/\(.*\)/, "").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  const ICON_X = `<svg viewBox="0 0 10 10" aria-hidden="true"><path d="m2 2 6 6M8 2 2 8"/></svg>`;
  const ICON_CHECK = `<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 .8 7.4 2l1.8-.2.6 1.7 1.6.9-.5 1.7.5 1.7-1.6.9-.6 1.7L7.4 10 6 11.2 4.6 10l-1.8.2-.6-1.7-1.6-.9.5-1.7L.6 4.2l1.6-.9.6-1.7L4.6 2zM5.2 7.9l3.5-3.5-.8-.8-2.7 2.7-1.3-1.3-.8.8z"/></svg>`;

  const PRICE_BANDS = [
    ["u150", "Under 1.5 lakh", 0, 150000],
    ["150-250", "1.5 to 2.5 lakh", 150000, 250000],
    ["250-350", "2.5 to 3.5 lakh", 250000, 350000],
    ["o350", "Over 3.5 lakh", 350000, Infinity],
  ];

  // ================================================================ state
  // A model page lives one folder down (/iphone-17-pro/) and says so on <body>.
  // The single-file preview build inlines its data and keeps using #hash links.
  const ROOT = document.body.dataset.root || "./";
  const PAGE_SLUG = document.body.dataset.slug || "";
  const TEXT_PAGE = document.body.dataset.page || "";   // e.g. the privacy page: header + footer only
  const INLINE = !!window.__LATEST__;
  const modelHref = (slug, gb) => (INLINE ? `#${slug}` : `${ROOT}${slug}/${gb ? `?storage=${gb}` : ""}`);
  const homeHref = (series, anchor) => (INLINE ? "#" : `${ROOT}${series ? `?series=${encodeURIComponent(series)}` : ""}${anchor ? `#${anchor}` : ""}`);
  const params = new URLSearchParams(location.search);

  const state = {
    q: "", series: new Set(), storage: new Set(), bands: new Set(), sellers: new Set(),
    authOnly: false, stockOnly: false, sort: "newest",
    pick: {}, range: "3m", mode: "lowest", pStock: false,
    emi: { open: false, down: 0, months: 12, rate: 12, flat: false },  // 12%: typical shop card EMI in Nepal
  };
  let DATA, HISTORY = {}, SELLERS = {}, MODELS = [], BY_SLUG = {};

  // ================================================================ theme
  const THEMES = ["auto", "light", "dark"];
  const themeNow = () => document.documentElement.dataset.theme || "auto";
  function setTheme(t) {
    if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
    $("#themeLabel").textContent = t[0].toUpperCase() + t.slice(1);
    $("#themeBtn").setAttribute("aria-label", `Colour theme: ${t}. Select to change.`);
    try { t === "auto" ? localStorage.removeItem("bhaau-theme") : localStorage.setItem("bhaau-theme", t); } catch {}
  }
  $("#themeBtn").addEventListener("click", () => setTheme(THEMES[(THEMES.indexOf(themeNow()) + 1) % 3]));
  setTheme(themeNow());

  // ================================================================ phone artwork (used until real photos are scraped)
  const FINISH = [[/1[89] Pro/, "#3e4c60"], [/17 Pro/, "#d0773f"], [/16 Pro/, "#b8a58e"], [/15 Pro/, "#5f6672"], [/Air/, "#cfdcea"],
    [/\d+e\b/, "#f0f0ee"], [/Duo|Fold/, "#34373d"], [/18/, "#a8c3d8"], [/17/, "#a9c0ae"], [/16/, "#8faad6"], [/15/, "#e8c8cf"]];
  let artId = 0;
  function phoneArt(model) {
    const id = "g" + ++artId;
    const fin = (FINISH.find(([rx]) => rx.test(model)) || [0, "#c9ced6"])[1];
    const lens = (x, y, r = 6.4) => `<circle cx="${x}" cy="${y}" r="${r + 1.6}" fill="rgba(0,0,0,.18)"/><circle cx="${x}" cy="${y}" r="${r}" fill="#15181d"/><circle cx="${x - 1.8}" cy="${y - 1.8}" r="${r * 0.32}" fill="#3b4250"/>`;
    const fold = /Duo|Fold/.test(model);
    let cam;
    if (fold) cam = `<rect x="18" y="16" width="34" height="18" rx="9" fill="rgba(0,0,0,.14)"/>${lens(27, 25, 5)}${lens(43, 25, 5)}`;
    else if (/1[789] Pro/.test(model)) cam = `<rect x="22" y="14" width="76" height="42" rx="11" fill="rgba(0,0,0,.12)"/>${lens(36, 26)}${lens(36, 44)}${lens(52, 35)}<circle cx="86" cy="25" r="2.6" fill="#f3e7c3"/>`;
    else if (/Pro/.test(model)) cam = `<rect x="26" y="16" width="38" height="38" rx="10" fill="rgba(0,0,0,.12)"/>${lens(37, 27)}${lens(37, 44)}${lens(53, 35.5)}<circle cx="55" cy="22" r="2.4" fill="#f3e7c3"/>`;
    else if (/Air/.test(model)) cam = `<rect x="22" y="16" width="76" height="20" rx="10" fill="rgba(0,0,0,.12)"/>${lens(34, 26, 5.6)}<circle cx="50" cy="26" r="2.2" fill="#f3e7c3"/>`;
    else if (/\d+e\b/.test(model)) cam = `${lens(35, 27)}<circle cx="35" cy="42" r="2.2" fill="#f3e7c3"/>`;
    else cam = `<rect x="27" y="16" width="20" height="40" rx="10" fill="rgba(0,0,0,.12)"/>${lens(37, 26, 5.6)}${lens(37, 46, 5.6)}<circle cx="53" cy="22" r="2.2" fill="#f3e7c3"/>`;
    const body = fold
      ? `<rect x="12" y="8" width="96" height="164" rx="14" fill="${fin}"/><line x1="60" y1="10" x2="60" y2="170" stroke="rgba(0,0,0,.18)" stroke-width="1.2"/><rect x="12" y="8" width="96" height="164" rx="14" fill="url(#${id})"/>`
      : `<rect x="20" y="8" width="80" height="164" rx="16" fill="${fin}"/><rect x="20" y="8" width="80" height="164" rx="16" fill="url(#${id})"/>`;
    return `<svg class="phone-img" viewBox="0 0 120 194" role="img" aria-label="${esc(model)}">
      <defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".28"/><stop offset=".55" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".12"/></linearGradient></defs>
      <ellipse cx="60" cy="184" rx="38" ry="5" fill="rgba(0,0,0,.10)"/>${body}
      <rect x="${fold ? 12.5 : 20.5}" y="8.5" width="${fold ? 95 : 79}" height="163" rx="${fold ? 13.5 : 15.5}" fill="none" stroke="rgba(0,0,0,.16)"/>${cam}</svg>`;
  }
  function productImage(m) {
    if (!m.image) return phoneArt(m.model);
    return `<img class="phone-img" src="${esc(imgSrc(m.image))}" alt="${esc(m.model)}" loading="lazy" referrerpolicy="no-referrer" data-model="${esc(m.model)}">`;
  }
  // swap broken remote images for the drawn phone
  document.addEventListener("error", (e) => {
    const img = e.target;
    if (img.tagName === "IMG" && img.dataset.model) img.outerHTML = phoneArt(img.dataset.model);
  }, true);

  // ================================================================ data
  async function getJSON(path, inline) {
    if (inline) return inline;
    const r = await fetch(path, { cache: "no-cache" });
    if (!r.ok) throw new Error(path);
    return r.json();
  }

  async function load() {
    try { DATA = await getJSON(ROOT + "data/latest.json", window.__LATEST__); }
    catch {
      $("#catalog").innerHTML = `<div class="panel empty-state" style="margin-top:24px"><h3>No prices yet</h3><p>Run <code>python -m scraper run</code> to collect prices, then reload this page.</p></div>`;
      return;
    }
    try { HISTORY = await getJSON(ROOT + "data/history.json", window.__HISTORY__); } catch { HISTORY = {}; }
    DATA.stores.forEach((s, i) => (SELLERS[s.id] = { ...s, color: SELLER_COLORS[i % SELLER_COLORS.length] }));

    const byModel = new Map();
    for (const v of DATA.variants) {
      if (!byModel.has(v.model)) byModel.set(v.model, { model: v.model, series: v.series, slug: slugify(v.model), variants: [], image: null });
      const m = byModel.get(v.model);
      m.variants.push(v);
      m.image = m.image || v.image || null;
      m.image_credit = m.image_credit || v.image_credit || null;
    }
    MODELS = [...byModel.values()];
    MODELS.forEach((m, i) => { m.order = i; m.variants.sort((a, b) => (a.storage_gb || 1e9) - (b.storage_gb || 1e9)); BY_SLUG[m.slug] = m; });

    $("#demo").hidden = !DATA.demo;
    $("#footMeta").textContent = `Prices updated ${fmtStamp(DATA.generated_at)} (Nepal time)`;
    if (!PAGE_SLUG && params.get("series")) state.series = new Set([params.get("series")]);
    buildSeriesBar();
    route();
  }

  // ================================================================ derived values
  const family = (m) => {
    const n = m.model.match(/iPhone (\d+)/);
    if (n) return +n[1] <= 15 ? "15" : n[1];
    return m.model.replace("iPhone ", "").replace(/ \d+$/, "");
  };
  const familyLabel = (f) => (/^\d+$/.test(f) ? (f === "15" ? "iPhone 15 and older" : `iPhone ${f} series`) : `iPhone ${f}`);
  const familyRank = (f) => (/^\d+$/.test(f) ? -+f : f === "Air" ? -17.5 : -1);
  const newestFamily = () => MODELS.map(family).filter((f) => /^\d+$/.test(f)).sort((a, b) => b - a)[0];

  function offersOf(v, { useSellerFilter = true, stock = state.stockOnly } = {}) {
    return v.offers.filter((o) => {
      const s = SELLERS[o.store];
      if (!s) return false;
      if (state.authOnly && !s.authorized) return false;
      if (useSellerFilter && state.sellers.size && !state.sellers.has(o.store)) return false;
      if (stock && o.in_stock === false) return false;
      return true;
    });
  }
  const bestOf = (offers) => offers.find((o) => o.in_stock !== false) || offers[0];

  function lowestSeries(key, sellerIds) {
    const byDay = {};
    for (const [sid, pts] of Object.entries(HISTORY[key] || {})) {
      if (!SELLERS[sid] || (state.authOnly && !SELLERS[sid].authorized)) continue;
      if (sellerIds && !sellerIds.has(sid)) continue;
      for (const [d, p] of pts) byDay[d] = Math.min(byDay[d] ?? Infinity, p);
    }
    return Object.entries(byDay).sort(([a], [b]) => (a < b ? -1 : 1));
  }
  function weekChange(v, now) {
    const s = lowestSeries(v.key);
    if (now == null || s.length < 2) return null;
    const cutoff = new Date(Date.now() - 7 * 864e5).toISOString().slice(0, 10);
    const before = s.filter(([d]) => d <= cutoff).pop();
    return before ? now - before[1] : null;
  }

  /** "Is this a good price?": today's lowest price against the last 90 days (needs 14+ days of history). */
  function priceVerdict(v, now) {
    if (now == null) return null;
    const cutoff = new Date(Date.now() - 90 * 864e5).toISOString().slice(0, 10);
    const win = lowestSeries(v.key).filter(([d]) => d >= cutoff);
    if (win.length < 14) return null;
    const prices = win.map(([, p]) => p);
    const days = win.length;
    const span = days >= 85 ? "the last 90 days" : `the ${days} days we've tracked it`;
    const low = Math.min(...prices);
    const usual = [...prices].sort((a, b) => a - b)[Math.floor(prices.length / 2)];
    const lowDay = [...win].reverse().find(([, p]) => p === low)[0];
    if (Math.max(...prices) === low && now === low) return { tone: "ok", head: "Price hasn't changed",
      text: `It has been ${rs(now)} for ${span}.` };
    if (now <= low) return { tone: "good", head: days >= 85 ? "Lowest price in 90 days" : "Lowest price we've seen",
      text: `Nothing has been cheaper in ${span}.` };
    if (now <= usual * 0.98) return { tone: "good", head: "Good price",
      text: `${rs(usual - now)} below its usual price of ${rs(usual)} over ${span}.` };
    if (now >= usual * 1.03) return { tone: "high", head: "Higher than usual",
      text: `${rs(now - usual)} above its usual price of ${rs(usual)}. It was ${rs(low)} on ${fmtDay(lowDay)}, so it may be worth waiting.` };
    return { tone: "ok", head: "Usual price", text: `Close to its usual price of ${rs(usual)} over ${span}.` };
  }

  // ---- EMI calculator: card EMI charges interest on the remaining balance;
  // hire-purchase schemes usually charge flat interest on the whole loan (often paid upfront)
  const EMI_MONTHS = [6, 9, 12, 18, 24];
  const EMI_DOWN = [0, 20, 40, 50];
  function emiCalc(price, e) {
    const loan = Math.round(price * (1 - e.down / 100));
    const n = e.months, rate = Math.max(0, +e.rate || 0);
    let monthly, interest;
    if (e.flat) {
      interest = loan * rate / 100 * (n / 12);
      monthly = (loan + interest) / n;
    } else {
      const r = rate / 1200;
      monthly = r ? loan * r * (1 + r) ** n / ((1 + r) ** n - 1) : loan / n;
      interest = monthly * n - loan;
    }
    return { downPay: price - loan, monthly: Math.round(monthly), interest: Math.round(interest), total: Math.round(price + interest) };
  }
  function emiBox(price) {
    const e = state.emi, c = emiCalc(price, e);
    const opts = (vals, cur, fmt) => vals.map((x) => `<option value="${x}"${x === cur ? " selected" : ""}>${fmt(x)}</option>`).join("");
    return `<details class="emi panel" id="emiBox"${e.open ? " open" : ""}>
      <summary><span>Pay in installments (EMI)</span><b id="emiSum">${rs(c.monthly)}/month</b></summary>
      <div class="emi-body" data-price="${price}">
        <div class="emi-grid">
          <label>Down payment<select name="down">${opts(EMI_DOWN, e.down, (x) => `${x}%`)}</select></label>
          <label>Months<select name="months">${opts(EMI_MONTHS, e.months, (x) => `${x} months`)}</select></label>
          <label>Interest per year<span class="pct"><input name="rate" type="number" inputmode="decimal" min="0" max="40" step="0.5" value="${e.rate}">%</span></label>
        </div>
        <div class="emi-type" role="radiogroup" aria-label="How interest is charged">
          <label><input type="radio" name="flat" value="0"${e.flat ? "" : " checked"}> On the remaining balance <span>(bank card EMI)</span></label>
          <label><input type="radio" name="flat" value="1"${e.flat ? " checked" : ""}> Flat on the whole loan <span>(hire purchase)</span></label>
        </div>
        <dl class="emi-out" id="emiOut">${emiOut(c)}</dl>
        <p class="emi-note">An estimate. Banks and shops set their own rates and may add a processing fee; 0% EMI offers usually need a 40–50% down payment. Ask the seller for the exact terms.</p>
      </div>
    </details>`;
  }
  const emiOut = (c) => `<div><dt>Pay now</dt><dd>${rs(c.downPay)}</dd></div><div><dt>Each month</dt><dd>${rs(c.monthly)}</dd></div>
    <div><dt>Interest</dt><dd>${rs(c.interest)}</dd></div><div><dt>Total cost</dt><dd>${rs(c.total)}</dd></div>`;
  function bindEmi() {
    const box = $("#emiBox");
    if (!box) return;
    const body = box.querySelector(".emi-body"), price = +body.dataset.price;
    box.addEventListener("toggle", () => { state.emi.open = box.open; });
    body.addEventListener("input", (ev) => {
      const t = ev.target;
      if (t.name === "flat") state.emi.flat = t.value === "1";
      else if (t.name) state.emi[t.name] = +t.value;
      const c = emiCalc(price, state.emi);
      $("#emiOut").innerHTML = emiOut(c);
      $("#emiSum").textContent = `${rs(c.monthly)}/month`;
    });
  }

  /** Which variants of a model pass the current filters, with their best offer. */
  function modelView(m) {
    const words = state.q.toLowerCase().replace(/iphone/g, "").replace(/(\d)\s*(gb|tb)/g, "$1$2").split(/\s+/).filter(Boolean);
    const rows = [];
    for (const v of m.variants) {
      if (state.storage.size && !state.storage.has(String(v.storage_gb))) continue;
      const hay = `${m.model} ${v.storage_gb}gb ${v.storage_gb} ${v.storage_gb >= 1024 ? v.storage_gb / 1024 + "tb" : ""} ${(v.colours || []).map((c) => c.colour).join(" ")}`.toLowerCase();
      if (!words.every((w) => hay.includes(w))) continue;
      const offers = offersOf(v);
      if (!offers.length) continue;
      const best = bestOf(offers);
      if (state.bands.size && !PRICE_BANDS.some(([id, , lo, hi]) => state.bands.has(id) && best.price >= lo && best.price < hi)) continue;
      rows.push({ v, offers, best, chg: weekChange(v, best.price) });
    }
    if (!rows.length) return null;
    const cheapest = rows.reduce((a, b) => (b.best.price < a.best.price ? b : a));
    const sellers = new Set(rows.flatMap((r) => r.offers.map((o) => o.store)));
    const drop = Math.min(0, ...rows.map((r) => r.chg ?? 0));
    return { m, rows, cheapest, sellers, drop };
  }

  // ================================================================ router
  window.addEventListener("hashchange", () => { if (!PAGE_SLUG) route(); });
  function route() {
    if (TEXT_PAGE) { markSeriesBar(); return; }
    const slug = PAGE_SLUG || location.hash.slice(1);
    const m = BY_SLUG[slug];
    $("#catalog").hidden = !!m;
    $("#product").hidden = !m;
    if (m) { renderProduct(m); if (!PAGE_SLUG) window.scrollTo(0, 0); }
    else {
      renderCatalog();
      const target = location.hash.slice(1) && document.getElementById(location.hash.slice(1));
      if (target) requestAnimationFrame(() => target.scrollIntoView());
    }
    markSeriesBar();
  }
  function goHome(scrollId) {
    if (location.hash) history.pushState(null, "", location.pathname + location.search);
    route();
    if (scrollId) requestAnimationFrame(() => document.getElementById(scrollId)?.scrollIntoView({ behavior: "smooth" }));
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest("[data-home]");
    if (!a) return;
    if ((PAGE_SLUG || TEXT_PAGE) && !INLINE) return;  // on a model or text page, follow the real link to the home page
    e.preventDefault();
    if (a.dataset.series !== undefined) { state.series = a.dataset.series ? new Set([a.dataset.series]) : new Set(); state.q = q.value = ""; }
    goHome(a.dataset.scroll);
    if (!a.dataset.scroll) window.scrollTo(0, 0);
  });

  // ================================================================ series bar + search
  function buildSeriesBar() {
    const fams = [...new Set(MODELS.map(family))].sort((a, b) => familyRank(a) - familyRank(b));
    $("#seriesBar").innerHTML = `<a href="${homeHref()}" data-home data-series="">All iPhones</a>` +
      fams.map((f) => `<a href="${esc(homeHref(f))}" data-home data-series="${esc(f)}">${esc(familyLabel(f))}</a>`).join("");
    $$("a[data-home]").forEach((a) => { if (a.closest(".top-nav, .logo-wrap") || a.classList.contains("logo")) a.setAttribute("href", homeHref("", a.dataset.scroll)); });
  }
  function markSeriesBar() {
    const onCatalog = !$("#catalog").hidden;
    $$("#seriesBar a").forEach((a) => {
      const f = a.dataset.series;
      const cur = onCatalog && (f ? state.series.size === 1 && state.series.has(f) : state.series.size === 0);
      a.setAttribute("aria-current", cur ? "true" : "false");
    });
  }

  const q = $("#q"), sug = $("#suggest");
  let sugItems = [], sugIdx = -1;
  q.addEventListener("input", () => {
    state.q = q.value;
    if (!$("#catalog").hidden) renderResults();
    showSuggestions();
  });
  q.addEventListener("focus", showSuggestions);
  q.addEventListener("blur", () => setTimeout(hideSuggestions, 150));
  q.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!sugItems.length) return;
      sugIdx = (sugIdx + (e.key === "ArrowDown" ? 1 : -1) + sugItems.length) % sugItems.length;
      $$("li", sug).forEach((li, i) => li.setAttribute("aria-selected", i === sugIdx));
    } else if (e.key === "Enter") {
      const it = sugItems[sugIdx >= 0 ? sugIdx : 0];
      if (it) { openVariant(it); hideSuggestions(); q.blur(); }
    } else if (e.key === "Escape") hideSuggestions();
  });
  sug.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li[data-i]");
    if (li) { e.preventDefault(); openVariant(sugItems[+li.dataset.i]); hideSuggestions(); q.blur(); }
  });
  function showSuggestions() {
    if (!DATA || !state.q.trim()) return hideSuggestions();
    const words = state.q.toLowerCase().replace(/iphone/g, "").replace(/(\d)\s*(gb|tb)/g, "$1$2").split(/\s+/).filter(Boolean);
    sugItems = [];
    for (const m of MODELS) for (const v of m.variants) {
      const hay = `${m.model} ${v.storage_gb}gb ${v.storage_gb >= 1024 ? v.storage_gb / 1024 + "tb" : ""} ${(v.colours || []).map((c) => c.colour).join(" ")}`.toLowerCase();
      if (!words.every((w) => hay.includes(w))) continue;
      // "17 pro max cosmic orange": say where that colour is in stock
      const colour = (v.colours || []).find((c) => words.some((w) => w.length > 2 && !m.model.toLowerCase().includes(w) && c.colour.toLowerCase().includes(w)));
      sugItems.push({ m, v, best: bestOf(v.offers), colour });
    }
    sugItems = sugItems.slice(0, 8);
    sugIdx = -1;
    sug.innerHTML = sugItems.length
      ? sugItems.map((it, i) => `<li role="option" data-i="${i}" aria-selected="false"><span class="s-img">${productImage(it.m)}</span><span class="s-name">${esc(it.m.model)} ${gbLabel(it.v.storage_gb)}${it.colour ? `<small>${esc(it.colour.colour)}: ${(() => { const n = it.colour.stores.filter((x) => x.in_stock === true).length; return n ? `in stock at ${n} ${n === 1 ? "shop" : "shops"}` : "not in stock right now"; })()}</small>` : ""}</span><span class="s-price">${rs(it.best.price)}</span></li>`).join("")
      : `<li class="s-empty">No iPhone matches “${esc(state.q)}”</li>`;
    sug.hidden = false;
    q.setAttribute("aria-expanded", "true");
  }
  function hideSuggestions() { sug.hidden = true; q.setAttribute("aria-expanded", "false"); }
  function openVariant(it) {
    state.pick[it.m.slug] = it.v.key; state.q = q.value = "";
    if (INLINE) location.hash = it.m.slug; else location.href = modelHref(it.m.slug, it.v.storage_gb);
  }

  // ================================================================ catalog
  function renderCatalog() {
    const live = DATA.stores.filter((s) => s.offers > 0 && s.kind !== "marketplace");  // marketplaces aren't sellers
    const fams = [...new Set(MODELS.map(family))].sort((a, b) => familyRank(a) - familyRank(b));
    const sizes = [...new Set(DATA.variants.map((v) => v.storage_gb).filter(Boolean))].sort((a, b) => a - b);
    const optList = (name, items) => items.map(([val, label, count]) =>
      `<label class="f-opt"><input type="checkbox" name="${name}" value="${esc(val)}"${state[name].has(val) ? " checked" : ""}> ${esc(label)}${count != null ? `<span class="count">${count}</span>` : ""}</label>`).join("");

    $("#catalog").innerHTML = `${flashFromLink()}
      <ol class="crumbs"><li><a href="${homeHref()}" data-home data-series="">Home</a></li><li>Mobile phones</li><li>Apple iPhone</li></ol>
      <div class="page-head">
        <h1>Apple iPhone prices in Nepal</h1>
        <p>Comparing ${MODELS.length} iPhone models at ${live.length} sellers. Updated ${fmtStamp(DATA.generated_at)}.</p>
      </div>
      <div class="cat-layout">
        <aside class="filters panel" id="filters" aria-label="Filters">
          <div class="filters-head"><h2>Filters</h2><button class="btn small" type="button" data-close-filters>Done</button></div>
          <div class="f-group"><h3>Series</h3>${optList("series", fams.map((f) => [f, familyLabel(f), MODELS.filter((m) => family(m) === f).length]))}</div>
          <div class="f-group"><h3>Price</h3>${optList("bands", PRICE_BANDS.map(([id, label]) => [id, label]))}</div>
          <div class="f-group"><h3>Storage</h3>${optList("storage", sizes.map((g) => [String(g), gbLabel(g), DATA.variants.filter((v) => v.storage_gb === g).length]))}</div>
          <div class="f-group"><h3>Sellers</h3>${optList("sellers", live.map((s) => [s.id, s.name, s.offers]))}</div>
          <div class="f-group">
            <label class="switch">Apple authorized sellers only<input type="checkbox" name="authOnly"${state.authOnly ? " checked" : ""}></label>
            <label class="switch">In stock only<input type="checkbox" name="stockOnly"${state.stockOnly ? " checked" : ""}></label>
          </div>
        </aside>
        <section aria-label="iPhone models">
          <div class="toolbar">
            <button class="btn small open-filters" type="button" data-open-filters>Filters</button>
            <span class="count-label" id="countLabel"></span>
            <div class="applied" id="applied"></div>
            <label class="sort">Sort by
              <select id="sortSel">
                <option value="newest">Newest</option>
                <option value="price-asc">Lowest price</option>
                <option value="price-desc">Highest price</option>
                <option value="drop">Biggest price drop</option>
              </select>
            </label>
          </div>
          <div class="grid" id="grid"></div>
        </section>
      </div>

      <section class="section" id="drops">
        <div class="section-head"><h2>Biggest price drops this week</h2></div>
        <div class="drops-row" id="dropsRow"></div>
      </section>

      <section class="section" id="price-list">
        <div class="section-head"><h2>iPhone price list in Nepal, ${esc(monthYear(DATA.generated_at))}</h2></div>
        <div class="panel" id="priceList"></div>
      </section>

      <section class="section" id="sellers">
        <div class="section-head"><h2>Sellers we compare</h2><p>Prices are read from each seller's website every 6 hours.</p></div>
        <div class="sellers-grid" id="sellersGrid"></div>
      </section>

      <section class="section" id="faq">
        <div class="section-head"><h2>Buying an iPhone in Nepal</h2></div>
        <div class="panel faq">
          <details><summary>Which sellers are Apple authorized?</summary><p>Sellers marked “Apple authorized” sell iPhones imported through Apple's official distributor in Nepal, with a local warranty. Other sellers may sell officially imported phones too, so ask about warranty before you pay.</p></details>
          <details><summary>How do I check that an iPhone is official?</summary><p>Enter the phone's IMEI on the distributor's website, or check it in the store before paying. The IMEI is printed on the box and shown in Settings, General, About.</p></details>
          <details><summary>How often are prices updated?</summary><p>Every 6 hours. Each price shows when it was last checked. Stores can change prices at any time, so confirm the final price with the seller.</p></details>
          <details><summary>Why is the store price different from the one shown here?</summary><p>Bhaau shows the price listed on the seller's website. In-store discounts, EMI offers, exchange deals and bank cashback are not included.</p></details>
        </div>
      </section>`;

    $("#sortSel").value = state.sort;
    renderResults();
    renderDrops();
    renderPriceList();
    renderSellers();
  }

  // filter + sort events (delegated, since the catalog re-renders)
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (!t.closest("#filters") && t.id !== "sortSel") return;
    if (t.id === "sortSel") state.sort = t.value;
    else if (t.name === "authOnly" || t.name === "stockOnly") state[t.name] = t.checked;
    else if (state[t.name] instanceof Set) t.checked ? state[t.name].add(t.value) : state[t.name].delete(t.value);
    renderResults();
    markSeriesBar();
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-open-filters]")) document.body.classList.add("filters-open");
    if (e.target.closest("[data-close-filters]")) document.body.classList.remove("filters-open");
    const chip = e.target.closest("[data-unset]");
    if (chip) {
      const [k, v] = chip.dataset.unset.split(":");
      if (k === "all") { ["series", "storage", "bands", "sellers"].forEach((s) => state[s].clear()); state.authOnly = state.stockOnly = false; state.q = q.value = ""; }
      else if (k === "q") { state.q = q.value = ""; }
      else if (state[k] instanceof Set) state[k].delete(v); else state[k] = false;
      renderCatalog(); markSeriesBar();
    }
    if (e.target.closest("[data-more]")) { $("#priceList").dataset.all = "1"; renderPriceList(); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.body.classList.remove("filters-open"); });

  function renderResults() {
    if (!$("#grid")) return;
    let views = MODELS.filter((m) => !state.series.size || state.series.has(family(m))).map(modelView).filter(Boolean);
    if (state.sort === "price-asc") views.sort((a, b) => a.cheapest.best.price - b.cheapest.best.price);
    if (state.sort === "price-desc") views.sort((a, b) => b.cheapest.best.price - a.cheapest.best.price);
    if (state.sort === "drop") views.sort((a, b) => a.drop - b.drop);
    $("#countLabel").textContent = `${views.length} ${views.length === 1 ? "model" : "models"}`;

    const chips = [];
    if (state.q) chips.push([`q:`, `“${state.q}”`]);
    state.series.forEach((f) => chips.push([`series:${f}`, familyLabel(f)]));
    state.bands.forEach((b) => chips.push([`bands:${b}`, PRICE_BANDS.find((x) => x[0] === b)[1]]));
    state.storage.forEach((g) => chips.push([`storage:${g}`, gbLabel(+g)]));
    state.sellers.forEach((s) => chips.push([`sellers:${s}`, SELLERS[s]?.name || s]));
    if (state.authOnly) chips.push(["authOnly", "Authorized only"]);
    if (state.stockOnly) chips.push(["stockOnly", "In stock"]);
    $("#applied").innerHTML = chips.map(([k, l]) => `<button class="chip" type="button" data-unset="${esc(k)}" aria-label="Remove filter ${esc(l)}">${esc(l)} ${ICON_X}</button>`).join("") +
      (chips.length > 1 ? `<button class="chip clear" type="button" data-unset="all">Clear all</button>` : "");

    if (!views.length) {
      $("#grid").innerHTML = `<div class="panel empty-state" style="grid-column:1/-1"><h3>No iPhones match these filters</h3><p>Remove a filter to see more models.</p><button class="btn" type="button" data-unset="all">Clear all filters</button></div>`;
      return;
    }
    const newest = newestFamily();
    $("#grid").innerHTML = views.map(({ m, rows, cheapest, sellers, drop }) => {
      const badges = [];
      if (family(m) === newest) badges.push(`<span class="badge new">New</span>`);
      if (drop < 0) badges.push(`<span class="badge drop">Price dropped</span>`);
      const sizes = rows.map((r) => gbLabel(r.v.storage_gb)).filter(Boolean).join(", ");
      return `<a class="card panel" href="${modelHref(m.slug)}">
        <div class="badges">${badges.join("")}</div>
        <div class="img">${productImage(m)}</div>
        <h3>Apple ${esc(m.model)}</h3>
        <div class="sizes">${esc(sizes)}</div>
        <div class="price-block">
          <div class="from">${rows.length > 1 ? "From" : "Lowest price"}</div>
          <div class="price">${rs(cheapest.best.price)}</div>
          <div class="meta"><span>at <b>${esc(SELLERS[cheapest.best.store].name)}</b></span><span>${sellers.size} ${sellers.size === 1 ? "seller" : "sellers"}</span></div>
        </div>
      </a>`;
    }).join("");
  }

  function renderDrops() {
    const seen = new Set();
    const drops = (DATA.drops || []).filter((d) => SELLERS[d.store] && BY_SLUG[slugify(d.model)])
      .filter((d) => { const k = d.variant; if (seen.has(k)) return false; seen.add(k); return true; })
      .sort((a, b) => (b.from - b.to) - (a.from - a.to)).slice(0, 6);
    $("#dropsRow").innerHTML = drops.length ? drops.map((d) => `<a class="drop-card panel" href="${modelHref(slugify(d.model), d.storage_gb)}" data-pick="${esc(d.variant)}" data-slug="${slugify(d.model)}">
        <span class="img">${productImage(BY_SLUG[slugify(d.model)])}</span>
        <span class="txt"><span class="name">${esc(d.model)} ${gbLabel(d.storage_gb)}</span><span class="where">${esc(SELLERS[d.store].name)}, ${fmtDay(d.at)}</span></span>
        <span class="p"><s>${rs(d.from)}</s><b>${rs(d.to)}</b></span></a>`).join("")
      : `<p class="note">No price drops in the last 14 days.</p>`;
    $$("#dropsRow [data-pick]").forEach((a) => a.addEventListener("click", () => { state.pick[a.dataset.slug] = a.dataset.pick; }));
  }

  function renderPriceList() {
    const box = $("#priceList");
    const all = box.dataset.all === "1";
    const rows = MODELS.map((m) => {
      const v = m.variants[0];
      const best = bestOf(v.offers);
      return { m, v, best };
    });
    const shown = all ? rows : rows.slice(0, 8);
    box.innerHTML = `<table class="price-list"><thead><tr><th>iPhone model</th><th>Available at</th><th class="r">Lowest price</th></tr></thead><tbody>` +
      shown.map(({ m, v, best }) => `<tr><td><a href="${modelHref(m.slug)}">Apple ${esc(m.model)}</a> <span style="color:var(--ink-3)">${gbLabel(v.storage_gb)}</span></td><td>${esc(SELLERS[best.store].name)}</td><td class="r">${rs(best.price)}</td></tr>`).join("") +
      `</tbody></table>` + (!all && rows.length > 8 ? `<div class="more-row"><button class="link-btn" type="button" data-more>Show all ${rows.length} models</button></div>` : "");
  }

  function renderSellers() {
    $("#sellersGrid").innerHTML = DATA.stores.filter((s) => s.enabled).map((s) => {
      const st = s.last_status === "ok" ? ["ok", `Checked ${s.last_success ? ago(s.last_success) : ""}`] : ["bad", "Prices not available right now"];
      return `<div class="seller-card panel">
        ${sellerMark(SELLERS[s.id])}
        <div class="txt">
          <div class="name"><a href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener">${esc(s.name)}</a>${s.authorized ? `<span class="badge auth">${ICON_CHECK}Apple authorized</span>` : ""}${s.kind === "marketplace" ? `<span class="badge new">Individual sellers</span>` : ""}</div>
          <div class="sub"><span class="dot ${st[0]}"></span>${esc(st[1])}${s.offers ? `, ${s.offers} ${s.kind === "marketplace" ? "brand-new listings" : "prices"}` : ""}</div>
          ${s.details ? `<details class="s-info"><summary>Warranty, EMI, delivery and shops</summary>${shopInfo(s)}</details>` : ""}
        </div></div>`;
    }).join("");
  }

  // ================================================================ product page
  function renderProduct(m) {
    const variants = m.variants.filter((v) => offersOf(v, { useSellerFilter: false, stock: false }).length);
    if (!variants.length) { $("#product").innerHTML = `<div class="panel empty-state" style="margin-top:24px"><h3>No prices for ${esc(m.model)}</h3><p>Turn off “Apple authorized sellers only” to see all sellers.</p></div>`; return; }
    const wanted = PAGE_SLUG && params.get("storage");
    const pickKey = state.pick[m.slug] || (wanted && (m.variants.find((x) => String(x.storage_gb) === wanted) || {}).key);
    const v = variants.find((x) => x.key === pickKey) || variants[0];
    state.pick[m.slug] = v.key;
    const all = offersOf(v, { useSellerFilter: false, stock: false });
    const offers = state.pStock ? all.filter((o) => o.in_stock !== false) : all;
    const best = bestOf(all);
    const s = SELLERS[best.store];
    const chg = weekChange(v, best.price);
    const prices = all.map((o) => o.price);
    const others = MODELS.filter((x) => x !== m && family(x) === family(m)).concat(MODELS.filter((x) => family(x) !== family(m))).slice(0, 4);
    document.title = `Apple ${m.model} ${gbLabel(v.storage_gb)} price in Nepal | Bhaau`;

    $("#product").innerHTML = `${flashFromLink()}
      <ol class="crumbs"><li><a href="${homeHref()}" data-home data-series="">Home</a></li>${familyLabel(family(m)) !== m.model ? `<li><a href="${esc(homeHref(family(m)))}" data-home data-series="${esc(family(m))}">${esc(familyLabel(family(m)))}</a></li>` : ""}<li>${esc(m.model)}</li></ol>
      <div class="prod-top">
        <div class="gallery panel">${productImage(m)}${m.image_credit ? `<p class="img-credit">${esc(m.image_credit)}</p>` : ""}</div>
        <div class="prod-info">
          <div>
            <h1>Apple ${esc(m.model)} ${gbLabel(v.storage_gb)}</h1>
            <p class="sub">Sold by ${all.length} ${all.length === 1 ? "seller" : "sellers"} in Nepal. Prices checked ${ago(best.last_seen)}.</p>
          </div>
          ${variants.length > 1 || v.storage_gb ? `<div><div class="label">Storage</div><div class="storage-opts" role="group" aria-label="Storage">${variants.map((x) => {
            const b = bestOf(offersOf(x, { useSellerFilter: false, stock: false }));
            return `<button type="button" data-variant="${esc(x.key)}" aria-pressed="${x === v}"><b>${gbLabel(x.storage_gb) || "Size not listed"}</b><span>${rs(b.price)}</span></button>`;
          }).join("")}</div></div>` : ""}
          <div class="best-box panel">
            <div class="lbl">Lowest price</div>
            <div class="big">${rs(best.price)}</div>
            <div class="at">at <b>${esc(s.name)}</b>${s.authorized ? `<span class="badge auth">${ICON_CHECK}Apple authorized</span>` : ""}${best.in_stock === false ? `<span class="badge rise">Out of stock</span>` : ""}</div>
            <a class="btn go block" href="${esc(safeUrl(best.url))}" target="_blank" rel="noopener nofollow">Go to ${esc(s.name)}</a>
            <div class="facts">
              ${all.length > 1 ? (Math.min(...prices) === Math.max(...prices) ? `<span>Same price at all ${all.length} sellers</span>` : `<span>${all.length} sellers from ${rs(Math.min(...prices))} to ${rs(Math.max(...prices))}</span>`) : ""}
              ${chg ? `<span class="${chg < 0 ? "down" : "up"}">${rs(Math.abs(chg))} ${chg < 0 ? "lower" : "higher"} than last week</span>` : chg === 0 ? `<span>Same price as last week</span>` : ""}
            </div>
            ${(() => { const vd = priceVerdict(v, best.in_stock === false ? null : best.price);
              return vd ? `<div class="verdict ${vd.tone}"><b>${esc(vd.head)}</b> ${esc(vd.text)}</div>` : ""; })()}
          </div>
          ${emiBox(best.price)}
          ${DATA.alerts_url && v.storage_gb ? alertForm(m, v) : ""}
        </div>
      </div>

      ${waysBox(m, v, all.find((o) => o.in_stock !== false) || null)}

      <nav class="tabs" aria-label="On this page">
        <a href="#" data-to="p-prices" class="on">Prices (${all.length})</a>
        <a href="#" data-to="p-history">Price history</a>
        <a href="#" data-to="p-similar">Other iPhones</a>
      </nav>

      <section class="section" id="p-prices" style="margin-top:20px">
        <div class="panel offers">
          <div class="offers-head"><h2 style="font-size:17px">Prices for ${esc(m.model)} ${gbLabel(v.storage_gb)}</h2>
            <label class="switch">In stock only <input type="checkbox" id="pStock"${state.pStock ? " checked" : ""}></label></div>
          ${offers.length ? offers.map((o) => offerRow(o, o === best, v)).join("") : `<div class="empty-state">No seller has this in stock right now.</div>`}
          <p class="offers-note">Prices are checked every 6 hours and can change. Confirm with the seller before you pay.${DATA.contact_email ? ` <a href="${esc(reportHref(m, v))}">Report a wrong price</a>` : ""}</p>
        </div>
        ${coloursBox(m, v, all)}
        ${marketBox(m, v, best)}
      </section>

      <section class="section" id="p-history">
        <div class="section-head"><h2>Price history</h2></div>
        <div class="panel history" id="historyBox"></div>
      </section>

      <section class="section" id="p-similar">
        <div class="section-head"><h2>Other iPhones</h2></div>
        <div class="grid">${others.map((x) => { const mv = modelView(x) || { cheapest: { best: bestOf(x.variants[0].offers) }, sellers: new Set(x.variants[0].offers.map((o) => o.store)) };
          return `<a class="card panel" href="${modelHref(x.slug)}"><div class="img">${productImage(x)}</div><h3>Apple ${esc(x.model)}</h3><div class="price-block"><div class="from">From</div><div class="price">${rs(mv.cheapest.best.price)}</div><div class="meta"><span>at <b>${esc(SELLERS[mv.cheapest.best.store].name)}</b></span><span>${mv.sellers.size} ${mv.sellers.size === 1 ? "seller" : "sellers"}</span></div></div></a>`; }).join("")}</div>
      </section>`;

    renderHistory(v);
    bindAlertForm(v);
    $$("#product [data-variant]").forEach((b) => b.addEventListener("click", () => { state.pick[m.slug] = b.dataset.variant; renderProduct(m); }));
    $("#pStock").addEventListener("change", (e) => { state.pStock = e.target.checked; renderProduct(m); });
    bindEmi();
    $$("#product .tabs a").forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault();
      $$("#product .tabs a").forEach((x) => x.classList.toggle("on", x === a));
      document.getElementById(a.dataset.to).scrollIntoView({ behavior: "smooth", block: "start" });
    }));
    $$("#product .ways [data-to]").forEach((a) => a.addEventListener("click", (e) => {  // jump without touching the URL
      const el = document.getElementById(a.dataset.to);
      if (el) { e.preventDefault(); el.scrollIntoView({ behavior: "smooth", block: "start" }); }
    }));
  }

  // ================================================================ price-drop alerts
  const ICON_BELL = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>`;
  function alertForm(m, v) {
    return `<form class="alert-box panel" id="alertForm" novalidate>
      <div class="alert-head">${ICON_BELL}<div><b>Email me when the price drops</b>
        <span>For Apple ${esc(m.model)} ${gbLabel(v.storage_gb)}. One email per drop, stop any time.</span></div></div>
      <div class="alert-fields">
        <label><span class="sr">Your email</span><input id="alertEmail" type="email" placeholder="Your email" autocomplete="email" required></label>
        <label><span class="sr">Only below this price (optional)</span><input id="alertTarget" inputmode="numeric" placeholder="Any drop, or below Rs…"></label>
        <input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">
        <button class="btn alert-btn" type="submit">Create alert</button>
      </div>
      <p class="alert-fine">We use your email only for this alert. <a href="${ROOT}privacy/">Privacy</a></p>
      <p class="alert-msg" id="alertMsg" role="status" hidden></p>
    </form>`;
  }
  function reportHref(m, v) {
    const page = location.origin && location.origin !== "null" ? location.origin + location.pathname : "";
    const subject = `Wrong price: ${m.model} ${gbLabel(v.storage_gb)}`.trim();
    const body = `Page: ${page}\n\nWhich seller, and what price do they show?\n`;
    return `mailto:${DATA.contact_email}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  }
  function bindAlertForm(v) {
    const form = $("#alertForm");
    if (!form) return;
    const msg = $("#alertMsg"), btn = $(".alert-btn", form);
    const say = (text, ok) => { msg.textContent = text; msg.className = `alert-msg ${ok ? "ok" : "err"}`; msg.hidden = false; };
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const email = $("#alertEmail").value.trim();
      const target = $("#alertTarget").value.replace(/[^\d]/g, "");
      if (!/^\S+@\S+\.\S+$/.test(email)) { say("Enter your email address.", false); $("#alertEmail").focus(); return; }
      if (DATA.demo) { say("This is a preview with sample prices, so alerts aren't connected yet.", false); return; }
      btn.disabled = true; btn.textContent = "Creating…";
      try {
        const r = await fetch(`${DATA.alerts_url}/subscribe`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, variant: v.key, target: target || null, website: form.website.value }) });
        const j = await r.json().catch(() => ({}));
        if (r.ok && j.ok) { say(j.message || "Check your inbox and click the link to turn on the alert.", true); form.reset(); }
        else say(j.error || "Couldn't create the alert. Please try again.", false);
      } catch {
        say("Couldn't reach the alert service. Check your connection and try again.", false);
      } finally {
        btn.disabled = false; btn.textContent = "Create alert";
      }
    });
  }
  function flashFromLink() {  // messages for people arriving from an alert email link
    const kind = params.get("alert");
    if (!kind) return "";
    const text = { confirmed: "Your price alert is on. We'll email you when the price drops.",
                   invalid: "That alert link has expired or was already used." }[kind];
    params.delete("alert");
    history.replaceState(null, "", location.pathname + (params.toString() ? `?${params}` : "") + location.hash);
    return text ? `<p class="flash ${kind === "confirmed" ? "ok" : "err"}" role="status">${esc(text)}</p>` : "";
  }

  // ---- shop details (from stores.yaml `details:`): only what the site owner entered
  const telHref = (p) => "tel:" + String(p).split(/[,/]/)[0].replace(/[^0-9+]/g, "");
  function shopInfo(s) {
    const d = s && s.details;
    if (!d) return "";
    const rows = [["warranty", "Warranty"], ["emi", "EMI"], ["delivery", "Delivery"], ["returns", "Returns"],
      ["hours", "Opening hours"], ["note", "Good to know"]].filter(([k]) => d[k])
      .map(([k, l]) => `<div><dt>${l}</dt><dd>${esc(d[k])}</dd></div>`);
    if (d.phone) rows.push(`<div><dt>Phone</dt><dd><a href="${esc(telHref(d.phone))}">${esc(d.phone)}</a></dd></div>`);
    const li = (l) => `<li><b>${esc(l.name)}</b>${l.address ? `<span>${esc(l.address)}</span>` : ""}${
      l.phone ? `<a href="${esc(telHref(l.phone))}">${esc(l.phone)}</a>` : ""}${l.map ? `<a href="${esc(safeUrl(l.map))}" target="_blank" rel="noopener">Map</a>` : ""}</li>`;
    // branches grouped by city, in the order they're listed in stores.yaml
    const byCity = new Map();
    (d.locations || []).forEach((l) => { const c = l.city || ""; byCity.set(c, [...(byCity.get(c) || []), l]); });
    const n = (d.locations || []).length;
    const locs = [...byCity].map(([city, ls]) => `${city ? `<div class="city">${esc(city)}</div>` : ""}<ul>${ls.map(li).join("")}</ul>`).join("");
    return `<dl class="shop-info">${rows.join("")}</dl>${n ? `<div class="shop-locs"><div class="lbl">${n === 1 ? "Shop" : `${n} branches`}${byCity.size > 1 ? ` in ${byCity.size} cities` : ""}</div>${locs}</div>` : ""}`;
  }

  // ---- colours: which shop has which colour of this exact phone
  const SWATCH = { "Cosmic Orange": "#e8762d", "Deep Blue": "#2b3a55", "Mist Blue": "#a9bdd6", "Sky Blue": "#bcd4e6",
    "Light Gold": "#e9dcc0", "Cloud White": "#f2f2ef", "Space Black": "#2b2b2d", Sage: "#b6c3a6", Lavender: "#c9b8dc",
    Burgundy: "#6d1f2c", "Desert Titanium": "#bfa48a", "Natural Titanium": "#bab3a8", "White Titanium": "#e8e6e1",
    "Black Titanium": "#3a3a3c", "Blue Titanium": "#3f4a5a", Ultramarine: "#4a5fd4", Teal: "#8fc6c0", "Deep Purple": "#4f4256",
    "Sierra Blue": "#9bb5ce", "Alpine Green": "#505f4e", "Pacific Blue": "#2f4d5f", Graphite: "#4a4a4c", Midnight: "#222a33",
    Starlight: "#f0e9dd", Red: "#c62a2f", Black: "#1f1f21", White: "#f5f5f3", Silver: "#e3e4e5", Gold: "#f2dfc2",
    Pink: "#f5c6cf", Blue: "#5f86b5", Green: "#9cc3a1", Yellow: "#f4e48c", Purple: "#c8b6dc", Orange: "#f08a3c" };
  function coloursBox(m, v, offers) {
    const cols = v.colours || [];
    if (!cols.length) return "";
    const listed = new Set(cols.flatMap((c) => c.stores.map((x) => x.store)));
    const silent = [...new Set(offers.map((o) => o.store))].filter((id) => !listed.has(id) && SELLERS[id]).map((id) => SELLERS[id].name);
    const rows = cols.map((c) => `<div class="c-row">
        <div class="c-name"><span class="swatch" style="background:${SWATCH[c.colour] || "#999"}"></span>${esc(c.colour)}</div>
        <div class="c-shops">${c.stores.filter((x) => SELLERS[x.store]).map((x) => {
          const st = x.in_stock === true ? ["in", "In stock"] : x.in_stock === false ? ["out", "Out of stock"] : ["", "Stock not shown"];
          return `<a class="c-chip ${st[0]}" href="${esc(safeUrl(x.url))}" target="_blank" rel="noopener nofollow" title="${esc(st[1])}">${esc(SELLERS[x.store].name)}<span>${st[1]}</span></a>`;
        }).join("")}</div></div>`).join("");
    return `<div class="panel colours" id="p-colours">
      <div class="offers-head"><h2 style="font-size:17px">Colours of the ${esc(m.model)} ${gbLabel(v.storage_gb)}</h2></div>
      ${rows}
      ${silent.length ? `<p class="offers-note">${esc(silent.length > 1 ? silent.slice(0, -1).join(", ") + " and " + silent[silent.length - 1] : silent[0])} ${silent.length === 1 ? "doesn't" : "don't"} show colours on ${silent.length === 1 ? "its website" : "their websites"}. Ask them directly.</p>` : ""}
    </div>`;
  }

  // ---- three ways to buy: shop (VAT bill) vs no-VAT ad vs used
  function waysBox(m, v, best) {
    const nov = (v.market || [])[0], u = v.used;
    if (!nov && !u) return "";
    const mk = (s) => (s && SELLERS[s] ? SELLERS[s] : null);
    const market = mk((nov || u || {}).store);
    const searchUrl = market && market.search_url ? market.search_url.replace("{query}", encodeURIComponent(m.model.toLowerCase())) : (market ? market.url : "#");
    const less = (p) => (best && best.price > p ? `<div class="w-diff">${rs(best.price - p)} less than in shops</div>` : "");
    return `<section class="ways" aria-label="Ways to buy">
      <h2>Three ways to buy the ${esc(m.model)} ${gbLabel(v.storage_gb)}</h2>
      <div class="ways-grid">
        <div class="way panel official">
          <div class="w-lbl">From a shop</div>
          <div class="w-price">${best ? rs(best.price) : "Not in stock"}</div>
          <ul><li>VAT bill</li><li>Warranty from the shop; Apple authorized sellers give the official warranty</li></ul>
          <a href="#p-prices" data-to="p-prices">Compare shops</a>
        </div>
        <div class="way panel novat">
          <div class="w-lbl">New, without VAT bill</div>
          ${nov ? `<div class="w-price">${rs(nov.price)}</div>${less(nov.price)}` : `<div class="w-price none">No ads right now</div>`}
          <ul><li>Individual sellers${market ? ` on ${esc(market.name)}` : ""}</li><li>Usually no official warranty</li><li>Check the IMEI is registered with NTA</li></ul>
          ${nov ? `<a href="#p-market" data-to="p-market">See the ads and the warning</a>` : ""}
        </div>
        <div class="way panel used">
          <div class="w-lbl">Used</div>
          ${u ? `<div class="w-price">${rs(u.median)}</div><div class="w-range">Most ads ${rs(u.low)} to ${rs(u.high)}, from ${u.ads} ads${u.all_sizes ? ", all storage sizes" : ""}</div>`
              : `<div class="w-price none">Not enough ads yet</div>`}
          <ul><li>Check battery health, and that iCloud is signed out</li><li>Ask for the original bill if there is one</li></ul>
          ${u ? `<a href="${esc(safeUrl(searchUrl))}" target="_blank" rel="noopener nofollow">See used ads${market ? ` on ${esc(market.name)}` : ""}</a>` : ""}
        </div>
      </div>
    </section>`;
  }

  // ---- marketplaces (HamroBazar): individual sellers, kept apart from shops, with a warning
  function marketBox(m, v, best) {
    const listings = v.market || [];
    if (!listings.length) return "";
    const byStore = {};
    listings.forEach((o) => (byStore[o.store] = byStore[o.store] || []).push(o));
    return Object.entries(byStore).filter(([id]) => SELLERS[id]).map(([id, items]) => {
      const s = SELLERS[id];
      const searchUrl = s.search_url ? s.search_url.replace("{query}", encodeURIComponent(m.model.toLowerCase())) : s.url;
      const rows = items.slice(0, 3).map((o) => {
        const below = best && best.price > o.price ? `${rs(best.price - o.price)} below the lowest shop price` : "";
        return `<div class="m-row">
          <div class="m-txt"><div class="m-title">${esc(o.title)}</div><div class="m-meta">Brand-new listing, seen ${ago(o.last_seen)}</div></div>
          <div class="m-price"><b>${rs(o.price)}</b>${below ? `<span>${below}</span>` : ""}</div>
          <a class="btn small" href="${esc(safeUrl(o.url))}" target="_blank" rel="noopener nofollow">View ad</a>
        </div>`;
      }).join("");
      return `<div class="panel market" id="p-market">
        <div class="market-head"><h2>On ${esc(s.name)}</h2><span class="badge new">Individual sellers</span></div>
        <div class="market-warn" role="note">
          <p><b>Often cheaper because it's sold without a VAT bill.</b> These are ads by individuals, not shops. Without a bill there's usually no official warranty, and the phone may be an unofficial import: if its IMEI isn't registered with NTA, it can be blocked from Nepali mobile networks.</p>
          <ul><li>Ask for the bill and warranty, and check the IMEI is registered with NTA before you pay.</li>
          <li>Make sure the box is sealed and the phone is genuine, not a copy.</li>
          <li>Meet in a busy public place, and don't send money in advance.</li></ul>
          <p class="m-fine">Bhaau shows only ads marked brand new and priced close to shop prices, but doesn't check them.</p>
        </div>
        ${rows}
        <a class="m-all" href="${esc(safeUrl(searchUrl))}" target="_blank" rel="noopener nofollow">See all ${esc(m.model)} ads on ${esc(s.name)}</a>
      </div>`;
    }).join("");
  }

  function offerRow(o, isBest, v) {
    const s = SELLERS[o.store];
    const oos = o.in_stock === false;
    return `<div class="offer${isBest ? " best" : ""}">
      ${sellerMark(s)}
      <div class="who"><div class="name">${esc(s.name)}${s.authorized ? `<span class="badge auth">${ICON_CHECK}Apple authorized</span>` : ""}</div>
        <div class="sub">Checked ${ago(o.last_seen)}${o.storage_assumed ? `. Size not stated by the seller; the price matches the ${gbLabel(v.storage_gb)} price elsewhere` : ""}</div></div>
      <div class="stock ${oos ? "out" : o.in_stock ? "in" : ""}">${oos ? "Out of stock" : o.in_stock ? "In stock" : "Stock not shown"}</div>
      <div class="amt">${isBest ? `<span class="lowest-tag">Lowest price</span>` : ""}${o.old_price > o.price ? `<s>${rs(o.old_price)}</s>` : ""}<b>${rs(o.price)}</b></div>
      <div class="act"><a class="btn ${isBest ? "go" : ""} small" href="${esc(safeUrl(o.url))}" target="_blank" rel="noopener nofollow">Go to store</a></div>
    </div>${s.details ? `<details class="o-info"><summary>Warranty, EMI, delivery and shop details for ${esc(s.name)}</summary>${shopInfo(s)}</details>` : ""}`;
  }

  // ================================================================ price history
  function renderHistory(v) {
    const box = $("#historyBox");
    const h = HISTORY[v.key] || {};
    const days = { "1m": 30, "3m": 90, all: Infinity }[state.range];
    const cutoff = days === Infinity ? "" : new Date(Date.now() - days * 864e5).toISOString().slice(0, 10);
    const low = lowestSeries(v.key).filter(([d]) => d >= cutoff);

    const controls = `<div class="hist-controls">
      <div class="seg" role="group" aria-label="Time range">${[["1m", "1 month"], ["3m", "3 months"], ["all", "All"]].map(([k, l]) => `<button type="button" data-range="${k}" aria-pressed="${state.range === k}">${l}</button>`).join("")}</div>
      <div class="seg" role="group" aria-label="Show">${[["lowest", "Lowest price"], ["sellers", "Each seller"]].map(([k, l]) => `<button type="button" data-mode="${k}" aria-pressed="${state.mode === k}">${l}</button>`).join("")}</div>
    </div>`;

    if (low.length < 2) {
      box.innerHTML = controls + `<p class="note">The price history fills in after a few days of tracking.</p>`;
    } else {
      const ps = low.map(([, p]) => p);
      const lo = Math.min(...ps), hi = Math.max(...ps), avg = Math.round(ps.reduce((a, b) => a + b, 0) / ps.length);
      const delta = (ps[ps.length - 1] - ps[0]) / ps[0];
      const trend = delta < -0.01 ? ["down", "Falling"] : delta > 0.01 ? ["up", "Rising"] : ["", "Stable"];
      const stats = `<dl class="stats">
        <div><dt>Lowest</dt><dd>${rs(lo)}</dd></div><div><dt>Highest</dt><dd>${rs(hi)}</dd></div>
        <div><dt>Average</dt><dd>${rs(avg)}</dd></div><div><dt>Trend</dt><dd class="${trend[0]}">${trend[1]}</dd></div></dl>`;
      const series = state.mode === "lowest"
        ? [{ name: "Lowest price", color: "var(--brand-line)", pts: low, area: true }]
        : Object.entries(h).filter(([sid]) => SELLERS[sid] && (!state.authOnly || SELLERS[sid].authorized))
            .map(([sid, pts]) => ({ name: SELLERS[sid].name, color: SELLERS[sid].color, pts: pts.filter(([d]) => d >= cutoff) }))
            .filter((x) => x.pts.length);
      box.innerHTML = controls + stats + `<div class="chart" id="chartBox"></div>` +
        (state.mode === "sellers" ? `<div class="legend">${series.map((x) => `<span><i style="background:${x.color}"></i>${esc(x.name)}</span>`).join("")}</div>` : "") +
        `<p class="note">${state.mode === "lowest" ? "The lowest price across all sellers on each day." : "Each seller's listed price on each day."}</p>`;
      drawChart($("#chartBox"), series);
    }
    $$("[data-range]", box).forEach((b) => b.addEventListener("click", () => { state.range = b.dataset.range; renderHistory(v); }));
    $$("[data-mode]", box).forEach((b) => b.addEventListener("click", () => { state.mode = b.dataset.mode; renderHistory(v); }));
  }

  function drawChart(box, series) {
    const days = [...new Set(series.flatMap((s) => s.pts.map(([d]) => d)))].sort();
    const W = 900, H = 280, L = 60, R = 12, T = 12, B = 28;
    const all = series.flatMap((s) => s.pts.map(([, p]) => p));
    let lo = Math.min(...all), hi = Math.max(...all);
    const pad = Math.max((hi - lo) * 0.15, 2000); lo -= pad; hi += pad;
    const t0 = Date.parse(days[0]), t1 = Date.parse(days[days.length - 1]);
    const x = (d) => L + ((Date.parse(d) - t0) / (t1 - t0 || 1)) * (W - L - R);
    const y = (p) => T + (1 - (p - lo) / (hi - lo)) * (H - T - B);
    const grid = [];
    for (let i = 0; i <= 4; i++) {
      const p = lo + ((hi - lo) * i) / 4;
      grid.push(`<line class="gl" x1="${L}" x2="${W - R}" y1="${y(p)}" y2="${y(p)}"/><text x="${L - 8}" y="${y(p) + 4}" text-anchor="end">${num(Math.round(p / 100) * 100)}</text>`);
    }
    const ticks = days.length > 2 ? [0, Math.floor((days.length - 1) / 2), days.length - 1] : [0, days.length - 1];
    ticks.forEach((i, k) => grid.push(`<text x="${x(days[i])}" y="${H - 8}" text-anchor="${k === 0 ? "start" : k === ticks.length - 1 ? "end" : "middle"}">${fmtDay(days[i])}</text>`));
    const brand = getComputedStyle(document.documentElement).getPropertyValue("--link").trim() || "#0b4fc4";
    const paths = series.map((s) => {
      const col = s.color.startsWith("var") ? brand : s.color;
      let d = "";
      s.pts.forEach(([day, p], i) => { d += i ? `H${x(day).toFixed(1)}V${y(p).toFixed(1)}` : `M${x(day).toFixed(1)} ${y(p).toFixed(1)}`; });
      const area = s.area ? `<path d="${d}V${H - B}H${x(s.pts[0][0]).toFixed(1)}Z" fill="${col}" opacity=".10"/>` : "";
      const [ld, lp] = s.pts[s.pts.length - 1];
      return `${area}<path d="${d}" fill="none" stroke="${col}" stroke-width="2.2" stroke-linejoin="round"/><circle cx="${x(ld)}" cy="${y(lp)}" r="3.5" fill="${col}"/>`;
    }).join("");
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Price history chart"><g class="ax">${grid.join("")}</g>${paths}
      <line class="cross" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="var(--ink-3)" stroke-dasharray="3 3" visibility="hidden"/>
      <rect class="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/></svg><div class="tip" hidden></div>`;
    const svg = $("svg", box), tip = $(".tip", box), cross = $(".cross", box);
    const at = (pts, d) => { let v = null; for (const [pd, p] of pts) { if (pd <= d) v = p; else break; } return v; };
    $(".hit", box).addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect();
      const px = ((e.clientX - r.left) / r.width) * W;
      const d = days.reduce((a, b) => (Math.abs(x(b) - px) < Math.abs(x(a) - px) ? b : a));
      cross.setAttribute("x1", x(d)); cross.setAttribute("x2", x(d)); cross.setAttribute("visibility", "visible");
      const vals = series.map((s) => [s.name, at(s.pts, d)]).filter(([, p]) => p).sort((a, b) => a[1] - b[1]);
      tip.innerHTML = `<strong>${fmtDay(d)}</strong>` + vals.map(([n, p]) => `<div><span>${esc(n)}</span><span>${rs(p)}</span></div>`).join("");
      tip.hidden = false;
      const left = (x(d) / W) * r.width;
      tip.style.left = (left > r.width * 0.6 ? left - tip.offsetWidth - 12 : left + 12) + "px";
    });
    $(".hit", box).addEventListener("pointerleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  }

  load();
})();
