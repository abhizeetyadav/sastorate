#!/usr/bin/env python3
"""Deploy Bhaau: the scraper runs on GitHub Actions, the website on Cloudflare Pages
(or GitHub Pages with --host github), and optionally the price-alert Worker on Cloudflare.

Run it from the project folder, with your virtual environment active:

    python scripts/deploy.py              # GitHub repo + Cloudflare Pages, first scrape, live-site check
    python scripts/deploy.py --alerts     # ...and deploy the price-alert service to Cloudflare
    python scripts/deploy.py --check      # only check the live site
    python scripts/deploy.py --dry-run    # show what would happen, change nothing

Safe to run again after any change: every step checks what is already done.
Secrets are read from .env and sent to GitHub / Cloudflare through stdin, never on the
command line, and the script refuses to commit any file that contains one of them.

Needs: git, the GitHub CLI (gh) and Node.js (npx, for Cloudflare).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
WORKFLOW = "scrape.yml"

# .env name -> how GitHub gets it
SECRETS = ["WEBSHARE_PROXY_USERNAME", "WEBSHARE_PROXY_PASSWORD", "WEBSHARE_API_KEY",
           "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID",
           "ALERTS_TOKEN", "RESEND_API_KEY", "OWNER_EMAIL", "STATUS_KEY"]
VARIABLES = ["HOSTING", "CF_PAGES_PROJECT", "WEBSHARE_MODE", "WEBSHARE_COUNTRIES", "SITE_URL", "CONTACT_EMAIL",
             "ALERTS_URL", "MAIL_FROM"]
NEVER_COMMIT = ["WEBSHARE_PROXY_PASSWORD", "WEBSHARE_API_KEY", "ALERTS_TOKEN", "RESEND_API_KEY", "STATUS_KEY",
                "CLOUDFLARE_API_TOKEN"]
CF_TOKEN_PAGE = "https://dash.cloudflare.com/profile/api-tokens"
MUST_IGNORE = [".env", ".venv/", "node_modules/", ".wrangler/"]
GITHUB_PAGES_IPS = ["185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153"]

DRY = False
ASSUME_YES = False


# ---------------------------------------------------------------- console
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class Stop(Exception):
    """A problem the person has to fix before deploying."""


def step(n: int | str, title: str) -> None:
    print(f"\n== {n}. {title}")


def ok(msg: str) -> None:
    print(f"   [ok] {msg}")


def info(msg: str) -> None:
    print(f"        {msg}")


def warn(msg: str) -> None:
    print(f"   [!]  {msg}")


def ask(prompt: str, default: str = "") -> str:
    if ASSUME_YES:
        return default
    got = input(f"   ?    {prompt}{f' [{default}]' if default else ''}: ").strip()
    return got or default


def confirm(prompt: str, default: bool = True) -> bool:
    if ASSUME_YES:
        return default
    got = input(f"   ?    {prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not got else got.startswith("y")


# ---------------------------------------------------------------- commands
def tool(name: str) -> str | None:
    return shutil.which(name)


def run(cmd: list[str], *, input: str | None = None, check: bool = True, capture: bool = True,
        change: bool = False, cwd: Path = ROOT, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command. `change=True` marks commands that change something (skipped in --dry-run)."""
    shown = " ".join(c if " " not in c else f'"{c}"' for c in [Path(cmd[0]).stem, *cmd[1:]])
    if change and DRY:
        info(f"(dry run) would run: {shown}" + ("   <- value sent privately" if input is not None else ""))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    try:
        p = subprocess.run(cmd, input=input, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                           capture_output=capture, env={**os.environ, **extra_env} if extra_env else None)
    except FileNotFoundError:
        raise Stop(f"Couldn't start {cmd[0]}. Is it installed and on PATH?")
    if check and p.returncode != 0:
        detail = ((p.stderr or "") + (p.stdout or "")).strip() if capture else ""
        raise Stop(f"This command failed: {shown}\n{detail[-1500:]}")
    return p


def git(*args: str, **kw) -> subprocess.CompletedProcess:
    return run([GIT, *args], **kw)


def gh(*args: str, **kw) -> subprocess.CompletedProcess:
    return run([GH, *args], **kw)


def gh_json(*args: str):
    p = gh(*args, check=False)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout or "null")
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------- .env
def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env(key: str, value: str) -> None:
    """Set KEY=value in .env, replacing an existing (or commented-out) line, else appending."""
    if DRY:
        info(f"(dry run) would save {key} to .env")
        return
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    live = re.compile(rf"^\s*{re.escape(key)}\s*=")
    commented = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
    idx = next((i for i, l in enumerate(lines) if live.match(l)), None)
    if idx is None:
        idx = next((i for i, l in enumerate(lines) if commented.match(l)), None)
    if idx is None:
        lines.append(f"{key}={value}")
    else:
        lines[idx] = f"{key}={value}"
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- file versions
MANIFEST = ROOT / "scripts" / "manifest.json"


