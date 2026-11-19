"""Your own image for each iPhone model, used everywhere instead of the store's photo.

    python -m scraper image "iPhone 17 Pro" https://example.com/iphone-17-pro.png --credit "Image: Apple"
    python -m scraper image "iPhone 17 Pro" C:\\Users\\me\\Pictures\\17pro.jpg --credit "Photo: Bhaau"
    python -m scraper image --list
    python -m scraper image "iPhone 17 Pro" --remove

The image is downloaded once, trimmed, resized and saved to web/images/models/<model>.webp, and
recorded in scraper/config/models.yaml. The site then serves it from your own hosting, so it never
breaks when a store changes its website. You choose every image, so make sure you're allowed to use it.
Models without an entry keep using the photo from a seller's listing.
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path

import requests
import yaml

from .normalize import parse_model

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MODELS_FILE = ROOT / "scraper" / "config" / "models.yaml"
IMG_SUBDIR = "images/models"
MAX_BYTES = 15_000_000
MAX_SIDE = 800
HEADER = """\
# Your own image for each iPhone model, used on every page instead of the store's photo.
# Managed by:  python -m scraper image "iPhone 17 Pro" <image link or file> --credit "Image: Apple"
# You can also edit it by hand: the image path is inside web/, the credit is shown under the image.
"""


class ImageProblem(ValueError):
    """The image would look wrong on the site; nothing was saved."""
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def canonical_model(name: str) -> str | None:
    """'17 pro' / 'iphone 17 pro' / 'iPhone 17 Pro' -> 'iPhone 17 Pro'."""
    parsed = parse_model(name if "iphone" in name.lower() else f"iPhone {name}")
    return parsed[0] if parsed else None


def _read(path: Path | None = None) -> dict:
    path = path or MODELS_FILE
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("models") or {}


def _write(models: dict, path: Path | None = None) -> None:
    path = path or MODELS_FILE
    body = yaml.safe_dump({"models": dict(sorted(models.items()))}, allow_unicode=True, sort_keys=False)
    path.write_text(HEADER + "\n" + body, encoding="utf-8")


def load(web_dir: Path, path: Path | None = None) -> dict[str, dict]:
    """{model: {"image": "images/models/x.webp", "credit": "..."}} for entries whose file exists."""
    out = {}
    for model, entry in _read(path).items():
        entry = entry or {}
        img = str(entry.get("image") or "").replace("\\", "/").lstrip("/")
        if not img:
            continue
        if not (web_dir / img).is_file():
            log.warning("models.yaml: image for %s not found at web/%s; using the store's photo", model, img)
            continue
        out[model] = {"image": img, "credit": (entry.get("credit") or None)}
    return out


def _fetch(source: str) -> tuple[bytes, str]:
    """Bytes and a file extension, from a web link or a file on this computer."""
    if re.match(r"^https?://", source, re.I):
        r = requests.get(source, timeout=30, stream=True, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8"})
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            raise ValueError(f"that link is a {ctype or 'web page'}, not an image. Right-click the image -> "
                             "'Copy image address' and use that link.")
        chunks, size = [], 0
        for chunk in r.iter_content(64 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                break
        data = b"".join(chunks)
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/avif": ".avif",
               "image/gif": ".gif"}.get(ctype, ".img")
    else:
        p = Path(source.strip('"')).expanduser()
        if not p.is_file():
            raise ValueError(f"no file at {p}")
        data, ext = p.read_bytes()[: MAX_BYTES + 1], p.suffix.lower()
    if len(data) > MAX_BYTES:
        raise ValueError("that image is over 15 MB; pick a smaller one")
    return data, ext


def check_image(im) -> list[str]:
    """Problems that make a model image look wrong on the white cards: a dark or coloured
    background, or a wide banner / group shot instead of one phone."""
    rgba = im.convert("RGBA")
    w, h = rgba.size
    k = max(1, min(w, h) // 20)
    corners = [rgba.crop(box).resize((1, 1)).getpixel((0, 0)) for box in
               ((0, 0, k, k), (w - k, 0, w, k), (0, h - k, k, h), (w - k, h - k, w, h))]
    problems = []
    solid = [c for c in corners if c[3] > 200]
    if len(solid) >= 3:
        r, g, b = (sum(c[i] for c in solid) / len(solid) for i in range(3))
        if min(r, g, b) < 225:
            shade = "dark" if (r + g + b) / 3 < 90 else "coloured"
            problems.append(f"it has a {shade} background, so it will show as a {shade} box on the white cards. "
                            "Pick one with a white or transparent background.")
    return problems


def _wide(size: tuple[int, int]) -> list[str]:
    w, h = size
    return (["it's wider than it is tall, so it's probably a banner or several phones. "
             "Pick an image of one phone (back view)."] if w > 1.15 * h else [])


def _optimise(data: bytes) -> tuple[bytes, str, tuple[int, int], list[str]]:
    """Trim empty borders, fit inside 800x800, save as WebP. Needs Pillow."""
    from PIL import Image, ImageChops
    im = Image.open(io.BytesIO(data))
    im.load()
    im = im.convert("RGBA")
    problems = check_image(im)
    # trim transparent edges, then plain white edges
    bbox = im.getchannel("A").getbbox()
    if bbox:
        im = im.crop(bbox)
    rgb = im.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, (255, 255, 255)))
    bbox = ImageChops.add(diff, diff, 2.0, -12).getbbox()
    if bbox and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 0.2 * im.size[0] * im.size[1]:
        im = im.crop(bbox)
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    pad = max(8, round(max(im.size) * 0.04))  # a little breathing room around the phone
    canvas = Image.new("RGBA", (im.size[0] + 2 * pad, im.size[1] + 2 * pad), (0, 0, 0, 0))
    canvas.paste(im, (pad, pad), im)
    buf = io.BytesIO()
    canvas.save(buf, "WEBP", quality=86, method=6)
    return buf.getvalue(), ".webp", canvas.size, problems + _wide(im.size)


def import_image(model_name: str, source: str, credit: str | None, web_dir: Path,
                 models_file: Path | None = None, force: bool = False) -> tuple[str, str]:
    """Save the image for a model and record it. Returns (model, path inside web/).
    Raises ImageProblem (without saving) if it would look wrong, unless force=True."""
    model = canonical_model(model_name)
    if not model:
        raise ValueError(f"'{model_name}' isn't an iPhone model I recognise (try e.g. \"iPhone 17 Pro\")")
    data, ext = _fetch(source)
    try:
        data, ext, size, problems = _optimise(data)
        if problems and not force:
            raise ImageProblem(problems)
        log.info("optimised to %dx%d WebP (%d KB)", size[0], size[1], len(data) // 1024)
    except ImportError:
        log.warning("Pillow isn't installed, so the image is saved as-is (pip install Pillow to optimise it)")
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".avif"):
            raise ValueError("without Pillow only .jpg, .png, .webp or .avif images can be used")
    except ImageProblem:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"that file isn't an image I can read ({e})")
    folder = web_dir / IMG_SUBDIR
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{slug(model)}.*"):
        old.unlink()
    rel = f"{IMG_SUBDIR}/{slug(model)}{ext}"
    (web_dir / rel).write_bytes(data)
    models = _read(models_file)
    models[model] = {"image": rel, **({"credit": credit} if credit else {})}
    _write(models, models_file)
    return model, rel


def remove_image(model_name: str, web_dir: Path, models_file: Path | None = None) -> str | None:
    model = canonical_model(model_name)
    models = _read(models_file)
    if not model or model not in models:
        return None
    img = str((models.pop(model) or {}).get("image") or "")
    if img and (web_dir / img).is_file():
        (web_dir / img).unlink()
    _write(models, models_file)
    return model
