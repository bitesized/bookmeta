"""Claude Haiku pass after Jev: format the description, and a once-over for mangled or missing fields."""

from __future__ import annotations

import json
import os
import re

from rapidfuzz import fuzz

from .normalize import strip_accents

MODEL = os.environ.get("BOOKMETA_HAIKU_MODEL", "claude-haiku-5-5")
FIELDS = ["title", "authors", "series", "series_index", "publisher", "pubdate", "language"]

SYSTEM = """You check ebook metadata before it is written into an EPUB for an e-reader.

1. DESCRIPTION: return the description with formatting fixed only: paragraph breaks (blank line between
paragraphs) where the text obviously runs paragraphs together, missing spaces ("DepressionA Penguin Classic"),
stray whitespace, broken encoding (â€™ -> ’), leftover HTML entities. Never reword, summarise, reorder, add or
remove sentences. If nothing needs fixing return it unchanged. Return "" if the input is empty.

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

    async def review(self, proposed: dict, description: str, records: list[str], filename: str, front: str) -> dict:
        import anthropic

        prompt = (
            f"PROPOSED: {json.dumps(proposed, ensure_ascii=False)}\n"
            f"FILENAME: {filename}\n"
            f"TITLE PAGE TEXT: {front}\n"
            "CATALOGUE RECORDS:\n" + ("\n".join(f"- {r}" for r in records) or "(none)") + "\n\n"
            f"DESCRIPTION:\n{description}"
        )
        try:
            resp = await self.client.messages.create(
                model=MODEL,
                max_tokens=8000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
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


def _letters(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(s).lower())


def same_words(before: str, after: str) -> bool:
    """Formatting may change spacing, breaks and encoding, never the words."""
    a, b = _letters(before), _letters(after)
    return bool(b) and fuzz.ratio(a, b) >= 98 and abs(len(a) - len(b)) <= max(10, len(a) // 100)


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
