"""scripts/deploy.py: publishing while the price bot keeps committing to GitHub.

Reproduces a real failure: the bot deleted a model page that this computer had rebuilt,
both sides changed web/index.html and the price database, and an earlier attempt left an
unpushed commit. Publishing must send only this computer's real changes, without conflicts.
"""
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _write(base: Path, files: dict):
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())


@pytest.fixture()
def repos(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t"); monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@x")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t"); monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@x")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    me, bot = tmp_path / "me", tmp_path / "bot"
    _git(tmp_path, "clone", "-q", str(remote), str(me))
    _git(me, "checkout", "-q", "-b", "main")
    _write(me, {"scraper/app.py": "v1\n", "README.md": "readme v1\n", "web/index.html": "<html>shell v1</html>\n",
                "web/iphone-18/index.html": "page 18 v1\n", "web/iphone-18/.bhaau-page": "x\n",
                "web/iphone-17/index.html": "page 17 v1\n", "web/iphone-17/.bhaau-page": "x\n",
                "web/data/latest.json": '{"v": 1}\n', "data/prices.db": b"DB-v1", "web/logos/evo.png": b"PNG1"})
    _git(me, "add", "-A"); _git(me, "commit", "-qm", "start"); _git(me, "push", "-q", "-u", "origin", "main")
    _git(tmp_path, "clone", "-q", str(remote), str(bot))

    spec = importlib.util.spec_from_file_location("deploy_under_test", ROOT / "scripts" / "deploy.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    monkeypatch.setattr(d, "ROOT", me)
    monkeypatch.setattr(d, "GIT", shutil.which("git"))
    monkeypatch.setattr(d, "DRY", False)
    return d, me, bot


def _bot_run(bot, n):
    shutil.rmtree(bot / "web" / "iphone-18", ignore_errors=True)            # model no longer sold
    _write(bot, {"data/prices.db": f"DB-bot{n}".encode(), "web/iphone-17/index.html": f"page 17 bot{n}\n",
                 "web/index.html": f"<html>shell v1 prerendered bot{n}</html>\n", "web/sitemap.xml": "<urlset/>\n",
                 "web/data/latest.json": f'{{"v": "bot{n}"}}\n'})
    _git(bot, "add", "-A"); _git(bot, "commit", "-qm", f"data: prices {n}"); _git(bot, "push", "-q", "origin", "main")


def _show(repo, path):
    return subprocess.run(["git", "show", f"main:{path}"], cwd=repo, capture_output=True).stdout


def test_publish_after_the_bot_changed_generated_files(repos):
    d, me, bot = repos
    _bot_run(bot, 1)
    _write(bot, {"README.md": "readme edited on github\n"}); _git(bot, "commit", "-qam", "readme"); _git(bot, "push", "-q")
    # this computer: real changes, plus locally rebuilt pages, and an unpushed commit from a failed attempt
    _write(me, {"scraper/app.py": "v2 from this computer\n", "web/iphone-18/index.html": "page 18 rebuilt here\n",
                "web/iphone-17/index.html": "page 17 rebuilt here\n", "web/index.html": "<html>shell v2 (new template)</html>\n",
                "data/prices.db": b"DB-local", "web/data/latest.json": '{"v": "local"}\n'})
    _git(me, "add", "-A"); _git(me, "commit", "-qm", "Deploy (failed attempt) [skip ci]")
    _write(me, {"scraper/config/shop_info.yaml": "shops: {}\n", "web/logos/evo.png": b"PNG2"})

    d.sync_with_github("me/repo", is_new=False)
    msg = "Deploy test [skip ci]"
    assert d.commit(msg)
    d.push("me/repo", False, msg)

    remote = me.parent / "remote.git"
    assert _show(remote, "scraper/app.py") == b"v2 from this computer\n"            # your change
    assert _show(remote, "scraper/config/shop_info.yaml") == b"shops: {}\n"         # your new file
    assert _show(remote, "web/logos/evo.png") == b"PNG2"                            # your logo
    assert _show(remote, "web/index.html") == b"<html>shell v2 (new template)</html>\n"   # your template wins
    assert _show(remote, "data/prices.db") == b"DB-bot1"                            # live prices kept
    assert _show(remote, "web/iphone-17/index.html") == b"page 17 bot1\n"           # bot's pages kept
    assert _show(remote, "web/iphone-18/index.html") == b""                         # deleted page stays deleted
    assert _show(remote, "README.md") == b"readme edited on github\n"               # GitHub-only edit kept
    assert not (me / "web" / "iphone-18").exists()
    assert list((me / "data").glob("prices.*.backup.db"))                           # local DB backed up
    log = _git(me, "log", "--format=%s", "-3")
    assert log.splitlines()[:2] == ["Deploy test [skip ci]", "readme"]             # on top of GitHub, no merge


def test_the_bot_pushing_during_publish_is_caught_up(repos):
    d, me, bot = repos
    _write(me, {"scraper/app.py": "v2\n"})
    d.sync_with_github("me/repo", is_new=False)
    msg = "Deploy race [skip ci]"
    d.commit(msg)
    _bot_run(bot, 2)                      # the bot pushes between our commit and our push
    d.push("me/repo", False, msg)
    remote = me.parent / "remote.git"
    assert _show(remote, "scraper/app.py") == b"v2\n" and _show(remote, "data/prices.db") == b"DB-bot2"


def test_the_same_file_changed_on_both_sides_stops_safely(repos):
    d, me, bot = repos
    _write(bot, {"scraper/app.py": "edited on github\n"}); _git(bot, "commit", "-qam", "x"); _git(bot, "push", "-q")
    _write(me, {"scraper/app.py": "edited here\n"})
    with pytest.raises(d.Stop, match="scraper/app.py"):
        d.sync_with_github("me/repo", is_new=False)
    assert (me / "scraper" / "app.py").read_text() == "edited here\n"              # nothing lost