def _file_hash(p: Path) -> str:
    import hashlib
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:16]  # same on Windows and Linux


def outdated_files() -> list[str]:
    """Code files that don't match the versions this update shipped with (older copy, or edited)."""
    if not MANIFEST.exists():
        return []
    expected = json.loads(MANIFEST.read_text(encoding="utf-8")).get("files", {})
    out = []
    for rel, h in expected.items():
        p = ROOT / rel
        if not p.exists():
            out.append(f"{rel} (missing)")
        elif _file_hash(p) != h:
            out.append(rel)
    return out


# ---------------------------------------------------------------- 1. preflight
def preflight(env: dict[str, str], alerts: bool, host: str) -> None:
    step(1, "Checking your computer and settings")
    if sys.version_info < (3, 10):
        raise Stop("Python 3.10 or newer is needed.")
    if not GIT:
        raise Stop("git isn't installed. Install it from https://git-scm.com/download/win, then open a new terminal.")
    if not GH:
        raise Stop("The GitHub CLI isn't installed. Run:  winget install --id GitHub.cli\n"
                   "then open a new terminal and run this script again.")
    ok("git and GitHub CLI found")
    if (alerts or host == "cloudflare") and not tool("npx"):
        raise Stop("Cloudflare needs Node.js. Run:  winget install OpenJS.NodeJS.LTS\n"
                   "then open a new terminal and run this script again.")

    if gh("auth", "status", check=False).returncode != 0:
        warn("You're not signed in to GitHub in this terminal.")
        if DRY:
            raise Stop("Run  gh auth login  first.")
        if not confirm("Sign in now (opens your browser)?"):
            raise Stop("Run  gh auth login  and try again.")
        run([GH, "auth", "login", "--web", "--git-protocol", "https"], capture=False)
    login = (gh("api", "user", "--jq", ".login").stdout or "").strip()
    ok(f"signed in to GitHub as {login}")

    if not ENV_FILE.exists():
        raise Stop("No .env file. Copy .env.example to .env and fill in your Webshare details first.")
    mode = env.get("WEBSHARE_MODE", "rotating") or "rotating"
    has_proxy = (env.get("WEBSHARE_PROXY_USERNAME") and env.get("WEBSHARE_PROXY_PASSWORD")) if mode == "rotating" \
        else env.get("WEBSHARE_API_KEY") if mode == "list" else True
    if not has_proxy:
        raise Stop(f"Webshare details are missing from .env for WEBSHARE_MODE={mode}.")
    ok(f"Webshare details found ({mode} mode)")

    if not env.get("STATUS_KEY"):
        env["STATUS_KEY"] = pysecrets.token_urlsafe(18)
        save_env("STATUS_KEY", env["STATUS_KEY"])
        ok("made a STATUS_KEY for your private status page (saved in .env)")
    if not env.get("CONTACT_EMAIL"):
        e = ask("Public contact email for the footer and privacy page (Enter to skip)")
        if e:
            env["CONTACT_EMAIL"] = e
            save_env("CONTACT_EMAIL", e)
    if not env.get("OWNER_EMAIL"):
        e = ask("Your email for 'a store broke' alerts (Enter to skip)")
        if e:
            env["OWNER_EMAIL"] = e
            save_env("OWNER_EMAIL", e)
    if env.get("OWNER_EMAIL") and not env.get("RESEND_API_KEY"):
        warn("OWNER_EMAIL is set but RESEND_API_KEY isn't, so problem emails won't be sent (the report still "
             "appears on each GitHub run). Add RESEND_API_KEY to .env to turn them on.")

    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "scraper/config/stores.yaml").read_text(encoding="utf-8"))
        enabled = [s["id"] for s in cfg["stores"] if s.get("enabled", True)]
    except Exception as e:  # noqa: BLE001
        raise Stop(f"scraper/config/stores.yaml has a problem: {e}")
    if not enabled:
        raise Stop("No store is enabled in stores.yaml.")
    ok(f"{len(enabled)} stores enabled: {', '.join(enabled)}")

    if (ROOT.parent / "scraper").is_dir() and (ROOT.parent / "scraper") != ROOT / "scraper":
        warn(f"There's another copy of the project in the folder above ({ROOT.parent}).")
        warn(f"Always unzip updates into THIS folder: {ROOT}")
    stale = outdated_files()
    if stale:
        warn(f"{len(stale)} file(s) don't match the latest update (an older copy, or edited by you):")
        for f in stale[:15]:
            info(f"   {f}")
        info("To fix: unzip the latest bhaau-full-*.zip into this folder (your settings and data aren't in it).")
    else:
        ok("all code files match the latest update")

    try:
        import pytest  # noqa: F401
        p = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], check=False)
        last = (p.stdout or "").strip().splitlines()[-1:] or [""]
        if p.returncode != 0:
            hint = ("\nLikely cause: the out-of-date files listed above. Unzip the latest bhaau-full-*.zip "
                    "into this folder and run again." if stale else "")
            raise Stop(f"Tests failed, so nothing was deployed:\n{p.stdout[-2000:]}{hint}")
        ok(f"tests pass ({last[0]})")
    except ImportError:
        warn("pytest isn't installed, so tests were skipped (pip install pytest to run them)")

    db = ROOT / "data" / "prices.db"
    if db.exists():
        mb = db.stat().st_size / 1e6
        (warn if mb > 50 else ok)(f"price database is {mb:.1f} MB" + (" - GitHub refuses files over 100 MB" if mb > 50 else ""))
    else:
        info("no local price database yet: the first GitHub run starts the price history")


