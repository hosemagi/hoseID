"""Capture media behind a sighting: path safety, downscaled previews, originals.

Every media_refs entry carries an absolute ``path``. We only ever serve a
path that resolves inside the landing-zone assets dir, so a corrupted or
hand-edited ref cannot turn the server into a file reader.

Previews are JPEGs downscaled to a max long edge (aspect preserved, never
cropped): full-res trailcam frames waste an LLM's context without adding
ID value. They are cached under derived/ (regenerable by definition) keyed
by asset digest + size, stable because the landing zone is immutable.
"""
from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

from hoseid import paths

DEFAULT_MAX_PX = 768
MAX_MAX_PX = 1024
PREVIEW_QUALITY = 82
VIDEO_POSTER_SEEK_S = 1.0
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".mp4": "video/mp4", ".mov": "video/quicktime"}


class MediaError(RuntimeError):
    pass


def preview_dir() -> Path:
    return paths.derived_dir() / "mcp-previews"


def clamp_px(max_px: int | None) -> int:
    try:
        v = int(max_px) if max_px is not None else DEFAULT_MAX_PX
    except (TypeError, ValueError):
        v = DEFAULT_MAX_PX
    return max(128, min(v, MAX_MAX_PX))


def safe_path(entry: dict) -> Path:
    """The on-disk file for a media entry, or MediaError if missing/outside assets."""
    raw = entry.get("path")
    if not raw:
        raise MediaError(f"media[{entry.get('index')}] has no path")
    p = Path(raw).expanduser().resolve()
    root = paths.assets_dir().resolve()
    if not p.is_relative_to(root):
        raise MediaError(f"media[{entry.get('index')}] path is outside the assets dir")
    if not p.is_file():
        raise MediaError(f"media[{entry.get('index')}] file is missing on disk: {p.name}")
    return p


def mime_of(p: Path) -> str:
    return MIME.get(p.suffix.lower(), "application/octet-stream")


def review_url(p: Path, base: str) -> str | None:
    """URL on the hoseID review app (port 8870 /images/<rel>) for the same file."""
    try:
        rel = p.resolve().relative_to(paths.assets_dir().resolve())
    except ValueError:
        return None
    return f"{base.rstrip('/')}/images/{rel.as_posix()}"


def _cache_key(entry: dict, p: Path, max_px: int) -> str:
    ref = entry.get("ref") or p.stem
    return ref.replace(":", "_").replace("/", "_") + f"_{max_px}"


def _image_preview(p: Path, max_px: int) -> bytes:
    from PIL import Image, ImageOps
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((max_px, max_px))  # aspect preserved, never crops
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=PREVIEW_QUALITY, optimize=True)
        return buf.getvalue()


def _video_poster(p: Path, max_px: int) -> bytes:
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(ffmpeg).exists():
        raise MediaError("ffmpeg not available; cannot render a video poster frame")
    scale = f"scale='min({max_px},iw)':-2"
    res = None
    for seek in (VIDEO_POSTER_SEEK_S, 0.0):
        cmd = [ffmpeg, "-v", "error", "-ss", str(seek), "-i", str(p), "-frames:v", "1",
               "-vf", scale, "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
        res = subprocess.run(cmd, capture_output=True, timeout=60)
        if res.returncode == 0 and res.stdout:
            return res.stdout
    err = res.stderr.decode(errors="replace").strip()[:200] if res else ""
    raise MediaError(f"ffmpeg could not extract a frame from {p.name}: {err}")


def preview_jpeg(entry: dict, max_px: int | None = None) -> bytes:
    """Downscaled JPEG for an image, or a poster frame for a video; cached."""
    max_px = clamp_px(max_px)
    p = safe_path(entry)
    cache = preview_dir() / f"{_cache_key(entry, p, max_px)}.jpg"
    if cache.is_file() and cache.stat().st_size > 0:
        return cache.read_bytes()
    data = _video_poster(p, max_px) if entry.get("kind") == "video" else _image_preview(p, max_px)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(cache)
    except OSError:
        pass  # cache is an optimisation; serving still succeeds
    return data


def original(entry: dict) -> tuple[bytes, str, Path]:
    p = safe_path(entry)
    return p.read_bytes(), mime_of(p), p
