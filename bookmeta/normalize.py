"""Deterministic clean-ups for repeated or malformed metadata, and fuzzy identity matching."""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

_SERIES_IN_TITLE = re.compile(
    r"\s*[(\[]\s*(?:the\s+)?(?P<series>[^()\[\]]*?)[\s,:#-]*(?:book|vol\.?|volume|no\.?|#|part)\s*(?P<index>\d+(?:\.\d+)?)\s*[)\]]\s*$",
    re.I,
)
_NOVEL_SUFFIX = re.compile(r"\s*(?::\s*|\s*[(\[]\s*|\s+-\s+)an?\s+(novel|thriller|mystery|memoir)(\s+in\s+stories)?\s*[)\]]?\s*$", re.I)
_TRANSLATOR = re.compile(r"\s*\((?:translat(?:or|ion|ed)|trans\.?|editor|ed\.?|illustrator)\)\s*$", re.I)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_title(t: str) -> str:
    t = strip_accents(t).lower()
    t = re.split(r"\s*[:(\[]\s*", t)[0]  # main title only
    t = re.sub(r"^(the|a|an)\s+", "", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t.replace("&", "and"))
    return re.sub(r"\s+", " ", t).strip()


def surname(name: str) -> str:
    name = strip_accents(name).lower().strip()
    if "," in name:
        name = name.split(",")[0]
    else:
        name = name.split()[-1] if name.split() else name
    return re.sub(r"[^a-z]", "", name)


def title_similarity(a: str, b: str) -> float:
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    return max(fuzz.ratio(na, nb), fuzz.token_set_ratio(na, nb) - 5) / 100


def author_similarity(a: list[str], b: list[str]) -> float:
    sa, sb = {surname(x) for x in a if x}, {surname(x) for x in b if x}
    if not sa or not sb:
        return 0.0
    best = max(fuzz.ratio(x, y) for x in sa for y in sb)
    return best / 100


def clean_author(name: str) -> str:
    name = re.sub(r"\s*\[[^\]]*\]\s*", " ", name)  # "David Attenborough [Attenborough, David]"
    name = _TRANSLATOR.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" ,;")
    if name.count(",") == 1:
        last, first = [p.strip() for p in name.split(",")]
        if first and last and not re.search(r"\b(jr|sr|ii|iii|iv|inc|ltd)\.?$", first, re.I):
            name = f"{first} {last}"
    if name.isupper() or name.islower():
        name = " ".join(w.capitalize() if len(w) > 2 else w.upper() if "." in w else w.capitalize() for w in name.split())
    return name


def _full_name(s: str) -> bool:
    """Two or more words that aren't initials: 'Robin Buss' yes, 'Kazuo' / 'Ursula K.' no."""
    return len([w for w in s.split() if not re.fullmatch(r"[A-Z]\.?", w)]) >= 2


def _split_names(raw: str) -> list[str]:
    if ";" in raw or " & " in raw:
        return re.split(r"\s*(?:;|&|\band\b)\s*", raw)
    # "Alexandre Dumas, Robin Buss" is a list; "Ishiguro, Kazuo" and "Le Guin, Ursula K." are one name.
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) > 1 and all(_full_name(p) for p in parts) and not re.search(r"\b(jr|sr|ii|iii|iv|inc|ltd)\.?$", raw, re.I):
        return parts
    return [raw]


def clean_authors(authors: list[str]) -> list[str]:
    out, seen = [], set()
    for raw in authors:
        for part in _split_names(raw):
            name = clean_author(part)
            key = (surname(name), strip_accents(name).lower().replace(".", "").replace(" ", "")[:1])
            if name and key not in seen:
                seen.add(key)
                out.append(name)
    return out


def split_series(title: str) -> tuple[str, str, str]:
    """'My Brilliant Friend (The Neapolitan Quartet Book 1)' -> (title, series, index)."""
    m = _SERIES_IN_TITLE.search(title)
    if not m or not m.group("series").strip():
        return title, "", ""
    return title[: m.start()].strip(), m.group("series").strip(" ,:-"), m.group("index")


def clean_title(title: str, series: str = "") -> str:
    t = re.sub(r"\s+", " ", title).strip()
    t = _NOVEL_SUFFIX.sub("", t)
    t2, s, _ = split_series(t)
    if s:
        t = t2
    if series and t.lower().endswith(f"({series.lower()})"):
        t = t[: -len(series) - 2].strip()
    # "Title: Subtitle: Subtitle" or "Title: Title"
    parts = [p.strip() for p in t.split(":")]
    deduped = [p for i, p in enumerate(parts) if p and p.lower() not in (q.lower() for q in parts[:i])]
    return ": ".join(deduped)


def looks_broken_title(title: str) -> bool:
    return (
        not title.strip()
        or bool(re.search(r"\.(epub|mobi|azw3?|pdf)$|\s--\s|_{1,}|\bz-?lib|\banna.?s archive|^unknown$", title, re.I))
        or (title.isupper() and len(title) > 4)
    )


def clean_subjects(subjects: list[str]) -> list[str]:
    out, seen = [], set()
    for s in subjects:
        for part in re.split(r"\s*[/;]\s*", s):
            part = part.strip()
            key = part.lower()
            if part and key not in seen and key not in {"general", "fiction / general", "books"} and len(part) < 40:
                seen.add(key)
                out.append(part)
    return out[:8]