# ---------------------------------------------------------------- 2. git
def ensure_gitignore() -> None:
    gi = ROOT / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    missing = [x for x in MUST_IGNORE if x not in text.split()]
    if missing and not DRY:
        gi.write_text(text.rstrip("\n") + "\n" + "\n".join(missing) + "\n", encoding="utf-8")
    if missing:
        ok(f"added {', '.join(missing)} to .gitignore")


def scan_for_secrets(env: dict[str, str]) -> None:
    if not (ROOT / ".git").exists():
        info("(dry run) secret check runs once the repository exists")
        return
    values = {k: env[k] for k in NEVER_COMMIT if len(env.get(k, "")) >= 6}
    files = (git("ls-files", "-co", "--exclude-standard").stdout or "").splitlines()
    leaks = []
    for f in files:
        p = ROOT / f
        if not p.is_file() or p.stat().st_size > 20e6:
            continue
        data = p.read_bytes()
        leaks += [f"{f} contains your {k}" for k, v in values.items() if v.encode() in data]
    if leaks:
        raise Stop("Refusing to commit secrets:\n   " + "\n   ".join(leaks) +
                   "\nRemove them from those files (keep secrets only in .env), then run again.")
    ok(f"no secrets in the {len(files)} files to be published")


def git_prepare(env: dict[str, str]) -> None:
    step(2, "Preparing the git repository")
    if not (ROOT / ".git").exists():
        git("init", "-b", "main", change=True)
        ok("created a git repository")
    ensure_gitignore()
    for path in (".env", ".venv/x"):
        if (ROOT / path.split("/")[0]).exists() and git("check-ignore", "-q", path, check=False).returncode != 0 and not DRY:
            raise Stop(f"{path.split('/')[0]} isn't ignored by git. Check .gitignore.")
    if not (git("config", "user.email", check=False).stdout or "").strip():
        u = gh_json("api", "user") or {}
        git("config", "user.name", u.get("name") or u.get("login", "bhaau"), change=True)
        git("config", "user.email", f"{u.get('id')}+{u.get('login')}@users.noreply.github.com", change=True)
        ok("set your git name and a private GitHub email for commits")
    scan_for_secrets(env)


def commit(message: str) -> bool:
    git("add", "-A", change=True)
    if DRY:
        return True
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git("commit", "-q", "-m", message, change=True)
    return True


# ---------------------------------------------------------------- 3. GitHub repo
def origin_repo() -> str | None:
    p = git("config", "--get", "remote.origin.url", check=False)   # as configured, before any URL rewriting
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", (p.stdout or "").strip()) if p.returncode == 0 else None
    return m.group(1) if m else None


def github_repo(args) -> tuple[str, bool]:
    """Returns (owner/name, is_new)."""
    step(3, "GitHub repository")
    repo = origin_repo()
    if repo:
        meta = gh_json("repo", "view", repo, "--json", "nameWithOwner,visibility,url")
        if not meta:
            raise Stop(f"git points to {repo} but GitHub can't find it. Check the 'origin' remote.")
        ok(f"using {meta['url']} ({meta['visibility'].lower()})")
        return meta["nameWithOwner"], False

    name = args.repo or ask("Name for the new GitHub repository", "bhaau")
    private = args.private
    if not args.private and not ASSUME_YES:
        info("Public is recommended: free GitHub Pages and unlimited Actions minutes. The code, store list and")
        info("price history become visible; your secrets and subscriber emails never do.")
        private = not confirm("Make the repository public?", True)
    if private:
        warn("Private repos need GitHub Pro for Pages and use ~1,500 of the 2,000 free Actions minutes a month.")
        if not confirm("Continue with a private repository?", False):
            raise Stop("Run again and choose public, or upgrade to GitHub Pro first.")
    gh("repo", "create", name, "--private" if private else "--public", "--source", str(ROOT),
       "--remote", "origin", "--description", "iPhone prices in Nepal, compared across stores", change=True)
    login = (gh("api", "user", "--jq", ".login").stdout or "").strip()
    ok(f"created github.com/{login}/{name}")
    return f"{login}/{name}", True


