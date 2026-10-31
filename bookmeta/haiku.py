"""Claude Haiku pass after Jev: format the description, and a once-over for mangled or missing fields."""

from __future__ import annotations

import base64
import json
import os
import re

from rapidfuzz import fuzz

from .normalize import strip_accents

MODEL = os.environ.get("BOOKMETA_HAIKU_MODEL", "claude-haiku-5-5")
FIELDS = ["title", "authors", "series", "series_index", "publisher", "pubdate", "language"]

SYSTEM = """You check ebook metadata before it is written into an EPUB for an e-reader.

1. DESCRIPTION: this has already been through one filter; clean up what it missed. You may:
   - fix formatting: paragraph breaks where text runs together, missing spaces ("DepressionA Penguin
     Classic"), stray whitespace, broken encoding (â€™ -> ’), leftover HTML entities.
   - DROP whole paragraphs that are slop, not synopsis: quoted praise/review excerpts ("'Gripping' —The
     Times"), bestseller/award banners, "Praise for...", "now a major motion picture"/streaming tie-ins,
     "about the author" bios, "also by this author" lists, discussion-guide or edition notices, a pasted
     excerpt/first chapter, and paragraphs that just repeat another paragraph.
   Never reword, summarise, reorder, or add a sentence, and never touch the wording of a paragraph you keep
   - only remove whole paragraphs or fix formatting within one. If nothing needs fixing return the
   description unchanged. Return "" if the input is empty or everything in it is slop.

2. ISSUES: compare the PROPOSED metadata with the catalogue records, the filename and the book's title page.
Report only clear problems:
- misspelled or mangled title or author names ("The Conte of Monte Cresto", "Alexander Dumas" for Alexandre)
- two people merged into one name, or a name split wrongly
- a translator, editor or introducer listed as an author when the records list only the author
- a field that is empty in PROPOSED but clearly given in the records
- a series/number that contradicts the records
Do not report differences of edition, subtitle style, publisher imprint or date precision.
For each issue give the corrected value; for authors give all authors separated by "; ".
Prefer values exactly as written in a catalogue record. Return no issues if everything is fine."""

SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "enum": FIELDS},
                    "problem": {"type": "string"},
                    "suggested": {"type": "string"},
                },
                "required": ["field", "problem", "suggested"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["description", "issues"],
    "additionalProperties": False,
}


COVER_SYSTEM = """You choose the best cover for an ebook on an e-reader. Image 0 is the book's current cover (if
given); the rest are candidates from book stores. For every image say what is wrong with it, if anything:
- "movie": a film/TV tie-in cover (stills, actors, "now a major motion picture")
- "advert": banners, stickers, award or price badges, "as seen on", review quotes plastered on the art
- "generic": blank, plain text on a flat colour, Project Gutenberg / publisher placeholder, library stamp
- "wrong_book": clearly a different book, title or author
- "poor": blurry, cropped, a photo of a physical book, or a scan with borders
- "ok": none of these
Then pick the best image: never one with a problem other than "ok" if any image is "ok". Among the "ok" ones
prefer the most attractive, well-designed cover, even if it is from a different edition; prefer the current
cover when it is about as good as the alternatives. Judge the design first: each image is labelled with its
real size, but size only breaks ties between covers that look about equally good.
Keep notes and "why" to one short sentence."""

COVER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "image": {"type": "integer"},
                    "problem": {"type": "string", "enum": ["ok", "movie", "advert", "generic", "wrong_book", "poor"]},
                    "note": {"type": "string"},
                },
                "required": ["image", "problem", "note"],
                "additionalProperties": False,
            },
        },
        "best": {"type": "integer"},
        "why": {"type": "string"},
    },
    "required": ["verdicts", "best", "why"],
    "additionalProperties": False,
}


class HaikuError(Exception):
    pass


