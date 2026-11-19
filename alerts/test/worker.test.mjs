// Run:  node --test alerts/test/
// Uses Node's built-in SQLite (Node 22+) behind a tiny Cloudflare-D1-compatible wrapper,
// and a fake Resend API that records emails instead of sending them.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import worker from "../worker.js";

const SCHEMA = readFileSync(new URL("../schema.sql", import.meta.url), "utf8");
const SITE = "https://bhaau.com.np";
const API = "https://alerts.bhaau.workers.dev";

class Stmt {
  constructor(db, sql, args = []) { this.db = db; this.sql = sql; this.args = args; }
  bind(...a) { return new Stmt(this.db, this.sql, a.map((x) => (x === undefined ? null : x))); }
  async first() { return this.db.prepare(this.sql).get(...this.args) ?? null; }
  async all() { return { results: this.db.prepare(this.sql).all(...this.args) }; }
  async run() { return this.runSync(); }
  runSync() { const r = this.db.prepare(this.sql).run(...this.args); return { meta: { changes: r.changes } }; }
}
class D1 {
  constructor() { this.db = new DatabaseSync(":memory:"); this.db.exec(SCHEMA); }
  prepare(sql) { return new Stmt(this.db, sql); }
  async batch(stmts) { return stmts.map((s) => s.runSync()); }
}

let env, mails, resendFails;
beforeEach(() => {
  env = { DB: new D1(), SITE_URL: SITE, SITE_ORIGINS: `${SITE},http://localhost:8000`, MAIL_FROM: "Bhaau <alerts@bhaau.com.np>",
          RESEND_API_KEY: "re_test", ALERTS_TOKEN: "secret-token-123", MAX_EMAILS_PER_RUN: "90" };
  mails = []; resendFails = false;
  globalThis.fetch = async (url, init) => {
    assert.equal(url, "https://api.resend.com/emails");
    if (resendFails) return new Response("down", { status: 500 });
    mails.push(JSON.parse(init.body));
    return new Response('{"id":"x"}', { status: 200 });
  };
});

const call = (path, { method = "GET", body, origin = SITE, auth, ip = "1.2.3.4", form } = {}) => {
  const headers = { "CF-Connecting-IP": ip };
  if (origin) headers.Origin = origin;
  if (auth) headers.Authorization = `Bearer ${auth}`;
  if (body) headers["Content-Type"] = "application/json";
  if (form) headers["Content-Type"] = "application/x-www-form-urlencoded";
  return worker.fetch(new Request(API + path, { method, headers, body: body ? JSON.stringify(body) : form }), env);
};
const notify = (variants, auth = env.ALERTS_TOKEN) => call("/notify", { method: "POST", body: { variants }, auth, origin: null });
const AIR = (price, store = "Oliz Store") => ({ key: "iphone-air|256", label: "iPhone Air 256 GB", slug: "iphone-air", storage_gb: 256, price, store, url: "https://oliz/p" });
const PRO = (price) => ({ key: "iphone-17-pro|512", label: "iPhone 17 Pro 512 GB", slug: "iphone-17-pro", storage_gb: 512, price, store: "Evo Store", url: "https://evo/p" });
const tokenOf = (email, variant) => env.DB.db.prepare("SELECT token FROM alerts WHERE email = ? AND variant = ?").get(email, variant).token;
const confirmLink = (mail) => mail.text.match(/https:\/\/\S+\/confirm\?token=\w+/)[0];

async function signUp(email, variant = "iphone-air|256", target, ip) {
  const r = await call("/subscribe", { method: "POST", body: { email, variant, target }, ip });
  assert.equal(r.status, 200, await r.clone().text());
  const res = await call(new URL(confirmLink(mails.at(-1))).pathname + new URL(confirmLink(mails.at(-1))).search);
  return res;
}

test("notify is refused without the token", async () => {
  assert.equal((await notify([AIR(190699)], "wrong")).status, 401);
});

test("sign-up sends a confirmation email; confirming turns the alert on and goes back to the phone page", async () => {
  await notify([AIR(190699)]);
  const r = await call("/subscribe", { method: "POST", body: { email: " Ram@Example.COM ", variant: "iphone-air|256" } });
  const j = await r.json();
  assert.equal(r.status, 200); assert.equal(j.status, "pending");
  assert.equal(r.headers.get("Access-Control-Allow-Origin"), SITE);
  assert.equal(mails.length, 1);
  assert.equal(mails[0].to[0], "ram@example.com");
  assert.match(mails[0].subject, /Confirm your price alert: iPhone Air 256 GB/);
  assert.match(mails[0].text, /Rs 1,90,699/);
  const link = new URL(confirmLink(mails[0]));
  const c = await call(link.pathname + link.search);
  assert.equal(c.status, 303);
  assert.equal(c.headers.get("Location"), `${SITE}/iphone-air/?alert=confirmed`);
  const row = env.DB.db.prepare("SELECT status, baseline FROM alerts").get();
  assert.deepEqual({ ...row }, { status: "active", baseline: 190699 });
});