def take_live_data(repo: str, is_new: bool) -> None:
    """Once the site is live, GitHub's copy of the price data is the newest: use it locally too."""
    if is_new or DRY:
        return
    git("fetch", "-q", "origin", "main", check=False)
    if git("rev-parse", "--verify", "-q", "origin/main", check=False).returncode != 0:
        return
    db = ROOT / "data" / "prices.db"
    live = git("ls-tree", "--name-only", "origin/main", "data/prices.db", check=False).stdout.strip()
    if live and db.exists():
        backup = db.with_name(f"prices.{datetime.now():%Y%m%d-%H%M}.backup.db")
        shutil.copy2(db, backup)
        info(f"kept a copy of your local price database as data/{backup.name}")
    if live:
        git("checkout", "origin/main", "--", "data/prices.db", check=False)
        git("checkout", "origin/main", "--", "web/data", check=False)
        ok("took the latest live prices from GitHub")


def push(repo: str) -> None:
    for attempt in (1, 2):
        p = git("push", "-u", "origin", "HEAD:main", check=False, change=True)
        if p.returncode == 0:
            ok("pushed to GitHub")
            return
        if attempt == 1 and ("rejected" in (p.stderr or "") or "fetch first" in (p.stderr or "")):
            info("GitHub has newer commits (the price bot); combining them with yours...")
            r = git("pull", "--rebase", "-X", "theirs", "origin", "main", check=False)
            if r.returncode != 0:
                git("rebase", "--abort", check=False)
                raise Stop("Couldn't combine your changes with GitHub's. Run  git pull --rebase origin main,"
                           " fix any conflicts, then run this script again.")
            continue
        raise Stop(f"git push failed:\n{(p.stderr or '').strip()[-1500:]}")


# ---------------------------------------------------------------- 4. Pages
def cf_pages(repo: str, env: dict[str, str]) -> str:
    step(4, "Cloudflare Pages (hosting)")
    if not env.get("CLOUDFLARE_API_TOKEN") and DRY:
        info("(dry run) would ask for a Cloudflare API token, create the Pages project and set SITE_URL")
        return f"https://{env.get('CF_PAGES_PROJECT') or 'bhaau'}.pages.dev"
    if not env.get("CLOUDFLARE_API_TOKEN"):
        info("GitHub needs a Cloudflare API token to publish the site. Create one (2 minutes):")
        info(f"   1. Open {CF_TOKEN_PAGE} -> Create Token -> Custom token -> Get started")
        info("   2. Name it 'bhaau', Permissions: Account / Cloudflare Pages / Edit")
        info("   3. Continue to summary -> Create Token, and copy it")
        token = "" if ASSUME_YES else getpass.getpass("   ?    Paste the token here (hidden): ").strip()
        if not token:
            raise Stop("A Cloudflare API token is needed. Add CLOUDFLARE_API_TOKEN to .env and run again.")
        env["CLOUDFLARE_API_TOKEN"] = token
        save_env("CLOUDFLARE_API_TOKEN", token)
    cf = {"CLOUDFLARE_API_TOKEN": env["CLOUDFLARE_API_TOKEN"]}

    if not env.get("CLOUDFLARE_ACCOUNT_ID"):
        who = cfw("whoami", check=False, extra_env=cf)
        ids = list(dict.fromkeys(re.findall(r"\b[0-9a-f]{32}\b", (who.stdout or "") + (who.stderr or ""))))
        if not ids:
            raise Stop(f"Cloudflare didn't accept the token. Make a new one at {CF_TOKEN_PAGE}\n"
                       f"{((who.stdout or '') + (who.stderr or '')).strip()[-800:]}")
        acc = ids[0] if len(ids) == 1 else ask(f"Which Cloudflare account ID? ({', '.join(ids)})", ids[0])
        env["CLOUDFLARE_ACCOUNT_ID"] = acc
        save_env("CLOUDFLARE_ACCOUNT_ID", acc)
    cf["CLOUDFLARE_ACCOUNT_ID"] = env["CLOUDFLARE_ACCOUNT_ID"]
    ok("Cloudflare token works")

    name = env.get("CF_PAGES_PROJECT") or "bhaau"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,56}", name):
        raise Stop(f"CF_PAGES_PROJECT={name!r}: use lowercase letters, digits and dashes.")
    listed = cfw("pages", "project", "list", check=False, extra_env=cf)
    if listed.returncode != 0:
        raise Stop("The token can't manage Cloudflare Pages. Give it the permission Account / Cloudflare Pages / Edit.\n"
                   + ((listed.stdout or "") + (listed.stderr or "")).strip()[-800:])

    def domain_in(text: str) -> str | None:
        for line in text.splitlines():
            if re.search(rf"(^|[\s│|]){re.escape(name)}[\s│|]", line):
                m = re.search(r"([a-z0-9-]+\.pages\.dev)", line)
                if m:
                    return m.group(1)
        return None

    sub = domain_in(listed.stdout or "")
    if sub:
        ok(f"Pages project '{name}' already exists")
    else:
        made = cfw("pages", "project", "create", name, "--production-branch", "main", change=True, extra_env=cf)
        m = re.search(r"https://([a-z0-9-]+\.pages\.dev)", (made.stdout or "") + (made.stderr or ""))
        sub = m.group(1) if m else domain_in((cfw("pages", "project", "list", check=False, extra_env=cf).stdout or ""))
        sub = sub or f"{name}.pages.dev"
        ok(f"created Pages project '{name}'")
    if name != env.get("CF_PAGES_PROJECT"):
        env["CF_PAGES_PROJECT"] = name
        save_env("CF_PAGES_PROJECT", name)

    default_url = f"https://{sub}"
    site = (env.get("SITE_URL") or "").rstrip("/")
    host = urlparse(site).hostname or ""
    if site and (not site.startswith("https://") or host in ("localhost", "127.0.0.1") or host.endswith("github.io")):
        info(f"replacing SITE_URL={site} with the Cloudflare address")
        site, host = "", ""
    if site and not host.endswith(".pages.dev"):
        add_cf_domain(env, name, host, sub)
    else:
        site = default_url
        if env.get("SITE_URL") != site:
            env["SITE_URL"] = site
            save_env("SITE_URL", site)
        ok(f"site address: {site}/")

    # don't leave a second public copy on GitHub Pages (duplicate pages hurt Google ranking)
    if gh_json("api", f"repos/{repo}/pages") and confirm("GitHub Pages is also on for this repo. Turn it off?", True):
        gh("api", "-X", "DELETE", f"repos/{repo}/pages", change=True)
        ok("turned off GitHub Pages")
    return site