class Haiku:
    def __init__(self):
        self.client = None
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            import anthropic

            self.client = anthropic.AsyncAnthropic(max_retries=3)

    @property
    def available(self) -> bool:
        return self.client is not None

    async def _ask(self, system: str, schema: dict, content, effort: str = "low") -> dict:
        import anthropic

        try:
            resp = await self.client.messages.create(
                model=MODEL,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIError as e:
            raise HaikuError(str(e)) from e
        if resp.stop_reason != "end_turn":
            raise HaikuError(f"stopped: {resp.stop_reason}")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except ValueError as e:
            raise HaikuError("unparseable response") from e

    async def pick_cover(self, images: list[tuple[bytes, str]], has_current: bool, title: str, authors: str) -> dict:
        """`images` are (JPEG thumbnail, real size) pairs; index 0 is the current cover when `has_current`. Returns
        {"verdicts": {index: (problem, note)}, "best": index, "why": str} with indices validated."""
        content: list = []
        for i, (data, size) in enumerate(images):
            label = "current cover" if i == 0 and has_current else "candidate"
            content.append({"type": "text", "text": f"Image {i} ({label}, {size}px):"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(data).decode()}})
        content.append({"type": "text", "text": f"Book: {title} by {authors}"
                        + ("" if has_current else "\nThere is no current cover; image 0 is a candidate.")})
        out = await self._ask(COVER_SYSTEM, COVER_SCHEMA, content, effort="medium")
        if not 0 <= out.get("best", -1) < len(images):
            raise HaikuError("cover pick out of range")
        verdicts = {v["image"]: (v["problem"], v["note"]) for v in out["verdicts"] if 0 <= v["image"] < len(images)}
        return {"verdicts": verdicts, "best": out["best"], "why": out["why"]}

    async def review(self, proposed: dict, description: str, records: list[str], filename: str, front: str) -> dict:
        prompt = (
            f"PROPOSED: {json.dumps(proposed, ensure_ascii=False)}\n"
            f"FILENAME: {filename}\n"
            f"TITLE PAGE TEXT: {front}\n"
            "CATALOGUE RECORDS:\n" + ("\n".join(f"- {r}" for r in records) or "(none)") + "\n\n"
            f"DESCRIPTION:\n{description}"
        )
        return await self._ask(SYSTEM, SCHEMA, prompt)


def _letters(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(s).lower())


def _match_chunk(remainder: str, target: str) -> tuple[bool, str] | None:
    """Can `target` (one output paragraph's letters) continue matching `remainder` (unmatched tail of the
    current input paragraph)? Returns (fully consumed?, new remainder), or None if it doesn't match at all."""
    if fuzz.ratio(target, remainder) >= 95:
        return True, ""
    if 0 < len(target) < len(remainder) and fuzz.ratio(target, remainder[: len(target)]) >= 97:
        return False, remainder[len(target):]
    return None


def trim_slop(original: str, result: str) -> tuple[str, int] | None:
    """Verify `result` only dropped whole paragraphs and/or fixed formatting - never reworded or invented
    text. Paragraphs may be split (a run-together one became two) but must stay in order and keep their
    wording. Returns (verified text, paragraphs dropped), or None if the result can't be trusted."""
    paras_in = [p.strip() for p in re.split(r"\n\s*\n", original) if p.strip()]
    paras_out = [p.strip() for p in re.split(r"\n\s*\n", result) if p.strip()]
    if not paras_out and paras_in:
        return None  # dropping everything looks like a failure, not genuine all-slop; keep the original
    i, remainder = 0, None
    for p in paras_out:
        target, matched = _letters(p), False
        while i < len(paras_in):
            cand = remainder if remainder is not None else _letters(paras_in[i])
            outcome = _match_chunk(cand, target)
            if outcome is None:
                if remainder is not None:
                    return None  # mid-split paragraph that doesn't continue as expected -> invented text
                i += 1  # skip this input paragraph (dropped as slop) and try the next
                continue
            whole, remainder = outcome
            if whole:
                i += 1
                remainder = None
            matched = True
            break
        if not matched:
            return None  # ran out of input paragraphs without a match -> invented text
    return "\n\n".join(paras_out), len(paras_in) - len(paras_out)


def _key(s: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(s).lower().replace(".", "")).strip()


def in_records(fieldname: str, value, records: list) -> bool:
    """True when a source record states exactly this value, so the correction isn't Haiku's invention."""
    if fieldname == "authors":
        names = {_key(a) for c in records for a in c.authors}
        return bool(value) and all(_key(v) in names for v in value)
    if fieldname == "title":
        return any(_key(value) in (_key(c.title), _key(f"{c.title}: {c.subtitle}")) for c in records)
    return any(_key(str(value)) == _key(str(getattr(c, fieldname, ""))) for c in records)
