"""Cover download and comparison. A cover is only ever replaced by a larger copy of the same artwork."""

from __future__ import annotations

import io
from dataclasses import dataclass

import httpx
from PIL import Image, ImageOps

MIN_USEFUL_HEIGHT = 600  # below this a cover looks soft on any modern Kobo
TINY_HEIGHT = 350  # below this the current cover is not worth matching against
SAME_ART_MAX_DISTANCE = 14  # of 64 dHash bits
UPGRADE_FACTOR = 1.4


@dataclass
class CoverInfo:
    data: bytes
    width: int
    height: int
    dhash: int
    url: str = ""
    source: str = ""

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}" + (f" ({self.source})" if self.source else "")


def dhash(im: Image.Image) -> int:
    g = ImageOps.grayscale(im).resize((9, 8), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def inspect(data: bytes | None, url: str = "", source: str = "") -> CoverInfo | None:
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            w, h = im.size
            # Cropping away borders makes the hash robust to letterboxing differences between stores.
            bbox = ImageOps.invert(ImageOps.grayscale(im)).getbbox() if im.mode != "P" else None
            body = im.crop(bbox) if bbox and (bbox[2] - bbox[0]) > w * 0.7 else im
            return CoverInfo(data, w, h, dhash(body), url, source)
    except Exception:
        return None


def plausible(c: CoverInfo) -> bool:
    """Reject placeholders, banners and square audiobook art."""
    ratio = c.height / c.width if c.width else 0
    return c.height >= 300 and 1.15 <= ratio <= 1.8


async def fetch(client: httpx.AsyncClient, url: str, source: str) -> CoverInfo | None:
    try:
        r = await client.get(url, timeout=30)
    except httpx.HTTPError:
        return None
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        return None
    info = inspect(r.content, url, source)
    return info if info and plausible(info) else None


@dataclass
class CoverDecision:
    chosen: CoverInfo | None
    auto: bool
    reason: str


def decide(current: CoverInfo | None, candidates: list[CoverInfo], identity_by_isbn: bool) -> CoverDecision:
    if not candidates:
        return CoverDecision(None, True, "no candidate covers")
    ranked = sorted(candidates, key=lambda c: c.width * c.height, reverse=True)

    if current and current.height >= TINY_HEIGHT:
        same = [c for c in ranked if distance(c.dhash, current.dhash) <= SAME_ART_MAX_DISTANCE]
        better = [c for c in same if c.height >= current.height * UPGRADE_FACTOR]
        if better:
            return CoverDecision(better[0], True, f"same artwork, {current.height}px -> {better[0].height}px")
        if current.height >= MIN_USEFUL_HEIGHT or not ranked:
            return CoverDecision(None, True, f"current {current.width}x{current.height} is fine")
        # Current is mediocre and no store has the same art at higher resolution. A different
        # edition's cover might be welcome, but that's a taste call, so it goes to review.
        best = ranked[0]
        if best.height >= current.height * 2:
            return CoverDecision(best, False, f"different artwork; current only {current.height}px")
        return CoverDecision(None, True, "no clear upgrade")

    # Missing or tiny cover: prefer art that two sources agree on, otherwise ISBN-matched art.
    for c in ranked:
        agree = [o for o in ranked if o is not c and distance(o.dhash, c.dhash) <= SAME_ART_MAX_DISTANCE]
        if agree and c.height >= MIN_USEFUL_HEIGHT:
            return CoverDecision(c, True, f"{'no' if not current else 'tiny'} cover; {len(agree) + 1} sources agree")
    best = ranked[0]
    return CoverDecision(best, identity_by_isbn and best.height >= MIN_USEFUL_HEIGHT,
                         f"{'no' if not current else 'tiny'} cover; single source")


def distinct(candidates: list[CoverInfo]) -> list[CoverInfo]:
    """One cover per artwork (the largest copy), largest first."""
    out: list[CoverInfo] = []
    for c in sorted(candidates, key=lambda c: c.width * c.height, reverse=True):
        if all(distance(c.dhash, o.dhash) > SAME_ART_MAX_DISTANCE for o in out):
            out.append(c)
    return out


def thumbnail(data: bytes, height: int = 512) -> bytes:
    """A small JPEG for Haiku to look at (~350 tokens at 512px tall)."""
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        if im.height > height:
            im = im.resize((max(1, round(im.width * height / im.height)), height), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()


def montage(images: list[tuple[bytes, str]], height: int = 700) -> bytes:
    """Covers side by side with a caption under each, for comparing in an image viewer."""
    from PIL import ImageDraw, ImageFont

    tiles = []
    for data, _ in images:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            tiles.append(im.resize((max(1, round(im.width * height / im.height)), height), Image.LANCZOS))
    pad, caption = 24, 90
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
    except OSError:
        font = ImageFont.load_default()
    sheet = Image.new("RGB", (sum(t.width for t in tiles) + pad * (len(tiles) + 1), height + caption + pad), "white")
    draw = ImageDraw.Draw(sheet)
    x = pad
    for tile, (_, text) in zip(tiles, images):
        sheet.paste(tile, (x, pad))
        draw.multiline_text((x, height + pad + 8), text, fill="black", font=font, spacing=4)
        x += tile.width + pad
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=90)
    return buf.getvalue()