def add_cf_domain(env: dict[str, str], project: str, host: str, sub: str) -> None:
    import requests
    if DRY:
        info(f"(dry run) would add {host} to the Pages project")
        return
    url = (f"https://api.cloudflare.com/client/v4/accounts/{env['CLOUDFLARE_ACCOUNT_ID']}"
           f"/pages/projects/{project}/domains")
    r = requests.post(url, json={"name": host}, timeout=30,
                      headers={"Authorization": f"Bearer {env['CLOUDFLARE_API_TOKEN']}"})
    errors = " ".join(e.get("message", "") for e in (r.json().get("errors") or [])) if r.content else ""
    if r.ok or "already" in errors.lower():
        ok(f"custom domain {host} added to the Pages project")
    else:
        warn(f"couldn't add {host} automatically ({errors or r.status_code}). Add it in Cloudflare: "
             f"Workers & Pages -> {project} -> Custom domains.")
    info("DNS for your domain:")
    info(f"   - If the domain is on Cloudflare: add  CNAME {host} -> {sub}  (proxied). Cloudflare may do it for you.")
    info(f"   - If DNS is elsewhere: a subdomain (www.) can use  CNAME -> {sub}  at your provider;")
    info("     a bare domain (bhaau.com.np) needs its nameservers moved to Cloudflare first.")
    info("   HTTPS is set up automatically once DNS works (usually within an hour).")


def pages(repo: str, env: dict[str, str]) -> str:
    step(4, "GitHub Pages (hosting)")
    info_ = gh_json("api", f"repos/{repo}/pages")
    if info_ is None:
        gh("api", "-X", "POST", f"repos/{repo}/pages", "-f", "build_type=workflow", change=True)
        ok("turned on GitHub Pages (deployed by the workflow)")
        info_ = gh_json("api", f"repos/{repo}/pages") or {}
    elif info_.get("build_type") != "workflow":
        gh("api", "-X", "PUT", f"repos/{repo}/pages", "-f", "build_type=workflow", change=True)
        ok("switched GitHub Pages to deploy from the workflow")
    else:
        ok("GitHub Pages already on")

    default_url = (info_.get("html_url") or f"https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/").rstrip("/")
    site = (env.get("SITE_URL") or "").rstrip("/")
    host = urlparse(site).hostname or ""
    if site and (not site.startswith("https://") or host in ("localhost", "127.0.0.1")):
        info(f"ignoring SITE_URL={site} from .env (local address)")
        site, host = "", ""
    if site and not host.endswith("github.io"):
        if (info_.get("cname") or "") != host:
            gh("api", "-X", "PUT", f"repos/{repo}/pages", "-f", f"cname={host}", change=True)
            ok(f"custom domain set to {host}")
        print_dns(host, repo.split("/")[0].lower())
    else:
        if site != default_url:
            site = default_url
            env["SITE_URL"] = site
            save_env("SITE_URL", site)
        ok(f"site address: {site}/")
    return site


def print_dns(host: str, owner: str) -> None:
    info(f"At your domain provider, point {host} to GitHub Pages:")
    if host.startswith("www."):
        info(f"   CNAME  www  ->  {owner}.github.io")
    else:
        info(f"   A      {host}  ->  " + ", ".join(GITHUB_PAGES_IPS) + "   (all four)")
        info(f"   CNAME  www.{host}  ->  {owner}.github.io")
    info("Then on GitHub: Settings -> Pages -> tick 'Enforce HTTPS' once the certificate is ready (can take a day).")


