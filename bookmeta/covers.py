"""Cover download, comparison and choice. Only a sharper copy of the same image replaces a cover without review;
other artwork is scored (Haiku's look at it, real resolution, source) and offered for the user to confirm."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

import httpx
import numpy as np
from PIL import Image, ImageOps

from .epub import COVER_MAX_EDGE

MIN_USEFUL_HEIGHT = 600  # below this a cover looks soft on any modern Kobo
MIN_REPLACEMENT_HEIGHT = 1000  # a new cover must be at least this tall unless the book has next to nothing
TINY_HEIGHT = 350  # below this the current cover is not worth matching against
# Of 64 dHash bits. The same artwork from two stores often differs by 15-20 bits (other crop, a sticker, a photo of
# the printed book), as do true variants, so only near-identical images count as the same: merged when listing
# options, and the only kind of swap made without review.
SAME_IMAGE_MAX_DISTANCE = 8
SAME_IMAGE_MAX_RATIO_DIFF = 0.04  # a different crop of the same art changes the shape
AGREE_MAX_DISTANCE = 14  # loose enough to count two stores' crops of one artwork as agreeing on it
UPGRADE_FACTOR = 1.4
# Store artwork from the publisher feeds comes first; Open Library is mostly user-uploaded scans, so it's a last resort.
SOURCE_TIER = {"apple": 0, "amazon": 0, "google": 0, "hardcover": 1, "openlibrary": 2}
LAST_RESORT = {"openlibrary"}

# Scoring. Haiku rates each image's appeal 1-10; resolution adds up to RES_POINTS for a cover that fills an
# e-reader screen; a problem Haiku spots costs points rather than ruling the cover out, except for the two that
# make it plainly the wrong cover. The current cover gets a small bonus so a change has to be a real improvement.
SCREEN_HEIGHT = 1800
RES_POINTS = 4.0
SOURCE_POINTS = {"apple": 0.5, "amazon": 0.5, "google": 0.0, "hardcover": 0.0, "openlibrary": -1.0}
PENALTY = {"ok": 0.0, "advert": 2.0, "movie": 3.0, "scan": 3.0, "poor": 3.0, "foreign": 5.0}
VETO = {"wrong_book", "generic"}
CURRENT_BONUS = 1.0
# Taste never excuses a small cover: once a sharp cover with nothing worse than a banner is on offer, anything
# below MIN_REPLACEMENT_HEIGHT (the current cover included) is out of the running.
SHARP_HEIGHT = 1200
ACCEPTABLE = {"ok", "advert"}


@dataclass
class CoverInfo:
    data: bytes
    width: int
    height: int
    dhash: int
    url: str = ""
    source: str = ""
    soft: bool = False  # upscaled from something about half the size or less

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}" + (" soft" if self.soft else "") + (f" ({self.source})" if self.source else "")


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


def same_image(a: CoverInfo | None, b: CoverInfo | None) -> bool:
    """The same picture, give or take scaling, re-encoding and letterboxing."""
    if a is None or b is None:
        return False
    ra, rb = a.height / a.width, b.height / b.width
    return distance(a.dhash, b.dhash) <= SAME_IMAGE_MAX_DISTANCE and abs(ra - rb) <= SAME_IMAGE_MAX_RATIO_DIFF * ra


def looks_upscaled(im: Image.Image) -> bool:
    """True when the image holds about as much detail as one half its size. Shrinking a genuinely sharp image to
    half and back loses roughly half as much as shrinking it to a quarter; an enlarged one loses far less at half,
    because that detail was never there (ratio ~0.55 for store art, ~0.3 for 2x+ upscales on the library)."""
    if im.height < 800:
        return False  # small covers are judged on their size alone
    g = ImageOps.grayscale(im)
    if max(g.size) > COVER_MAX_EDGE:
        g = g.resize((round(g.width * COVER_MAX_EDGE / max(g.size)), round(g.height * COVER_MAX_EDGE / max(g.size))), Image.LANCZOS)
    a = np.asarray(g, dtype=np.float32)

    def loss(f: float) -> float:
        small = g.resize((max(1, round(g.width * f)), max(1, round(g.height * f))), Image.LANCZOS)
        return float(np.abs(a - np.asarray(small.resize(g.size, Image.BICUBIC), dtype=np.float32)).mean())

    quarter = loss(0.25)
    return quarter >= 0.5 and loss(0.5) / quarter < 0.42  # below 0.5 the cover is too flat to tell


def tier(c: CoverInfo) -> int:
    return SOURCE_TIER.get(c.source, 1)


def rank_key(c: CoverInfo) -> tuple:
    """Better source first, then sharper."""
    return tier(c), -effective_height(c)


def stored_height(c: CoverInfo) -> int:
    """Height once written to the book, which caps the longest edge."""
    return round(c.height * min(1.0, COVER_MAX_EDGE / max(c.width, c.height)))


def effective_height(c: CoverInfo) -> int:
    """How sharp the cover will look: its stored height, halved if it was blown up from a smaller image."""
    h = stored_height(c)
    return h // 2 if c.soft else h


def upgrades(current: CoverInfo, c: CoverInfo) -> bool:
    """Noticeably sharper than `current` after the cap; a 3000px copy of a cover already stored at 2000px isn't."""
    return effective_height(c) >= effective_height(current) * UPGRADE_FACTOR


