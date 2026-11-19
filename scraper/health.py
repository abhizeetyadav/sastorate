"""Health check: notice when a store breaks or a price looks wrong, and tell you.

Checks every enabled store's latest scrape:
  * failed        - the store couldn't be scraped (blocked, site down, error)
  * no prices     - it was scraped but nothing was found (layout probably changed)
  * far fewer     - it found much fewer phones than usual (half or less of its normal count)
  * not checked   - no scrape for over 30 hours (the schedule itself may have stopped)
  * price jump    - a phone's price moved more than 30% since the previous check

`python -m scraper health` prints the report. With --notify it also emails it to
OWNER_EMAIL through Resend (RESEND_API_KEY, MAIL_FROM), at most once a day per
problem, and exits with code 1 so a GitHub Actions run shows as failed (GitHub
then emails you too, as a backup).
"""
from __future__ import annotations

import html
import logging
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from .db import DB, now_iso

log = logging.getLogger(__name__)

JUMP = 0.30            # price moved more than 30%
FEWER = 0.5            # found half or less of the usual number of phones
STALE_HOURS = 30
RENOTIFY_HOURS = 24

SCHEMA = """CREATE TABLE IF NOT EXISTS health_notices (
    key TEXT PRIMARY KEY, message TEXT, first_seen TEXT NOT NULL, last_notified TEXT)"""


@dataclass
class Problem:
    key: str        # stable id, used to avoid repeating the same email
    store: str
    severity: str   # "broken" | "check"
    message: str


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _gb(g) -> str:
    return "" if not g else (f"{g // 1024} TB" if g >= 1024 else f"{g} GB")


def _rs(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return f"Rs {s}"
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:]); head = head[:-2]
    if head:
        parts.insert(0, head)
    return "Rs " + ",".join(parts + [tail])


def check(db: DB, stores: list[dict], now: datetime | None = None) -> list[Problem]:
    now = now or datetime.now(timezone.utc)
    problems: list[Problem] = []
    for s in stores:
        if not s.get("enabled", True):
            continue
        sid, name = s["id"], s["name"]
        market = (s.get("kind") or "").lower() == "marketplace"  # listings come and go: only real failures count
        runs = db.q("SELECT * FROM runs WHERE store_id = ? ORDER BY id DESC LIMIT 6", sid)
        if not runs:
            problems.append(Problem(f"{sid}:never", sid, "broken", f"{name} has never been scraped."))
            continue
        last = runs[0]
        when = _dt(last["finished_at"] or last["started_at"])
        if now - when > timedelta(hours=STALE_HOURS):
            hours = int((now - when).total_seconds() // 3600)
            problems.append(Problem(f"{sid}:stale", sid, "broken",
                                    f"{name} hasn't been checked for {hours} hours. Is the scheduled scrape still running?"))
        if last["status"] == "error":
            problems.append(Problem(f"{sid}:error", sid, "broken",
                                    f"{name} couldn't be scraped: {(last['error'] or 'unknown error')[:300]}"))
        elif last["status"] == "empty":
            problems.append(Problem(f"{sid}:empty", sid, "broken",
                                    f"{name} was scraped but no iPhone prices were found. Its website has probably changed. "
                                    f"Try: python -m scraper probe <an iPhone page> --store {sid}"))
        elif last["status"] == "ok" and not market:
            usual = [r["offers"] for r in runs[1:] if r["status"] == "ok" and r["offers"]]
            if len(usual) >= 2:
                normal = statistics.median(usual)
                if normal >= 4 and last["offers"] <= normal * FEWER:
                    problems.append(Problem(f"{sid}:fewer", sid, "check",
                                            f"{name} returned {last['offers']} phones; it usually has about {int(normal)}. "
                                            "Some pages may have changed or failed."))

        if market:
            continue
        # price jumps recorded during the latest scrape of this store
        since = last["started_at"]
        rows = db.q("""SELECT p.*, (SELECT q.price FROM price_points q WHERE q.store_id = p.store_id AND q.variant_key = p.variant_key
                                    AND q.id < p.id ORDER BY q.id DESC LIMIT 1) AS prev_price
                       FROM price_points p WHERE p.store_id = ? AND p.first_seen >= ?""", sid, since)
        for r in rows:
            prev = r["prev_price"]
            if prev and abs(r["price"] - prev) / prev > JUMP:
                pct = round((r["price"] - prev) / prev * 100)
                problems.append(Problem(
                    f"{sid}:jump:{r['variant_key']}:{r['price']}", sid, "check",
                    f"{name}: {r['model']} {_gb(r['storage_gb'])} went from {_rs(prev)} to {_rs(r['price'])} "
                    f"({'+' if pct > 0 else ''}{pct}%). If that's wrong, hide it in overrides.yaml."))
    return problems


def report_text(problems: list[Problem]) -> str:
    if not problems:
        return "All stores look healthy."
    lines = [f"{len(problems)} problem{'s' if len(problems) != 1 else ''} found:"]
    for p in problems:
        lines.append(f"  [{'BROKEN' if p.severity == 'broken' else 'CHECK '}] {p.message}")
    return "\n".join(lines)


def _due(db: DB, problems: list[Problem], now: datetime) -> list[Problem]:
    """Problems not emailed in the last 24 h. Also forgets problems that went away."""
    db.conn.execute(SCHEMA)
    current = {p.key for p in problems}
    for row in db.q("SELECT key FROM health_notices"):
        if row["key"] not in current:
            db.conn.execute("DELETE FROM health_notices WHERE key = ?", (row["key"],))
    due = []
    for p in problems:
        row = db.q("SELECT * FROM health_notices WHERE key = ?", p.key)
        if not row:
            db.conn.execute("INSERT INTO health_notices (key, message, first_seen) VALUES (?, ?, ?)", (p.key, p.message, now_iso()))
            due.append(p)
        elif not row[0]["last_notified"] or now - _dt(row[0]["last_notified"]) > timedelta(hours=RENOTIFY_HOURS):
            due.append(p)
    db.commit()
    return due


def _mark_notified(db: DB, problems: list[Problem]) -> None:
    for p in problems:
        db.conn.execute("UPDATE health_notices SET last_notified = ? WHERE key = ?", (now_iso(), p.key))
    db.commit()


def send_email(problems: list[Problem], status_url: str | None = None, subject: str | None = None) -> str:
    """Returns "sent", "not_configured" or "failed"."""
    key, to = os.getenv("RESEND_API_KEY"), os.getenv("OWNER_EMAIL")
    # Resend's shared test sender only delivers to your own Resend account address - fine for this.
    sender = os.getenv("MAIL_FROM") or "Bhaau <onboarding@resend.dev>"
    if not (key and to):
        log.warning("Health email not sent: set RESEND_API_KEY and OWNER_EMAIL to get these by email")
        return "not_configured"
    broken = sum(p.severity == "broken" for p in problems)
    subject = subject or (f"Bhaau: {broken} store{'s' if broken != 1 else ''} not working" if broken
               else f"Bhaau: {len(problems)} price{'s' if len(problems) != 1 else ''} to check")
    items = "".join(f'<li style="margin:0 0 10px"><b style="color:{"#c2271b" if p.severity == "broken" else "#8a5a00"}">'
                    f'{"Broken" if p.severity == "broken" else "Check"}:</b> {html.escape(p.message)}</li>' for p in problems)
    link = f'<p><a href="{html.escape(status_url)}">Open the status page</a></p>' if status_url else ""
    body = (f'<div style="font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#1b1f24;max-width:600px">'
            f'<h2 style="margin:0 0 12px">Bhaau health check</h2><ul style="padding-left:18px">{items}</ul>{link}'
            f'<p style="color:#6e7784;font-size:13px">You get each problem at most once a day while it lasts.</p></div>')
    try:
        r = requests.post("https://api.resend.com/emails", timeout=30,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"from": sender, "to": [to], "subject": subject, "html": body, "text": report_text(problems)})
        if not r.ok:
            try:
                why = r.json().get("message") or r.text
            except ValueError:
                why = r.text
            log.error("Health email failed (%s): %s", r.status_code, why[:300])
            return "failed"
        return "sent"
    except Exception as e:  # noqa: BLE001
        log.error("Health email failed: %s", e)
        return "failed"