# ---------------------------------------------------------------- 5. Cloudflare alerts (optional)
def wrangler(*args: str, **kw) -> subprocess.CompletedProcess:
    """wrangler for the alerts Worker (uses alerts/wrangler.toml)."""
    return run([NPX, "--yes", "wrangler", *args], cwd=ROOT / "alerts", **kw)


def cfw(*args: str, **kw) -> subprocess.CompletedProcess:
    """wrangler for Cloudflare Pages, run from the project root (no Worker config there)."""
    return run([NPX, "--yes", "wrangler@4", *args], cwd=ROOT, **kw)


def set_toml_var(text: str, key: str, value: str) -> str:
    line = f'{key} = "{value}"'
    pat = re.compile(rf'^{key}\s*=\s*"[^"]*"(.*)$', re.M)
    if pat.search(text):
        return pat.sub(lambda m: line + m.group(1), text, count=1)
    return text.replace("[vars]\n", f"[vars]\n{line}\n", 1)


def deploy_alerts(env: dict[str, str], site: str) -> bool:
    """Returns True if alerts/wrangler.toml changed (needs committing)."""
    step(5, "Price-alert service (Cloudflare)")
    who = wrangler("whoami", check=False)
    if who.returncode != 0 or "not authenticated" in (who.stdout + who.stderr).lower():
        if DRY:
            raise Stop("Run  cd alerts  then  npx wrangler login  first.")
        info("Signing in to Cloudflare (opens your browser)...")
        wrangler("login", capture=False)
    ok("signed in to Cloudflare")

    if not env.get("RESEND_API_KEY"):
        key = "" if ASSUME_YES else getpass.getpass("   ?    Resend API key (from resend.com -> API Keys; hidden): ").strip()
        if not key:
            raise Stop("Alerts need a Resend API key. Add RESEND_API_KEY to .env and run again.")
        env["RESEND_API_KEY"] = key
        save_env("RESEND_API_KEY", key)
    if not env.get("ALERTS_TOKEN"):
        env["ALERTS_TOKEN"] = pysecrets.token_urlsafe(32)
        save_env("ALERTS_TOKEN", env["ALERTS_TOKEN"])
        ok("made an ALERTS_TOKEN (saved in .env)")
    mail_from = env.get("MAIL_FROM") or ""
    if not mail_from:
        warn("MAIL_FROM isn't set, so alerts use Resend's test sender, which only delivers to YOUR Resend")
        warn("account email. Verify your domain in Resend, set MAIL_FROM in .env, and run this again.")
        mail_from = "Bhaau <onboarding@resend.dev>"

    toml_path = ROOT / "alerts" / "wrangler.toml"
    before = toml_path.read_text(encoding="utf-8")
    text = before
    if "PUT-YOUR-DATABASE-ID-HERE" in text:
        made = wrangler("d1", "create", "bhaau-alerts", check=False, change=True)
        out = (made.stdout or "") + (made.stderr or "")
        m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", out)
        if not m and not DRY:
            listed = wrangler("d1", "list", "--json", check=False)
            try:
                m_id = next(d["uuid"] for d in json.loads(listed.stdout or "[]") if d.get("name") == "bhaau-alerts")
            except (StopIteration, json.JSONDecodeError, KeyError):
                raise Stop(f"Couldn't create the alerts database:\n{out[-1000:]}")
        else:
            m_id = m.group(0) if m else "DRY-RUN-ID"
        text = text.replace("PUT-YOUR-DATABASE-ID-HERE", m_id)
        ok("created the alerts database")
    origin = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
    text = set_toml_var(text, "SITE_URL", site)
    text = set_toml_var(text, "SITE_ORIGINS", f"{origin},http://localhost:8000")
    text = set_toml_var(text, "MAIL_FROM", mail_from)
    changed = text != before
    if changed and not DRY:
        toml_path.write_text(text, encoding="utf-8")

    # captured output = non-interactive, so wrangler doesn't stop to ask; the schema is safe to re-run
    wrangler("d1", "execute", "bhaau-alerts", "--remote", "--file=schema.sql", change=True)
    ok("database tables ready")
    out = wrangler("deploy", change=True)
    m = re.search(r"https://[\w.-]+\.workers\.dev", (out.stdout or "") + (out.stderr or ""))
    url = m.group(0) if m else env.get("ALERTS_URL", "")
    if not url and not DRY:
        raise Stop("Deployed, but couldn't find the Worker's address in the output. Add it to .env as ALERTS_URL.")
    if url and url != env.get("ALERTS_URL"):
        env["ALERTS_URL"] = url
        save_env("ALERTS_URL", url)
    for name in ("RESEND_API_KEY", "ALERTS_TOKEN"):   # after deploy, so the Worker exists
        wrangler("secret", "put", name, input=env[name], change=True)
    ok("secrets stored in Cloudflare")
    ok(f"alert service live at {url or '(dry run)'}")
    return changed