test("bad input, other websites, bots and unknown phones are refused", async () => {
  await notify([AIR(190699)]);
  assert.equal((await call("/subscribe", { method: "POST", body: { email: "nope", variant: "iphone-air|256" } })).status, 400);
  assert.equal((await call("/subscribe", { method: "POST", body: { email: "a@b.com", variant: "iphone-99|256" } })).status, 404);
  assert.equal((await call("/subscribe", { method: "POST", body: { email: "a@b.com", variant: "x';drop" } })).status, 400);
  assert.equal((await call("/subscribe", { method: "POST", body: { email: "a@b.com", variant: "iphone-air|256", target: "5" } })).status, 400);
  assert.equal((await call("/subscribe", { method: "POST", origin: "https://evil.example", body: { email: "a@b.com", variant: "iphone-air|256" } })).status, 403);
  const bot = await call("/subscribe", { method: "POST", body: { email: "a@b.com", variant: "iphone-air|256", website: "spam" } });
  assert.equal(bot.status, 200);
  assert.equal(env.DB.db.prepare("SELECT COUNT(*) AS n FROM alerts").get().n, 0);
  assert.equal(mails.length, 0);
});

test("an email goes out only when the price drops below the last price we told them, once", async () => {
  await notify([AIR(190699)]);
  await signUp("sita@example.com");
  mails = [];
  await notify([AIR(190699)]);                       // same price: nothing
  assert.equal(mails.length, 0);
  await notify([AIR(195000)]);                       // went up: nothing
  assert.equal(mails.length, 0);
  const r = await (await notify([AIR(185499, "Evo Store")])).json();
  assert.equal(r.emails_sent, 1);
  assert.equal(mails[0].subject, "iPhone Air 256 GB is now Rs 1,85,499 at Evo Store");
  assert.match(mails[0].text, /was Rs 1,90,699/);
  assert.match(mails[0].headers["List-Unsubscribe"], /unsubscribe\?token=\w+&all=1/);
  mails = [];
  await notify([AIR(185499, "Evo Store")]);           // still that price: no repeat
  assert.equal(mails.length, 0);
});

test("a target price waits until the price reaches it", async () => {
  await notify([AIR(190699)]);
  await signUp("hari@example.com", "iphone-air|256", "Rs 1,80,000");
  mails = [];
  await notify([AIR(185000)]);
  assert.equal(mails.length, 0, "dropped, but not to the target yet");
  await notify([AIR(179999)]);
  assert.equal(mails.length, 1);
  assert.match(mails[0].subject, /Rs 1,79,999/);
});

test("several drops for one person arrive in one email; out of stock never triggers", async () => {
  await notify([AIR(190699), PRO(259999)]);
  await signUp("gita@example.com", "iphone-air|256");
  await signUp("gita@example.com", "iphone-17-pro|512");
  mails = [];
  await notify([AIR(null), PRO(259999)]);             // nobody has stock: nothing
  assert.equal(mails.length, 0);
  await notify([AIR(185000), PRO(255000)]);
  assert.equal(mails.length, 1);
  assert.equal(mails[0].subject, "2 iPhones you're watching are cheaper now");
});

test("the daily email cap defers the rest to the next run instead of losing them", async () => {
  env.MAX_EMAILS_PER_RUN = "1";
  await notify([AIR(190699)]);
  await signUp("a@example.com"); await signUp("b@example.com");
  mails = [];
  let r = await (await notify([AIR(180000)])).json();
  assert.deepEqual([r.emails_sent, r.emails_deferred], [1, 1]);
  r = await (await notify([AIR(180000)])).json();
  assert.deepEqual([r.emails_sent, r.emails_deferred], [1, 0]);
  assert.deepEqual(mails.map((m) => m.to[0]).sort(), ["a@example.com", "b@example.com"]);
});