def min_height(current: CoverInfo | None) -> int:
    """The smallest cover worth offering: a book with a decent cover only ever gets a sharp one."""
    return MIN_REPLACEMENT_HEIGHT if current and effective_height(current) >= MIN_USEFUL_HEIGHT else MIN_USEFUL_HEIGHT


def eligible(current: CoverInfo | None, candidates: list[CoverInfo]) -> list[CoverInfo]:
    floor = min_height(current)
    return [c for c in candidates if effective_height(c) >= floor]


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
            return CoverInfo(data, w, h, dhash(body), url, source, looks_upscaled(im))
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
    info = await asyncio.to_thread(inspect, r.content, url, source)
    return info if info and plausible(info) else None


@dataclass
class CoverDecision:
    chosen: CoverInfo | None
    auto: bool
    reason: str


@dataclass
class Verdict:
    """Haiku's view of one image."""
    problem: str = "ok"
    note: str = ""
    appeal: float = 5.0


def score(c: CoverInfo, v: Verdict, is_current: bool) -> float | None:
    """Higher is better; None when the image is plainly not this book's cover."""
    if v.problem in VETO:
        return None
    res = RES_POINTS * min(effective_height(c), SCREEN_HEIGHT) / SCREEN_HEIGHT
    return (v.appeal + res + SOURCE_POINTS.get(c.source, 0.0) - PENALTY.get(v.problem, 3.0)
            + (CURRENT_BONUS if is_current else 0.0))