# ---------------------------------------------------------------- 6. secrets + variables
def configure(repo: str, env: dict[str, str]) -> None:
    step(6, "Secrets and settings on GitHub")
    done, skipped = [], []
    for k in SECRETS:
        if env.get(k):
            gh("secret", "set", k, "--repo", repo, input=env[k], change=True)
            done.append(k)
        else:
            skipped.append(k)
    ok(f"secrets: {', '.join(done)}")
    vdone = []
    for k in VARIABLES:
        if env.get(k):
            gh("variable", "set", k, "--repo", repo, "--body", env[k], change=True)
            vdone.append(k)
        else:
            skipped.append(k)
    ok(f"variables: {', '.join(vdone)}")
    if skipped:
        info(f"not set (optional): {', '.join(skipped)}")


# ---------------------------------------------------------------- 7. first run
def first_run(repo: str) -> bool:
    step(7, "First scrape and deploy on GitHub")
    def recent():
        return gh_json("run", "list", "--repo", repo, "--workflow", WORKFLOW, "--event", "workflow_dispatch",
                       "--limit", "10", "--json", "databaseId,url") or []
    before = {r["databaseId"] for r in recent()}
    for attempt in range(6):
        p = gh("workflow", "run", WORKFLOW, "--repo", repo, "--ref", "main", check=False, change=True)
        if p.returncode == 0:
            break
        time.sleep(5)  # a brand-new repo needs a moment before its workflow can be started
    else:
        raise Stop(f"Couldn't start the workflow: {(p.stderr or '').strip()}")
    if DRY:
        return True
    info("Started. This takes about 10-20 minutes (installing the browser, then every store).")
    run_id = None
    for _ in range(30):
        new = [r for r in recent() if r["databaseId"] not in before]
        if new:
            run_id, url = new[0]["databaseId"], new[0]["url"]
            break
        time.sleep(4)
    if not run_id:
        warn("Started, but couldn't find the run. Watch it in the Actions tab on GitHub.")
        return False
    info(f"Watching {url}  (Ctrl+C stops watching; the run carries on)")
    try:
        p = run([GH, "run", "watch", str(run_id), "--repo", repo, "--exit-status", "--interval", "20"],
                capture=False, check=False)
    except KeyboardInterrupt:
        info("Stopped watching. Check the result later with:  python scripts/deploy.py --check")
        return False
    if p.returncode == 0:
        ok("run finished: prices collected and site deployed")
        return True
    warn("The run finished with a problem. If only a store failed, the site still deployed with the others.")
    info(f"See what failed:  gh run view {run_id} --repo {repo} --log-failed")
    return False


# ---------------------------------------------------------------- 8. live check
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def live_check(site: str, env: dict[str, str], wait: bool = True) -> bool:
    step(8, f"Checking the live site: {site}/")
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = "bhaau-deploy-check"

    def get(path: str, tries: int = 1):
        for i in range(tries):
            try:
                r = s.get(f"{site}/{path}", timeout=20)
                if r.status_code == 200:
                    return r
            except requests.RequestException:
                r = None
            if i < tries - 1:
                time.sleep(15)
        return r

    results: list[tuple[bool, str]] = []
    home = get("", tries=12 if wait else 1)  # a fresh Pages site can take a few minutes to appear
    if not (home is not None and home.status_code == 200 and "Bhaau" in home.text):
        warn(f"the site isn't reachable at {site}/ yet. A brand-new site can take a few minutes to appear.")
        info("Check that the run's 'Publish' job succeeded on GitHub, then run:  python scripts/deploy.py --check")
        return False
    results.append((True, "home page loads"))
    latest = get("data/latest.json")
    data = None
    if latest is not None and latest.status_code == 200:
        try:
            data = latest.json()
        except ValueError:
            pass
    if data:
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(data["generated_at"])).total_seconds() / 3600
        live = [x["name"] for x in data["stores"] if x.get("offers")]
        broken = [x["name"] for x in data["stores"] if x.get("enabled") and not x.get("offers")]
        results.append((not data.get("demo"), "real prices (not the sample data)"))
        results.append((age_h < 8, f"prices updated {age_h:.1f} hours ago"))
        results.append((len(live) > 0, f"{len(data['variants'])} phone prices from {len(live)} stores"
                                        + (f"; no prices from {', '.join(broken)}" if broken else "")))
        if data["variants"]:
            slug = _slug(data["variants"][0]["model"])
            r = get(f"{slug}/")
            results.append((bool(r and r.status_code == 200), f"model page /{slug}/ loads"))
        if data.get("alerts_url") and env.get("ALERTS_TOKEN"):
            try:
                r = s.get(f"{data['alerts_url']}/stats", headers={"Authorization": f"Bearer {env['ALERTS_TOKEN']}"}, timeout=20)
                results.append((r.status_code == 200, "price-alert service answers" if r.status_code == 200
                                else f"price-alert service returned {r.status_code} (check ALERTS_TOKEN matches)"))
            except requests.RequestException:
                results.append((False, "price-alert service unreachable"))
    else:
        results.append((False, "data/latest.json missing: the first scrape hasn't finished"))
    r = get("privacy/")
    results.append((bool(r and r.status_code == 200), "privacy page loads"))
    try:
        r = s.get(f"{site}/no-such-page-{pysecrets.token_hex(4)}/", timeout=20)
        results.append((r.status_code == 404, "unknown addresses get a proper 404" if r.status_code == 404
                        else f"unknown addresses return {r.status_code} instead of 404 (is web/404.html deployed?)"))
    except requests.RequestException:
        pass
    r = get("sitemap.xml")
    results.append((bool(r and r.status_code == 200), "sitemap.xml loads"))
    if env.get("STATUS_KEY"):
        r = get(f"status-{env['STATUS_KEY']}/")
        results.append((bool(r and r.status_code == 200), "private status page loads"))
    if env.get("CONTACT_EMAIL") and home is not None:
        results.append((f"mailto:{env['CONTACT_EMAIL']}" in home.text, "contact links in the footer"))

    for good, label in results:
        ok(label) if good else warn(f"{label}: FAILED" if label.endswith(("loads", "footer")) else label)
    return all(g for g, _ in results)


