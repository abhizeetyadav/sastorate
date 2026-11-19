"""Webshare.io proxy support.

Three modes (set WEBSHARE_MODE in .env):

  rotating  - Use Webshare's rotating endpoint (p.webshare.io:80). Every request
              exits from a different IP. Needs WEBSHARE_PROXY_USERNAME and
              WEBSHARE_PROXY_PASSWORD (the "-rotate" username from your
              dashboard -> Proxy List -> Rotating Proxy Endpoint).
  list      - Pull your proxy list from the Webshare API with WEBSHARE_API_KEY
              and rotate through it ourselves, retiring proxies that fail.
  none      - Direct connection (handy for local debugging).

Username/password auth is used everywhere (not IP authorization) so it also
works from GitHub Actions, whose IP changes on every run.
"""
from __future__ import annotations

import logging
import os
import random
import threading
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

WEBSHARE_API = "https://proxy.webshare.io/api/v2"
ROTATING_HOST = "p.webshare.io"
ROTATING_PORT = 80


@dataclass
class Proxy:
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    country: str | None = None
    failures: int = 0

    @property
    def url(self) -> str:
        if self.username:
            return f"http://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"http://{self.host}:{self.port}"

    def for_requests(self) -> dict[str, str]:
        return {"http": self.url, "https": self.url}

    def for_playwright(self) -> dict[str, str]:
        cfg = {"server": f"http://{self.host}:{self.port}"}
        if self.username:
            cfg.update(username=self.username, password=self.password or "")
        return cfg

    def __str__(self) -> str:  # never log credentials
        return f"{self.host}:{self.port}" + (f" [{self.country}]" if self.country else "")


@dataclass
class ProxyPool:
    mode: str = "none"
    proxies: list[Proxy] = field(default_factory=list)
    max_failures: int = 3
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _i: int = 0

    # ------------------------------------------------------------------ setup
    @classmethod
    def from_env(cls) -> "ProxyPool":
        mode = os.getenv("WEBSHARE_MODE", "none").strip().lower()
        if mode == "rotating":
            user = os.getenv("WEBSHARE_PROXY_USERNAME")
            pwd = os.getenv("WEBSHARE_PROXY_PASSWORD")
            if not (user and pwd):
                raise SystemExit("WEBSHARE_MODE=rotating needs WEBSHARE_PROXY_USERNAME and WEBSHARE_PROXY_PASSWORD")
            host = os.getenv("WEBSHARE_PROXY_HOST", ROTATING_HOST)
            port = int(os.getenv("WEBSHARE_PROXY_PORT", ROTATING_PORT))
            log.info("Proxy mode: Webshare rotating endpoint %s:%s", host, port)
            # A single endpoint; Webshare rotates the exit IP per connection.
            return cls(mode="rotating", proxies=[Proxy(host, port, user, pwd)], max_failures=10**9)

        if mode == "list":
            key = os.getenv("WEBSHARE_API_KEY")
            if not key:
                raise SystemExit("WEBSHARE_MODE=list needs WEBSHARE_API_KEY")
            countries = [c.strip().upper() for c in os.getenv("WEBSHARE_COUNTRIES", "").split(",") if c.strip()]
            proxies = fetch_webshare_list(key, countries)
            if not proxies:
                raise SystemExit("Webshare API returned no usable proxies (check plan / country filter)")
            random.shuffle(proxies)
            log.info("Proxy mode: Webshare list (%d proxies)", len(proxies))
            return cls(mode="list", proxies=proxies)

        log.info("Proxy mode: none (direct connection)")
        return cls(mode="none")

    # ------------------------------------------------------------------ use
    @property
    def enabled(self) -> bool:
        return self.mode != "none" and bool(self.proxies)

    def next(self) -> Proxy | None:
        if not self.enabled:
            return None
        with self._lock:
            alive = [p for p in self.proxies if p.failures < self.max_failures]
            if not alive:  # everything burned: reset rather than stall the run
                log.warning("All proxies marked bad - resetting failure counters")
                for p in self.proxies:
                    p.failures = 0
                alive = self.proxies
            self._i = (self._i + 1) % len(alive)
            return alive[self._i]

    def report_failure(self, proxy: Proxy | None) -> None:
        if proxy is not None:
            with self._lock:
                proxy.failures += 1

    def report_success(self, proxy: Proxy | None) -> None:
        if proxy is not None:
            with self._lock:
                proxy.failures = 0


def fetch_webshare_list(api_key: str, countries: list[str] | None = None) -> list[Proxy]:
    """Download every proxy on the account via the v2 list endpoint (paginated)."""
    headers = {"Authorization": f"Token {api_key}"}
    url = f"{WEBSHARE_API}/proxy/list/"
    params = {"mode": "direct", "page": 1, "page_size": 100}
    if countries:
        params["country_code__in"] = ",".join(countries)
    out: list[Proxy] = []
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for item in data.get("results", []):
            if not item.get("valid", True):
                continue
            out.append(Proxy(
                host=item["proxy_address"],
                port=int(item["port"]),
                username=item.get("username"),
                password=item.get("password"),
                country=item.get("country_code"),
            ))
        url, params = data.get("next"), None  # `next` already carries the query string
    return out


def check_pool(pool: ProxyPool, n: int = 3) -> None:
    """Print the exit IP for a few proxies - quick sanity check for credentials."""
    if not pool.enabled:
        print("Proxies disabled (WEBSHARE_MODE=none).")
        return
    for _ in range(n):
        p = pool.next()
        try:
            r = requests.get("https://ipv4.webshare.io/", proxies=p.for_requests(), timeout=20)
            print(f"OK   via {p} -> exit IP {r.text.strip()}")
        except Exception as e:  # noqa: BLE001
            print(f"FAIL via {p}: {e}")
