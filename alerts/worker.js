/**
 * Bhaau price-drop alerts: a Cloudflare Worker with a D1 (SQLite) database.
 *
 *   POST /subscribe     {email, variant, target?}  -> sends a confirmation email (double opt-in)
 *   GET  /confirm?token=...                         -> turns the alert on, back to the phone's page
 *   GET  /unsubscribe?token=...[&all=1]             -> asks "stop these emails?" (POST does it; one-click supported)
 *   POST /notify        (Bearer ALERTS_TOKEN)       -> scraper pushes latest prices; emails price drops
 *   GET  /stats         (Bearer ALERTS_TOKEN)       -> counts, for you
 *
 * Subscriber emails live only in D1. They never touch GitHub or the website's files.
 *
 * Settings (wrangler.toml [vars] and secrets):
 *   SITE_URL        https://bhaau.com.np            (links in emails, redirects)
 *   SITE_ORIGINS    https://bhaau.com.np,http://localhost:8000   (who may call /subscribe)
 *   MAIL_FROM       Bhaau <alerts@bhaau.com.np>     (a domain verified in Resend)
 *   MAX_EMAILS_PER_RUN  90                          (Resend free plan: 100 emails/day)
 *   secret RESEND_API_KEY, secret ALERTS_TOKEN
 */