# ---------------------------------------------------------------- main
def main() -> int:
    global DRY, ASSUME_YES, GIT, GH, NPX
    ap = argparse.ArgumentParser(description="Deploy Bhaau to GitHub Pages (and optionally Cloudflare).")
    ap.add_argument("--alerts", action="store_true", help="also deploy the price-alert Worker to Cloudflare")
    ap.add_argument("--check", action="store_true", help="only check the live site")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen without changing anything")
    ap.add_argument("--yes", action="store_true", help="accept the defaults instead of asking")
    ap.add_argument("--repo", help="name for a new GitHub repository (default: bhaau)")
    ap.add_argument("--private", action="store_true", help="make a new repository private (needs GitHub Pro for Pages)")
    ap.add_argument("--no-run", action="store_true", help="don't start a scrape now; wait for the schedule")
    ap.add_argument("--host", choices=["cloudflare", "github"],
                    help="where the website is hosted (default: HOSTING in .env, else cloudflare)")
    args = ap.parse_args()
    DRY, ASSUME_YES = args.dry_run, args.yes
    GIT, GH, NPX = tool("git"), tool("gh"), tool("npx")
    os.chdir(ROOT)
    env = read_env()

    try:
        if args.check:
            site = (env.get("SITE_URL") or "").rstrip("/")
            if not site:
                raise Stop("SITE_URL isn't in .env yet. Deploy first.")
            return 0 if live_check(site, env, wait=False) else 1

        print("Deploying Bhaau" + ("  (dry run: nothing will be changed)" if DRY else ""))
        host = args.host or env.get("HOSTING") or "cloudflare"
        if host not in ("cloudflare", "github"):
            raise Stop(f"HOSTING={host!r} in .env: use cloudflare or github.")
        if env.get("HOSTING") != host:
            env["HOSTING"] = host
            save_env("HOSTING", host)
        preflight(env, args.alerts, host)
        git_prepare(env)
        repo, is_new = github_repo(args)
        take_live_data(repo, is_new)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        # [skip ci]: don't start a deploy for this push; the script starts one itself once settings are in place
        if commit(f"Deploy {stamp} [skip ci]"):
            ok("committed your changes")
        push(repo)
        site = cf_pages(repo, env) if host == "cloudflare" else pages(repo, env)
        if args.alerts:
            if deploy_alerts(env, site) and commit(f"Alerts config {stamp} [skip ci]"):
                push(repo)
        elif env.get("ALERTS_URL"):
            info("price alerts: using ALERTS_URL from .env (run with --alerts to redeploy the Worker)")
        configure(repo, env)
        finished = True if args.no_run else first_run(repo)
        if not DRY and not args.no_run:
            live_check(site, env, wait=finished)
    except Stop as e:
        print(f"\n   [x]  {e}\n\nNothing after this step was changed. Fix it and run the script again.")
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130

    if DRY:
        print("\nDry run finished. Nothing was changed. Run without --dry-run to deploy.")
        return 0
    print(f"""
Done. Your site: {site}/
   Private status page: {site}/status-{env.get('STATUS_KEY', '<STATUS_KEY>')}/   (bookmark it)
   Prices refresh every 6 hours. GitHub emails you if a run fails{', and so does the health check' if env.get('RESEND_API_KEY') and env.get('OWNER_EMAIL') else ''}.
   Next: add the site to Google Search Console and submit {site}/sitemap.xml
   Re-deploy after any change:  python scripts/deploy.py      Check the live site:  python scripts/deploy.py --check""")
    return 0


GIT = GH = NPX = None

if __name__ == "__main__":
    sys.exit(main())
