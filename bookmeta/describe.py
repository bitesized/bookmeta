"""Turn publisher descriptions into a clean synopsis: no blurbs, film tie-ins, bios or repeats."""

from __future__ import annotations

import html
import re

from rapidfuzz import fuzz

def clean_html(text: str) -> str:
    """Keep descriptions readable in KOReader: paragraphs preserved, other markup dropped."""
    if not text:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h\d|blockquote)\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# Paragraphs matching these are marketing regardless of what Jev thinks.
_DRIVEL = re.compile(
    r"""(?ix)
    now\s+a\s+(major\s+)?(motion\s+picture|film|movie|netflix|hbo|tv|television|series|limited\s+series)
    | soon\s+to\s+be\s+a | coming\s+soon\s+to | starring\s+[A-Z]
    | ^\s*(praise|acclaim)\s+for\b | ^\s*(winner|finalist)\s+of\s+the\b | (short|long)listed\s+for
    | \#\s?1\s+(new\s+york\s+times|international|sunday\s+times|national|instant)?\s*best\s?sell
    | new\s+york\s+times\s+best\s?sell(er|ing)\b(?!.*\b(he|she|they)\b)
    | named\s+(a|one\s+of\s+the)\s+best\s+books | (a|an)\s+\w+\s+(notable|best)\s+book\s+of\s+the\s+year
    | includes\s+a\s+(reading\s+group|discussion)\s+guide | this\s+edition\s+(includes|features|contains)
    | enhanced\s+edition | (also\s+)?available\s+(as|in)\s+(an\s+)?(audio|ebook|hardcover|paperback)
    | ^\s*(look\s+for|don'?t\s+miss|discover\s+more|read\s+more|also\s+by|from\s+the\s+author\s+of)\b
    | ^\s*about\s+the\s+author\b | ^\s*(an\s+)?excerpt\b | ^\s*(copyright|©)
    """
)
# “Gripping...” —The Guardian   /   "A triumph." - Stephen King
_QUOTE_BLURB = re.compile(r"""^\s*["“'‘].{3,400}["”'’]\s*(?:[—–-]{1,2}|―)\s*\S""", re.S)
_ATTRIBUTION_ONLY = re.compile(r"^\s*(?:[—–-]{1,2})\s*[A-Z][\w .,&'’]{2,60}$")
_CAPS_LEAD = re.compile(r"^((?:[A-Z0-9#•·|:;,'’&!?.\-–—\s]|\bthe\b){12,}?)\s+(?=[A-Z][a-z])")

# Store descriptions often paste the first chapter after one of these headings; nothing after it is synopsis.
_EXCERPT_HEAD = re.compile(r"(?i)^\s*(excerpt\b.*|read an excerpt|chapter (1|one)\b.*|prologue|(1|i)\.?)\s*$")

KINDS = {
    "synopsis": "Describes the book's story, subject or argument (what the book is about)",
    "praise": "Quoted praise, review excerpts or endorsements from critics, authors or publications",
    "marketing": "Sales/marketing copy: bestseller claims, awards, film or TV tie-ins, edition features, calls to action",
    "author_bio": "Biography of the author or translator",
    "other": "Table of contents, legal text, series list or other non-synopsis content",
}


def paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n+", text or "") if p.strip()]
    out: list[str] = []
    for p in parts:
        if _EXCERPT_HEAD.match(p):
            break
        # "#1 NEW YORK TIMES BESTSELLER • NOW A MAJOR MOTION PICTURE In this ..." -> split off the caps banner
        m = _CAPS_LEAD.match(p)
        if m and sum(ch.isupper() for ch in m.group(1)) >= 8:
            out.append(m.group(1).strip())
            p = p[m.end():]
        out.append(p)
    return out


def rule_kind(p: str) -> str | None:
    letters = [ch for ch in p if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.7 and len(letters) > 6:
        return "marketing"
    if _QUOTE_BLURB.match(p) or _ATTRIBUTION_ONLY.match(p):
        return "praise"
    if _DRIVEL.search(p) and len(p) < 400:
        return "marketing"
    if len(p) < 25 and not p.endswith((".", "!", "?")):
        return "other"
    return None


def dedupe(paras: list[str]) -> list[str]:
    out: list[str] = []
    for p in paras:
        if any(fuzz.ratio(p.lower(), q.lower()) > 90 or (len(p) > 60 and p.lower() in q.lower()) for q in out):
            continue
        out.append(p)
    return out


def join(paras: list[str]) -> str:
    return "\n\n".join(paras)


class DescriptionCleaner:
    """Two passes: cheap rules, then Jev per remaining paragraph (batched by the caller)."""

    KEEP_THRESHOLD = 0.5  # keep when P(synopsis) is at least this
    REVIEW_BAND = (0.3, 0.5)  # dropping a paragraph in this band needs a human

    def __init__(self, text: str):
        self.paras = dedupe(paragraphs(text))
        self.kinds: list[str | None] = [rule_kind(p) for p in self.paras]
        self.synopsis_p: list[float | None] = [None] * len(self.paras)

    def pending(self) -> list[tuple[int, str]]:
        return [(i, p) for i, p in enumerate(self.paras) if self.kinds[i] is None]

    def record(self, i: int, probs: dict[str, float]) -> None:
        self.synopsis_p[i] = probs.get("synopsis", 0.0)
        self.kinds[i] = max(probs, key=probs.get)

    def result(self) -> tuple[str, bool]:
        """Returns (clean text, confident). Unclassified paragraphs are kept."""
        keep = []
        confident = True
        for p, kind, sp in zip(self.paras, self.kinds, self.synopsis_p):
            if kind is None or kind == "synopsis" or (sp is not None and sp >= self.KEEP_THRESHOLD):
                keep.append(p)
            elif sp is not None and sp > self.REVIEW_BAND[0]:
                confident = False  # dropped something that might be synopsis
        return join(keep), confident


def to_html(text: str) -> str:
    return "".join(f"<p>{html.escape(p)}</p>" for p in text.split("\n\n") if p.strip())
