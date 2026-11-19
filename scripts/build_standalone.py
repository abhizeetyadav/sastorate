"""Bundle web/ (HTML + CSS + JS + current data) into one self-contained HTML file.
Handy for sharing a snapshot or hosting somewhere without a data folder.

    python scripts/build_standalone.py            -> dist/bhaau.html
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def main():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    css = (WEB / "styles.css").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    latest = json.loads((WEB / "data" / "latest.json").read_text(encoding="utf-8"))
    hist = json.loads((WEB / "data" / "history.json").read_text(encoding="utf-8"))
    def inline(obj):  # "</" inside JSON strings would end the <script> early
        return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")

    data = f"<script>window.__LATEST__={inline(latest)};window.__HISTORY__={inline(hist)};</script>"
    html = html.replace('<link rel="stylesheet" href="styles.css">', f"<style>\n{css}\n</style>")
    html = html.replace('<script src="app.js"></script>', f"{data}\n<script>\n{js}\n</script>")
    out = ROOT / "dist" / "bhaau.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
