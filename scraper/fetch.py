"""HTTP fetching: proxy rotation, retries, polite delays, robots.txt and an
optional headless-browser fallback for JS-rendered or Cloudflare-protected pages.
"""
from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from .proxies import Proxy, ProxyPool

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
]

BLOCK_MARKERS = ("cf-chl", "challenge-platform", "Just a moment...", "Attention Required!", "captcha")
RETRY_STATUS = {403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, pool: ProxyPool, *, timeout: int = 30, retries: int = 4,
                 delay: tuple[float, float] = (2.0, 5.0), respect_robots: bool = True):
        self.pool = pool
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.respect_robots = respect_robots
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._browser = None  # lazily created BrowserFetcher

    # ---------------------------------------------------------------- public
    def get(self, url: str, *, render_js: bool = False, wait_selector: str | None = None) -> str:
        if self.respect_robots and not self.allowed(url):
            raise FetchError(f"robots.txt disallows {url}")
        self._throttle(url)
        if render_js:
            return self._browser_get(url, wait_selector)
        try:
            return self._requests_get(url)
        except FetchError as e:
            if "blocked" in str(e):
                log.info("Falling back to headless browser for %s", url)
                return self._browser_get(url, wait_selector)
            raise

    def close(self) -> None:
        if self._browser:
            self._browser.close()

    # ---------------------------------------------------------------- robots
    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                txt = self._requests_get(base + "/robots.txt", quiet=True)
                rp.parse(txt.splitlines())
            except Exception:  # noqa: BLE001 - no robots.txt means allowed
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch("*", url)

    # ---------------------------------------------------------------- internals
    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc
        wait = random.uniform(*self.delay) - (time.monotonic() - self._last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ne;q=0.8",
            "Cache-Control": "no-cache",
        }

    def _requests_get(self, url: str, quiet: bool = False) -> str:
        last_err: Exception | None = None
        tries = 1 if quiet else self.retries
        for attempt in range(1, tries + 1):
            proxy: Proxy | None = self.pool.next()
            try:
                r = requests.get(url, headers=self._headers(), timeout=self.timeout,
                                 proxies=proxy.for_requests() if proxy else None)
                if r.status_code == 404:
                    raise FetchError(f"404 {url}")
                if r.status_code in RETRY_STATUS:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                text = r.text
                if any(m in text[:5000] for m in BLOCK_MARKERS) and len(text) < 30000:
                    raise requests.HTTPError("blocked by bot protection")
                self.pool.report_success(proxy)
                return text
            except FetchError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.pool.report_failure(proxy)
                if not quiet:
                    log.warning("  attempt %d/%d failed for %s via %s: %s", attempt, tries, url, proxy or "direct", e)
                time.sleep(min(2 ** attempt, 20) + random.random())
        msg = "blocked" if last_err and "blocked" in str(last_err) else "failed"
        raise FetchError(f"{msg}: {url} ({last_err})")

    def _browser_get(self, url: str, wait_selector: str | None) -> str:
        if self._browser is None:
            self._browser = BrowserFetcher(self.pool)
        return self._browser.get(url, wait_selector, self.timeout)


def with_captured(html: str, extra: str) -> str:
    """Put the captured JSON inside <body>. Text after </html> is dropped by some HTML parser versions."""
    if not extra:
        return html
    for tag in ("</body>", "</html>"):
        i = html.lower().rfind(tag)
        if i >= 0:
            return html[:i] + extra + html[i:]
    return html + extra


def _captured_json(responses, max_bytes: int = 3_000_000, max_items: int = 40) -> str:
    """Product data the page downloaded, appended as <script type="application/json">
    blocks: extract.from_embedded_json already reads those."""
    out, total = [], 0
    for r in responses[:max_items]:
        try:
            body = r.text()
        except Exception:  # noqa: BLE001 - body gone (redirect, aborted request)
            continue
        if not body or len(body) > max_bytes or total > max_bytes * 3 or not re.search(r'"[^"]*price', body, re.I):
            continue
        total += len(body)
        safe_url = r.url.replace('"', "%22").replace("<", "%3C")
        safe_body = body.replace("</", "<" + "\\/")  # "</" would end the script early; "<\\/" is the same JSON
        out.append(f'<script type="application/json" data-captured-from="{safe_url}">{safe_body}</script>')
    if out:
        log.debug("  kept %d JSON responses from the page", len(out))
    return "\n".join(out)


class BrowserFetcher:
    """Playwright Chromium, routed through the same Webshare proxy."""

    def __init__(self, pool: ProxyPool):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise FetchError("Playwright not installed: pip install playwright && playwright install chromium") from e
        self.pool = pool
        self._pw = sync_playwright().start()
        proxy = pool.next()
        self._browser = self._pw.chromium.launch(
            headless=True, proxy=proxy.for_playwright() if proxy else None)

    def get(self, url: str, wait_selector: str | None, timeout: int) -> str:
        ctx = self._browser.new_context(user_agent=random.choice(USER_AGENTS[:4]),
                                        locale="en-US", viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        # JavaScript shops (Next.js, React...) download product data, often with every
        # storage size and its price, while only showing the default size on screen.
        # Keep those downloads so the normal extractors can read them.
        responses = []
        page.on("response", lambda r: responses.append(r)
                if r.request.resource_type in ("xhr", "fetch") and "json" in (r.headers.get("content-type") or "")
                else None)
        try:
            page.goto(url, timeout=timeout * 1000, wait_until="load")
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=timeout * 1000)
            else:
                # Many shops never go quiet (chat widgets, analytics, polling), so
                # waiting for "no network activity" is only a best effort.
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout, 10) * 1000)
                except Exception:  # noqa: BLE001 - page is loaded, just still busy
                    log.debug("  %s keeps loading in the background; reading it anyway", url)
                page.wait_for_timeout(1500)  # let the product data render
            # trigger lazy-loaded product grids
            for _ in range(4):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(600)
            html = page.content()
            return with_captured(html, _captured_json(responses))
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"browser failed: {url} ({e})") from e
        finally:
            ctx.close()

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()