test("unsubscribe: the link shows a button, the button stops the alert, mail-app one-click works", async () => {
  await notify([AIR(190699), PRO(259999)]);
  await signUp("ram@example.com"); await signUp("ram@example.com", "iphone-17-pro|512");
  const tok = tokenOf("ram@example.com", "iphone-air|256");
  const g = await call(`/unsubscribe?token=${tok}`);
  assert.equal(g.status, 200);
  assert.match(await g.text(), /<button type="submit">Stop the emails<\/button>/);
  assert.equal(env.DB.db.prepare("SELECT COUNT(*) AS n FROM alerts").get().n, 2, "GET alone deletes nothing");
  const p = await call(`/unsubscribe?token=${tok}`, { method: "POST", origin: API, form: "" });
  assert.match(await p.text(), /You won't get these emails/);
  assert.equal(env.DB.db.prepare("SELECT COUNT(*) AS n FROM alerts").get().n, 1);
  const tok2 = tokenOf("ram@example.com", "iphone-17-pro|512");
  const one = await call(`/unsubscribe?token=${tok2}&all=1`, { method: "POST", origin: null, form: "List-Unsubscribe=One-Click" });
  assert.equal(await one.text(), "OK");
  assert.equal(env.DB.db.prepare("SELECT COUNT(*) AS n FROM alerts").get().n, 0);
});

test("signing up twice doesn't send twice, and too many sign-ups from one connection are slowed down", async () => {
  await notify([AIR(190699)]);
  await call("/subscribe", { method: "POST", body: { email: "x@example.com", variant: "iphone-air|256" } });
  await call("/subscribe", { method: "POST", body: { email: "x@example.com", variant: "iphone-air|256" } });
  assert.equal(mails.length, 1, "second request within 10 minutes doesn't resend");
  for (let i = 0; i < 9; i++) await call("/subscribe", { method: "POST", body: { email: `u${i}@example.com`, variant: "iphone-air|256" } });
  const r = await call("/subscribe", { method: "POST", body: { email: "late@example.com", variant: "iphone-air|256" } });
  assert.equal(r.status, 429);
});

test("if the confirmation email can't be sent, nothing is stored", async () => {
  await notify([AIR(190699)]);
  resendFails = true;
  const r = await call("/subscribe", { method: "POST", body: { email: "y@example.com", variant: "iphone-air|256" } });
  assert.equal(r.status, 502);
  assert.equal(env.DB.db.prepare("SELECT COUNT(*) AS n FROM alerts").get().n, 0);
});

test("stats need the token", async () => {
  await notify([AIR(190699)]);
  await signUp("s@example.com");
  assert.equal((await call("/stats")).status, 401);
  const s = await (await call("/stats", { auth: env.ALERTS_TOKEN })).json();
  assert.deepEqual([s.active_alerts, s.people, s.most_watched[0].label], [1, 1, "iPhone Air 256 GB"]);
});

test("a target that's already met is explained at sign-up", async () => {
  await notify([AIR(177099)]);
  const j = await (await call("/subscribe", { method: "POST", body: { email: "k@example.com", variant: "iphone-air|256", target: "180000" } })).json();
  assert.match(j.message, /already Rs 1,77,099 at Oliz Store, which is at or below your target/);
  assert.match(mails[0].text, /already Rs 1,77,099/);
});

test("sign-ups nobody confirms are deleted after 7 days", async () => {
  await notify([AIR(180000)]);
  await call("/subscribe", { method: "POST", body: { email: "old@example.com", variant: "iphone-air|256" } });
  await call("/subscribe", { method: "POST", body: { email: "new@example.com", variant: "iphone-air|256" }, ip: "5.6.7.8" });
  await signUp("kept@example.com", "iphone-air|256", undefined, "9.9.9.9");
  const eightDaysAgo = new Date(Date.now() - 8 * 86400e3).toISOString();
  env.DB.db.prepare("UPDATE alerts SET created_at = ? WHERE email IN ('old@example.com', 'kept@example.com')").run(eightDaysAgo);
  await notify([AIR(180000)]);
  const left = env.DB.db.prepare("SELECT email FROM alerts ORDER BY email").all().map((r) => r.email);
  assert.deepEqual(left, ["kept@example.com", "new@example.com"]);
});

test("a site in a sub-folder (GitHub Pages project site) is still allowed to sign people up", async () => {
  env.SITE_URL = "https://you.github.io/bhaau"; env.SITE_ORIGINS = "https://you.github.io/bhaau/";
  await notify([AIR(180000)]);
  const r = await call("/subscribe", { method: "POST", body: { email: "a@example.com", variant: "iphone-air|256" }, origin: "https://you.github.io" });
  assert.equal(r.status, 200);
  assert.match(mails.at(-1).text, /https:\/\/\S+\/confirm\?token=/);
  const bad = await call("/subscribe", { method: "POST", body: { email: "b@example.com", variant: "iphone-air|256" }, origin: "https://evil.example", ip: "7.7.7.7" });
  assert.equal(bad.status, 403);
});