def choose(current: CoverInfo | None, options: list[CoverInfo], verdicts: dict[int, Verdict] | None,
           identity_by_isbn: bool, pool: list[CoverInfo] | None = None) -> tuple[CoverDecision, int]:
    """Pick from `options` (already eligible and distinct), with `verdicts` indexed like [current, *options] when
    there is a current cover; `pool` is every copy fetched, for counting sources that agree. Returns the decision and the index of the best-scoring image (-1 if none).
    Without Haiku every image is taken as fine and equally attractive, so only resolution and source count."""
    off = 1 if current else 0
    shown = ([current] if current else []) + options
    vs = [(verdicts or {}).get(i, Verdict()) for i in range(len(shown))]
    scored = [(s, i) for i, c in enumerate(shown) if (s := score(c, vs[i], i < off)) is not None]
    if any(effective_height(c) >= SHARP_HEIGHT and vs[i].problem in ACCEPTABLE for i, c in enumerate(shown) if i >= off):
        scored = [(s, i) for s, i in scored if effective_height(shown[i]) >= MIN_REPLACEMENT_HEIGHT]
    if not scored:
        return CoverDecision(None, True, "no usable cover"), -1
    # Ties go to the current image, then the better-ranked option.
    best_score, best = max(scored, key=lambda t: (t[0], same_image(current, shown[t[1]]), -t[1]))
    pick = shown[best]
    if best < off:
        return CoverDecision(None, True, f"current {current.width}x{current.height} scores best"), best
    clean = vs[best].problem == "ok"
    if current and current.height >= TINY_HEIGHT:
        cur_score = next((s for s, i in scored if i == 0), None)
        sharper = upgrades(current, pick) or (cur_score is None and effective_height(pick) > effective_height(current))
        if same_image(current, pick):
            if sharper and clean:
                return CoverDecision(pick, True, f"same image, {effective_height(current)}px -> {effective_height(pick)}px"), best
            if not sharper:
                return CoverDecision(None, True, "current cover kept; the stores' copy is no sharper"), 0
        if verdicts is None and cur_score is not None and effective_height(current) >= MIN_USEFUL_HEIGHT:
            return CoverDecision(None, True, f"current {current.width}x{current.height} is fine"), 0  # no taste call without Haiku
        if cur_score is not None:
            why = f"scores {best_score:.1f} vs current {cur_score:.1f}"
        elif score(current, vs[0], True) is None:
            why = f"current cover looks like {vs[0].problem.replace('_', ' ')}"
        else:
            why = f"current cover is only {effective_height(current)}px"
        return CoverDecision(pick, False, f"different cover; {why}" + (f" ({vs[best].note})" if vs[best].note else "")), best
    # Missing or tiny cover: anything sensible beats nothing, but only well-attested store art goes in unreviewed.
    agree = len({o.source for o in pool or options if o is not pick and o.source != pick.source
                 and distance(o.dhash, pick.dhash) <= AGREE_MAX_DISTANCE})
    auto = (clean and effective_height(pick) >= MIN_USEFUL_HEIGHT and pick.source not in LAST_RESORT
            and (identity_by_isbn or agree > 0))
    return CoverDecision(pick, auto, f"{'no' if not current else 'tiny'} cover; "
                         + (f"{agree + 1} sources agree" if agree else "single source")), best


def distinct(candidates: list[CoverInfo]) -> list[CoverInfo]:
    """One entry per image (its sharpest copy, then best source), best source then largest first. Different
    crops and variants of the same art stay separate so Haiku and the user can compare them."""
    out: list[CoverInfo] = []
    for c in sorted(candidates, key=lambda c: (-effective_height(c), tier(c))):
        if not any(same_image(c, o) for o in out):
            out.append(c)
    return sorted(out, key=rank_key)


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
    """Covers side by side with a caption under each, for comparing in an image viewer. Each tile is scaled
    relative to the tallest cover and never enlarged, so a small cover looks small next to a large one."""
    from PIL import ImageDraw, ImageFont

    opened = []
    for data, _ in images:
        with Image.open(io.BytesIO(data)) as im:
            opened.append(im.convert("RGB"))
    scale = height / max(im.height for im in opened)
    tiles = []
    for im in opened:
        h = max(120, round(im.height * scale))
        tiles.append(im.resize((max(1, round(im.width * h / im.height)), h), Image.LANCZOS))
    pad, caption = 24, 90
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
    except OSError:
        font = ImageFont.load_default()
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    # Each slot is as wide as its caption, so a small cover's caption doesn't run into the next one.
    slots = [max(t.width, round(probe.multiline_textbbox((0, 0), text, font=font, spacing=4)[2]))
             for t, (_, text) in zip(tiles, images)]
    sheet = Image.new("RGB", (sum(slots) + pad * (len(tiles) + 1), height + caption + pad), "white")
    draw = ImageDraw.Draw(sheet)
    x = pad
    for tile, slot, (_, text) in zip(tiles, slots, images):
        sheet.paste(tile, (x, pad + height - tile.height))
        draw.multiline_text((x, height + pad + 8), text, fill="black", font=font, spacing=4)
        x += slot + pad
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=90)
    return buf.getvalue()