const EMAIL_RE = /^[^\s@<>"'(),;:\\]{1,64}@[^\s@<>"'(),;:\\]{1,190}\.[a-z]{2,24}$/i;
const VARIANT_RE = /^[a-z0-9-]{1,60}\|(\d{2,5}|na)$/;
const MAX_ALERTS_PER_EMAIL = 20;
const MAX_SIGNUPS_PER_IP_PER_HOUR = 10;
const RESEND_CONFIRM_AFTER_MIN = 10;

const nf = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const rs = (n) => `Rs ${nf.format(n)}`;
const now = () => new Date().toISOString();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const site = (env) => String(env.SITE_URL || "").replace(/\/+$/, "");
const self = (req) => new URL(req.url).origin;

export default {
  async fetch(req, env) {
    try {
      return await route(req, env);
    } catch (e) {
      console.error("alerts error:", e && e.stack || e);
      return json({ ok: false, error: "Something went wrong on our side. Please try again in a minute." }, 500, req, env);
    }
  },
};

async function route(req, env) {
  const url = new URL(req.url);
  if (req.method === "OPTIONS") return withCors(new Response(null, { status: 204 }), req, env);
  if (url.pathname === "/subscribe" && req.method === "POST") return subscribe(req, env);
  if (url.pathname === "/confirm" && req.method === "GET") return confirm(url, env);
  if (url.pathname === "/unsubscribe") return unsubscribe(req, url, env);
  if (url.pathname === "/notify" && req.method === "POST") return notify(req, env);
  if (url.pathname === "/stats" && req.method === "GET") return stats(req, env);
  return new Response("Not found", { status: 404 });
}

// ------------------------------------------------------------------ helpers
function allowedOrigin(req, env) {
  const origin = req.headers.get("Origin") || "";
  // Entries may be full addresses (https://you.github.io/bhaau): only the origin part counts.
  const list = String(env.SITE_ORIGINS || env.SITE_URL || "").split(",").map((s) => { try { return new URL(s.trim()).origin; } catch { return ""; } }).filter(Boolean);
  return list.includes(origin) ? origin : "";
}
function withCors(res, req, env) {
  const origin = allowedOrigin(req, env);
  if (origin) {
    res.headers.set("Access-Control-Allow-Origin", origin);
    res.headers.set("Vary", "Origin");
    res.headers.set("Access-Control-Allow-Methods", "POST, OPTIONS");
    res.headers.set("Access-Control-Allow-Headers", "Content-Type");
    res.headers.set("Access-Control-Max-Age", "86400");
  }
  return res;
}
function json(obj, status, req, env) {
  const res = new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json; charset=utf-8" } });
  return req ? withCors(res, req, env) : res;
}
function redirect(to) {
  return new Response(null, { status: 303, headers: { Location: to } });
}
function page(title, bodyHtml, status = 200) {
  return new Response(`<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>${esc(title)}</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:520px;margin:12vh auto;padding:0 20px;color:#1b1f24}
h1{font-size:22px}button{font:inherit;font-weight:600;background:#0b3b8c;color:#fff;border:0;border-radius:8px;padding:10px 18px;cursor:pointer}
a{color:#0b4fc4}</style></head><body>${bodyHtml}</body></html>`, { status, headers: { "content-type": "text/html; charset=utf-8" } });
}
function token() {
  const b = new Uint8Array(24);
  crypto.getRandomValues(b);
  return [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
}
async function sha256(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((x) => x.toString(16).padStart(2, "0")).join("").slice(0, 32);
}
function sameSecret(a, b) {  // constant-time compare
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
function authorized(req, env) {
  const h = req.headers.get("Authorization") || "";
  return sameSecret(h.replace(/^Bearer\s+/i, ""), String(env.ALERTS_TOKEN || ""));
}
function pageUrl(env, slug, params) {
  return `${site(env)}/${slug}/${params ? `?${params}` : ""}`;
}

async function sendEmail(env, { to, subject, html, text, unsubscribeUrl }) {
  const payload = {
    from: env.MAIL_FROM, to: [to], subject, html, text,
    headers: unsubscribeUrl ? { "List-Unsubscribe": `<${unsubscribeUrl}>`, "List-Unsubscribe-Post": "List-Unsubscribe=One-Click" } : undefined,
  };
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) console.error("email failed", r.status, await r.text().catch(() => ""));
  return r.ok;
}

function emailShell(inner, footer) {
  return `<div style="font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;color:#1b1f24;max-width:560px">
<div style="font-weight:700;font-size:18px;color:#0b3b8c;margin-bottom:16px">Bhaau <span style="font-weight:400;color:#6e7784;font-size:14px">iPhone prices in Nepal</span></div>
${inner}<p style="color:#6e7784;font-size:13px;margin-top:28px;border-top:1px solid #e2e6eb;padding-top:12px">${footer}</p></div>`;
}

// ------------------------------------------------------------------ /subscribe
async function subscribe(req, env) {
  if (!allowedOrigin(req, env)) return json({ ok: false, error: "Not allowed." }, 403, req, env);
  let body;
  try { body = await req.json(); } catch { return json({ ok: false, error: "Could not read the form." }, 400, req, env); }
  if (body.website) return json({ ok: true, status: "pending" }, 200, req, env);  // honeypot field filled by a bot

  const email = String(body.email || "").trim().toLowerCase();
  if (email.length > 254 || !EMAIL_RE.test(email)) return json({ ok: false, error: "Enter a valid email address." }, 400, req, env);
  const variant = String(body.variant || "");
  if (!VARIANT_RE.test(variant)) return json({ ok: false, error: "This phone isn't tracked." }, 400, req, env);
  const v = await env.DB.prepare("SELECT * FROM variants WHERE key = ?").bind(variant).first();
  if (!v) return json({ ok: false, error: "This phone isn't tracked yet. Try again after the next price check." }, 404, req, env);

  let target = null;
  if (body.target !== undefined && body.target !== null && String(body.target).trim() !== "") {
    target = parseInt(String(body.target).replace(/[^\d]/g, ""), 10);
    if (!Number.isFinite(target) || target < 10000 || target > 2000000)
      return json({ ok: false, error: "Enter the target price in rupees, for example 180000." }, 400, req, env);
  }

  const ipHash = await sha256(`${req.headers.get("CF-Connecting-IP") || "?"}|${env.ALERTS_TOKEN || ""}`);
  const hourAgo = new Date(Date.now() - 3600e3).toISOString();
  const recent = await env.DB.prepare("SELECT COUNT(*) AS n FROM alerts WHERE ip_hash = ? AND created_at > ?").bind(ipHash, hourAgo).first();
  if (recent && recent.n >= MAX_SIGNUPS_PER_IP_PER_HOUR)
    return json({ ok: false, error: "Too many sign-ups from your connection. Please try again in an hour." }, 429, req, env);

  const existing = await env.DB.prepare("SELECT * FROM alerts WHERE email = ? AND variant = ?").bind(email, variant).first();
  if (existing) {
    await env.DB.prepare("UPDATE alerts SET target = ? WHERE id = ?").bind(target, existing.id).run();
    if (existing.status === "active")
      return json({ ok: true, status: "active", message: target ? `Your alert is already on. We'll email you when it's ${rs(target)} or less.` : "Your alert for this phone is already on." }, 200, req, env);
    const resendAfter = new Date(Date.now() - RESEND_CONFIRM_AFTER_MIN * 60e3).toISOString();
    if (!existing.confirm_sent_at || existing.confirm_sent_at < resendAfter) {
      if (!(await sendConfirmation(req, env, email, existing.token, v, target)))
        return json({ ok: false, error: "We couldn't send the confirmation email. Please try again later." }, 502, req, env);
      await env.DB.prepare("UPDATE alerts SET confirm_sent_at = ? WHERE id = ?").bind(now(), existing.id).run();
    }
    return json({ ok: true, status: "pending", message: "Check your inbox and click the link to turn on the alert." + alreadyMet(v, target) }, 200, req, env);
  }

  const count = await env.DB.prepare("SELECT COUNT(*) AS n FROM alerts WHERE email = ?").bind(email).first();
  if (count && count.n >= MAX_ALERTS_PER_EMAIL)
    return json({ ok: false, error: `You can have up to ${MAX_ALERTS_PER_EMAIL} alerts. Remove one from an alert email first.` }, 400, req, env);

  const tok = token();
  await env.DB.prepare(
    "INSERT INTO alerts (email, variant, target, baseline, status, token, ip_hash, created_at, confirm_sent_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
  ).bind(email, variant, target, v.price ?? null, tok, ipHash, now(), now()).run();
  if (!(await sendConfirmation(req, env, email, tok, v, target))) {
    await env.DB.prepare("DELETE FROM alerts WHERE token = ?").bind(tok).run();
    return json({ ok: false, error: "We couldn't send the confirmation email. Please try again later." }, 502, req, env);
  }
  return json({ ok: true, status: "pending", message: "Check your inbox and click the link to turn on the alert." + alreadyMet(v, target) }, 200, req, env);
}

function alreadyMet(v, target) {
  return target && v.price && v.price <= target
    ? ` It's already ${rs(v.price)} at ${v.store}, which is at or below your target, so we'll email you if it drops further.`
    : "";
}

async function sendConfirmation(req, env, email, tok, v, target) {
  const confirmUrl = `${self(req)}/confirm?token=${tok}`;
  const what = target ? `when ${esc(v.label)} is ${rs(target)} or less` : `when the price of ${esc(v.label)} drops`;
  const now_ = alreadyMet(v, target).trim() ? esc(alreadyMet(v, target).trim())
    : v.price ? `It's ${rs(v.price)} right now, at ${esc(v.store)}.` : "It's out of stock everywhere right now.";
  const html = emailShell(
    `<h1 style="font-size:20px;margin:0 0 10px">Confirm your price alert</h1>
     <p>Click the button to get an email ${what}. ${now_}</p>
     <p><a href="${confirmUrl}" style="display:inline-block;background:#0d8a4c;color:#fff;text-decoration:none;font-weight:600;padding:11px 20px;border-radius:8px">Turn on the alert</a></p>`,
    "You're getting this because someone entered this address on Bhaau. If that wasn't you, ignore this email and nothing will happen.");
  const text = `Confirm your price alert ${what.replace(/&amp;/g, "&")}.\n${now_}\n\nTurn it on: ${confirmUrl}\n\nIf that wasn't you, ignore this email.`;
  return sendEmail(env, { to: email, subject: `Confirm your price alert: ${v.label}`, html, text });
}

// ------------------------------------------------------------------ /confirm
async function confirm(url, env) {
  const tok = url.searchParams.get("token") || "";
  const row = tok && await env.DB.prepare(
    "SELECT a.*, v.slug, v.price FROM alerts a JOIN variants v ON v.key = a.variant WHERE a.token = ?").bind(tok).first();
  if (!row) return redirect(`${site(env)}/?alert=invalid`);
  if (row.status !== "active") {
    await env.DB.prepare("UPDATE alerts SET status = 'active', confirmed_at = ?, baseline = COALESCE(baseline, ?) WHERE id = ?")
      .bind(now(), row.price ?? null, row.id).run();
  }
  return redirect(pageUrl(env, row.slug, "alert=confirmed"));
}

// ------------------------------------------------------------------ /unsubscribe
async function unsubscribe(req, url, env) {
  const tok = url.searchParams.get("token") || "";
  const all = url.searchParams.get("all") === "1";
  const row = tok && await env.DB.prepare(
    "SELECT a.*, v.label, v.slug FROM alerts a LEFT JOIN variants v ON v.key = a.variant WHERE a.token = ?").bind(tok).first();
  if (req.method === "GET") {
    // Show a button instead of deleting straight away: email scanners open links automatically.
    if (!row) return page("Alert not found", `<h1>This alert is already off</h1><p><a href="${esc(site(env))}/">Back to Bhaau</a></p>`, 404);
    const what = all ? "all your Bhaau price alerts" : `price alerts for ${esc(row.label || "this phone")}`;
    return page("Stop price alerts", `<h1>Stop ${what}?</h1>
      <form method="post" action="/unsubscribe?token=${esc(tok)}${all ? "&amp;all=1" : ""}"><button type="submit">Stop the emails</button></form>
      <p><a href="${esc(site(env))}/">Keep them and go back to Bhaau</a></p>`);
  }
  if (req.method !== "POST") return new Response("Method not allowed", { status: 405 });
  if (row) {
    if (all) await env.DB.prepare("DELETE FROM alerts WHERE email = ?").bind(row.email).run();
    else await env.DB.prepare("DELETE FROM alerts WHERE id = ?").bind(row.id).run();
  }
  // One-click unsubscribe from a mail app (RFC 8058) comes without an Origin header: a plain 200 is enough.
  // The button on our own page is posted by a browser, which always sends Origin.
  if (!req.headers.get("Origin")) return new Response("OK");
  return page("Alerts stopped", `<h1>Done. You won't get these emails any more.</h1>
    <p><a href="${esc(row && row.slug ? pageUrl(env, row.slug) : site(env) + "/")}">Back to Bhaau</a></p>`);
}

// ------------------------------------------------------------------ /notify (from the scraper)
async function notify(req, env) {
  if (!authorized(req, env)) return json({ ok: false, error: "Unauthorized" }, 401);
  const body = await req.json();
  const list = Array.isArray(body.variants) ? body.variants : [];
  const ts = now();
  const upserts = list.filter((v) => VARIANT_RE.test(String(v.key || ""))).map((v) =>
    env.DB.prepare(`INSERT INTO variants (key, label, slug, storage_gb, price, store, url, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(key) DO UPDATE SET label=excluded.label, slug=excluded.slug, storage_gb=excluded.storage_gb,
        price=excluded.price, store=excluded.store, url=excluded.url, updated_at=excluded.updated_at`)
      .bind(v.key, String(v.label || v.key).slice(0, 80), String(v.slug || "").slice(0, 60), v.storage_gb ?? null,
        Number.isFinite(v.price) ? Math.round(v.price) : null, v.store ?? null, v.url ?? null, ts));
  for (let i = 0; i < upserts.length; i += 50) await env.DB.batch(upserts.slice(i, i + 50));

  // Sign-ups nobody confirmed are deleted after 7 days (the privacy page promises this).
  const weekAgo = new Date(Date.now() - 7 * 86400e3).toISOString();
  await env.DB.prepare("DELETE FROM alerts WHERE status = 'pending' AND created_at < ?").bind(weekAgo).run();

  // Alerts whose phone is now cheaper than the price we last told them about (and at/below their target).
  const { results } = await env.DB.prepare(`
    SELECT a.id, a.email, a.token, a.target, a.baseline, v.label, v.slug, v.price, v.store, v.url, v.storage_gb
    FROM alerts a JOIN variants v ON v.key = a.variant
    WHERE a.status = 'active' AND v.price IS NOT NULL
      AND (a.baseline IS NULL OR v.price < a.baseline)
      AND (a.target IS NULL OR v.price <= a.target)
    ORDER BY a.email, v.price`).all();

  const byEmail = new Map();
  for (const r of results || []) {
    if (!byEmail.has(r.email)) byEmail.set(r.email, []);
    byEmail.get(r.email).push(r);
  }
  const cap = parseInt(env.MAX_EMAILS_PER_RUN || "90", 10);
  let sent = 0, deferred = 0;
  for (const [email, items] of byEmail) {
    if (sent >= cap) { deferred += 1; continue; }  // baseline unchanged, so they go out next run
    const ok = await sendEmail(env, dropEmail(req, env, email, items));
    if (!ok) { deferred += 1; continue; }
    sent += 1;
    await env.DB.batch(items.map((it) =>
      env.DB.prepare("UPDATE alerts SET baseline = ?, last_sent_at = ? WHERE id = ?").bind(it.price, ts, it.id)));
  }
  return json({ ok: true, variants: upserts.length, alerts_matched: (results || []).length, emails_sent: sent, emails_deferred: deferred }, 200);
}

function dropEmail(req, env, email, items) {
  const base = self(req);
  const lines = items.map((it) => {
    const was = it.baseline ? `<span style="color:#6e7784;text-decoration:line-through">${rs(it.baseline)}</span> ` : "";
    const why = it.baseline ? `down ${rs(it.baseline - it.price)}` : "back in stock";
    return `<tr><td style="padding:12px 0;border-bottom:1px solid #eef1f4">
      <a href="${esc(pageUrl(env, it.slug, `storage=${it.storage_gb || ""}`))}" style="color:#1b1f24;font-weight:600;text-decoration:none">${esc(it.label)}</a><br>
      <span style="color:#4a5360;font-size:14px">${was}<b style="color:#0a7a43">${rs(it.price)}</b> at ${esc(it.store)}, ${why}</span><br>
      <a href="${esc(it.url)}" style="font-size:14px;color:#0b4fc4">Go to ${esc(it.store)}</a>
      <span style="color:#c4c9d0"> · </span>
      <a href="${base}/unsubscribe?token=${it.token}" style="font-size:13px;color:#6e7784">Stop alerts for this phone</a></td></tr>`;
  }).join("");
  const first = items[0];
  const subject = items.length === 1
    ? `${first.label} is now ${rs(first.price)} at ${first.store}`
    : `${items.length} iPhones you're watching are cheaper now`;
  const stopAll = `${base}/unsubscribe?token=${first.token}&all=1`;
  const html = emailShell(
    `<h1 style="font-size:20px;margin:0 0 6px">Price drop</h1>
     <p style="margin:0 0 6px;color:#4a5360">Prices are from each seller's website. Confirm the price with the seller before paying.</p>
     <table style="width:100%;border-collapse:collapse">${lines}</table>`,
    `You asked Bhaau to tell you when these prices drop. <a href="${stopAll}" style="color:#6e7784">Stop all price alerts</a>.`);
  const text = items.map((it) => `${it.label}: ${rs(it.price)} at ${it.store}${it.baseline ? ` (was ${rs(it.baseline)})` : ""}\n${it.url}`).join("\n\n") +
    `\n\nStop all price alerts: ${stopAll}`;
  return { to: email, subject, html, text, unsubscribeUrl: stopAll };
}

// ------------------------------------------------------------------ /stats
async function stats(req, env) {
  if (!authorized(req, env)) return json({ ok: false, error: "Unauthorized" }, 401);
  const q = (sql) => env.DB.prepare(sql).first();
  const active = await q("SELECT COUNT(*) AS n FROM alerts WHERE status = 'active'");
  const pending = await q("SELECT COUNT(*) AS n FROM alerts WHERE status = 'pending'");
  const people = await q("SELECT COUNT(DISTINCT email) AS n FROM alerts WHERE status = 'active'");
  const top = await env.DB.prepare(`SELECT v.label, COUNT(*) AS alerts FROM alerts a JOIN variants v ON v.key = a.variant
    WHERE a.status = 'active' GROUP BY a.variant ORDER BY alerts DESC LIMIT 10`).all();
  return json({ ok: true, active_alerts: active.n, pending_alerts: pending.n, people: people.n, most_watched: top.results || [] }, 200);
}