def send_test_email(status_url: str | None = None) -> int:
    """`python -m scraper health --test-email`: check the email settings without waiting for a problem."""
    to = os.getenv("OWNER_EMAIL") or "(OWNER_EMAIL not set)"
    print(f"Sending a test email to {to} from {os.getenv('MAIL_FROM') or 'Bhaau <onboarding@resend.dev>'} ...")
    result = send_email([Problem("test", "-", "check", "This is a test. If you can read it, Bhaau can email you "
                                 "when a store stops working.")], status_url, subject="Bhaau: test email")
    print({"sent": "Sent. Check your inbox, and the spam folder the first time.",
           "not_configured": "Not sent: add RESEND_API_KEY and OWNER_EMAIL to .env.",
           "failed": "Not sent: see the error above."}[result])
    return 0 if result == "sent" else 1


def run(db: DB, stores: list[dict], notify: bool = False, status_url: str | None = None) -> int:
    """Print the report; with notify, email new problems. Returns an exit code (1 = something to look at)."""
    problems = check(db, stores)
    print(report_text(problems))
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:  # shows on the GitHub Actions run page
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Store health\n\n" + ("\n".join(f"- **{p.severity}**: {p.message}" for p in problems) or "All stores healthy.") + "\n")
    if not notify:
        return 0
    due = _due(db, problems, datetime.now(timezone.utc))
    if not due:
        if problems:
            print("(Already reported in the last 24 hours, so no new email.)")
        return 0
    result = send_email(due, status_url)
    if result == "sent":
        print(f"Emailed {len(due)} problem{'s' if len(due) != 1 else ''} to {os.getenv('OWNER_EMAIL')}.")
    if result != "failed":
        # Without email set up, the failed GitHub run is the notice: still only once a day per problem.
        _mark_notified(db, due)
    return 1
