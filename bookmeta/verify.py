"""Per-book pipeline: read the EPUB, look it up, verify identity with Jev, propose field changes."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from . import covers
from .describe import KINDS, DescriptionCleaner
from .epub import Epub, find_isbns, read_epub
from .haiku import Haiku, HaikuError, in_records, trim_slop
from .jev import Jev, JevError
from .normalize import (
    author_similarity,
    clean_authors,
    clean_subjects,
    clean_title,
    looks_broken_title,
    split_series,
    title_similarity,
)
from .sources import Candidate, gather

AUTO_IDENTITY = 0.85  # Jev's "same book" degree needed to write without review
MIN_IDENTITY = 0.5
DESC_MIN = 200

# Which sources to trust first for each field (ISBN-matched candidates always win).
PRIORITY = {
    "description": ["apple", "google", "hardcover", "openlibrary"],
    "series": ["hardcover", "google"],
    "publisher": ["google", "hardcover", "openlibrary"],
    "pubdate": ["google", "apple", "hardcover", "openlibrary"],
    "language": ["google"],
    "subjects": ["hardcover", "google", "apple"],
    "authors": ["openlibrary", "google", "hardcover", "apple"],
    "title": ["openlibrary", "google", "hardcover", "apple"],
}

STATUS_LABEL = {
    "pending": "·  pending",
    "flagged": "✓  already verified",
    "ok": "✓  good as-is",
    "auto": "→  auto-apply",
    "review": "?  needs review",
    "nomatch": "✗  no match",
    "error": "!  error",
    "applied": "✓  updated",
    "kept": "✓  kept current",
}


@dataclass
class Change:
    field: str
    current: object
    proposed: object
    source: str
    auto: bool
    reason: str
    accepted: bool = True


@dataclass
class Proposal:
    path: str
    size: int = 0
    mtime: float = 0.0
    status: str = "pending"
    identity: float = 0.0
    title: str = ""
    authors: str = ""
    matched: str = ""
    changes: list[Change] = field(default_factory=list)
    cover_file: str = ""
    cover_options: list[dict] = field(default_factory=list)  # current + candidates, for comparing with `o`
    flag: str = ""  # the book's bookmeta:status when it was analysed
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Proposal":
        d = dict(d)
        d["changes"] = [Change(**c) for c in d.get("changes", [])]
        return cls(**d)


def _cache_cover(ctx: "Context", c: covers.CoverInfo) -> str:
    fn = ctx.cache_dir / (hashlib.sha1(c.data).hexdigest()[:16] + ".img")
    if not fn.exists():
        fn.write_bytes(c.data)
    return str(fn)


def _cover_decision(current, fetched, options, verdict, by_isbn: bool, prop) -> covers.CoverDecision:
    """Haiku vetoes candidates it flags (movie tie-in, adverts, placeholder, wrong book, poor scan) and may
    prefer other artwork; anything that isn't a larger copy of the current art goes to review."""
    if not verdict:
        return covers.decide(current, fetched, by_isbn)
    same = lambda a, b: a is not None and b is not None and covers.distance(a.dhash, b.dhash) <= covers.SAME_ART_MAX_DISTANCE
    off = 1 if current else 0
    vetoed = [options[i - off] for i, (problem, _) in verdict["verdicts"].items() if i >= off and problem != "ok"]
    usable = [c for c in fetched if not any(same(c, v) for v in vetoed)]
    decision = covers.decide(current, usable, by_isbn)
    why = verdict["why"]
    cur_problem = verdict["verdicts"].get(0, ("ok", ""))[0] if current else "ok"
    if cur_problem != "ok":
        prop.notes.append(f"Haiku: current cover looks like {cur_problem.replace('_', ' ')} - "
                          f"{verdict['verdicts'][0][1]}")
    best = verdict["best"]
    pick = options[best - off] if best >= off else current
    if any(pick is v for v in vetoed):
        return decision  # every option had a problem; don't swap one flawed cover for another on Haiku's say-so
    if decision.chosen and same(decision.chosen, pick):
        return decision  # rules and Haiku agree
    if same(pick, current):
        if decision.chosen:  # rules wanted other artwork; Haiku prefers what's there
            prop.notes.append(f"Haiku prefers the current artwork: {why}")
        bigger = pick is not current and pick.height >= current.height * covers.UPGRADE_FACTOR
        return covers.CoverDecision(pick if bigger else None, True,
                                    f"same artwork, {current.height}px -> {pick.height}px" if bigger else "current cover kept")
    if current and pick.height < current.height:
        return decision  # never trade down in resolution on Haiku's taste alone
    return covers.CoverDecision(pick, False, f"Haiku prefers this cover: {why}")


@dataclass
class Context:
    http: httpx.AsyncClient
    jev: Jev
    cache_dir: Path
    haiku: Haiku | None = None
    include_verified: bool = False
    covers_only: bool = False  # --covers: identify the book, then look at nothing but its cover


def filename_hints(path: Path) -> tuple[str, str, list[str]]:
    """Anna's Archive ('Title -- Author -- ...') and Calibre-style ('Title, The - Author') names."""
    name = re.sub(r"(\.kepub)?\.epub$", "", path.name, flags=re.I)
    isbns = find_isbns(" ".join(re.findall(r"97[89]\d{10}", name)))
    if " -- " in name:
        parts = name.split(" -- ")
        title, author = parts[0], parts[1] if len(parts) > 1 else ""
        author = re.sub(r"\s*\[.*?\]", "", author)
    elif " - " in name:
        title, author = name.rsplit(" - ", 1)
    else:
        title, author = name, ""
    title = title.replace("_ ", ": ").replace("_", "'")
    m = re.match(r"^(.*), (The|A|An)(:.*)?$", title)
    if m:
        title = f"{m.group(2)} {m.group(1)}{m.group(3) or ''}"
    return title.strip(), author.strip(), isbns


def _match(title: str, authors: list[str], c: Candidate) -> tuple[bool, bool, float]:
    full = f"{c.title}: {c.subtitle}" if c.subtitle else c.title
    ts = max(title_similarity(title, c.title), title_similarity(title, full))
    au = author_similarity(authors, c.authors) if authors else 1.0
    swapped = bool(authors) and title_similarity(" ".join(authors), c.title) > 0.85 and author_similarity([title], c.authors) > 0.85
    ok = (ts >= 0.85 and au >= 0.8) or (c.by_isbn and ts >= 0.6 and au >= 0.6) or swapped
    return ok, swapped, ts


def _pick(cands: list[Candidate], fieldname: str, attr: str | None = None) -> Candidate | None:
    attr = attr or fieldname
    order = PRIORITY.get(fieldname, [])
    ranked = sorted(cands, key=lambda c: (not c.by_isbn, order.index(c.source) if c.source in order else 99))
    for c in ranked:
        if getattr(c, attr):
            return c
    return None


def _book_context(book: Epub, title: str, authors: list[str]) -> str:
    excerpt = re.sub(r"\s+", " ", book.front_text)[:900]
    return (
        f"EBOOK FILE: title={title!r}; authors={authors}; filename={book.path.name!r}; "
        f"ISBNs printed inside the book={book.text_isbns[:3]}.\n"
        f"Opening text of the ebook (title page/copyright): {excerpt}"
    )


async def _haiku_pass(haiku: Haiku, book: Epub, matched: list[Candidate], changes: list[Change], trusted: bool, prop: Proposal) -> None:
    m = book.meta
    final = {f: getattr(m, f) for f in ("title", "authors", "series", "series_index", "publisher", "pubdate", "language")}
    for ch in changes:
        if ch.field in final:
            final[ch.field] = ch.proposed
    desc_change = next((ch for ch in changes if ch.field == "description"), None)
    desc = desc_change.proposed if desc_change else m.description
    result = await haiku.review(
        final, desc or "", [c.summary() for c in matched][:5], book.path.name, re.sub(r"\s+", " ", book.front_text)[:900]
    )

    formatted = (result.get("description") or "").strip()
    if desc and formatted != desc:
        if not formatted:
            prop.notes.append("Haiku flagged the whole description as slop; kept it for review rather than drop it blind")
        else:
            trimmed = trim_slop(desc, formatted)
            if trimmed is None:
                prop.notes.append("Haiku's description edit changed wording, not just formatting/slop; ignored")
            else:
                clean, dropped = trimmed
                reason = f"removed {dropped} slop paragraph(s)" if dropped > 0 else "formatting fixed"
                if desc_change:
                    desc_change.proposed = clean
                    desc_change.reason += f" + {reason}"
                else:
                    changes.append(Change("description", m.description, clean, "haiku", trusted, reason))

    for issue in result.get("issues", []):
        fld, suggested = issue["field"], issue["suggested"].strip()
        value = [a.strip() for a in suggested.split(";") if a.strip()] if fld == "authors" else suggested
        if not value or value == final.get(fld):
            continue
        backed = in_records(fld, value, matched)
        prop.notes.append(f"Haiku: {fld}: {issue['problem']}" + ("" if backed else " (not in any source record)"))
        changes[:] = [ch for ch in changes if ch.field != fld]
        changes.append(Change(fld, getattr(m, fld), value, "haiku", trusted and backed, issue["problem"]))


async def analyze(path: Path, ctx: Context) -> Proposal:
    st = path.stat()
    prop = Proposal(path=str(path), size=st.st_size, mtime=st.st_mtime)
    try:
        book = await asyncio.to_thread(read_epub, path)
    except Exception as e:
        prop.status, prop.error = "error", f"unreadable EPUB: {e}"
        return prop
    m = book.meta
    prop.title, prop.authors, prop.flag = m.title, ", ".join(m.authors), book.flag or ""
    if book.flag and not (ctx.include_verified or ctx.covers_only):
        prop.status = "flagged"
        return prop

    changes: list[Change] = []

    # 1. Deterministic clean-ups of what's already there.
    fn_title, fn_author, fn_isbns = filename_hints(path)
    broken = looks_broken_title(m.title)
    title_for_lookup = fn_title if broken else m.title
    authors = clean_authors(m.authors) or clean_authors([fn_author] if fn_author else [])
    if m.authors and authors != m.authors:
        changes.append(Change("authors", m.authors, authors, "cleanup", True, "normalised / de-duplicated names"))

    series, series_index = m.series, m.series_index
    if not broken:
        t = clean_title(m.title, series)
        _, s_from_title, i_from_title = split_series(m.title)
        if s_from_title and not series:
            series, series_index = s_from_title, i_from_title
            changes.append(Change("series", "", s_from_title, "title", True, "series name was embedded in the title"))
            changes.append(Change("series_index", "", i_from_title, "title", True, "series number from title"))
        if t != m.title:
            changes.append(Change("title", m.title, t, "cleanup", True, "removed series tag / 'A Novel' / repeated text"))
        title_for_lookup = t

    if m.subjects:
        subj = clean_subjects(m.subjects)
        if subj != m.subjects:
            changes.append(Change("subjects", m.subjects, subj, "cleanup", True, "de-duplicated subjects"))

    # 2. Look the book up everywhere, ISBN first.
    isbns = list(dict.fromkeys([i for i in [m.isbn, *book.text_isbns, *fn_isbns] if i]))
    cands = await gather(ctx.http, title_for_lookup, authors[0] if authors else "", isbns)
    matched, swapped_any = [], False
    for c in cands:
        ok, swapped, _ = _match(title_for_lookup, authors, c)
        if ok:
            matched.append(c)
            swapped_any |= swapped
    by_isbn = any(c.by_isbn for c in matched)

    # 3. One batched Jev request: identity + description paragraph classification.
    cleaners: dict[str, DescriptionCleaner] = {}
    if m.description:
        cleaners["current"] = DescriptionCleaner(m.description)
    seen_desc = set()
    for c in sorted(matched, key=lambda c: (not c.by_isbn, PRIORITY["description"].index(c.source))):
        key = c.description[:80]
        if len(c.description) >= DESC_MIN and key not in seen_desc and len(cleaners) < 3:
            seen_desc.add(key)
            cleaners[f"{c.source}{len(cleaners)}"] = DescriptionCleaner(c.description)
            cleaners[f"{c.source}{len(cleaners) - 1}"].candidate = c  # type: ignore[attr-defined]

    if ctx.covers_only:
        cleaners.clear()  # no description work, so no paragraph questions for Jev

    questions: dict[str, dict] = {}
    best = matched[0] if matched else None
    if matched:
        best = max(matched, key=lambda c: (_match(title_for_lookup, authors, c)[2], c.by_isbn, -PRIORITY["title"].index(c.source)))
        agreeing = sorted({c.source for c in matched})
        others = [c.summary() for c in matched if c is not best][:3]
        questions["identity"] = Jev.noul_q(
            "Are the ebook file and the catalogue record the same book (same work by the same author)?",
            _book_context(book, title_for_lookup, authors)
            + f"\nCATALOGUE RECORD ({best.source}{', matched by ISBN' if best.by_isbn else ''}): {best.summary()}"
            + f"\nSources that agree: {', '.join(agreeing)}"
            + (f"\nOther matching records: {' || '.join(others)}" if others else ""),
        )
    para_keys: dict[str, tuple[str, int]] = {}
    for name, cl in cleaners.items():
        for i, p in cl.pending()[:12]:
            k = f"p_{name}_{i}"
            para_keys[k] = (name, i)
            questions[k] = Jev.choice_q(
                f"What kind of text is this paragraph from the description of the book '{title_for_lookup}'?",
                KINDS,
                p[:1200],
            )

    answers: dict = {}
    if questions and ctx.jev.available:
        try:
            answers = await ctx.jev.ask(questions)
        except JevError as e:
            prop.notes.append(f"Jev unavailable: {e}")

    if matched:
        try:
            identity = Jev.degree(answers["identity"])
        except (KeyError, JevError):
            # Without Jev, fall back to how strong the deterministic evidence is.
            identity = 0.9 if by_isbn and len({c.source for c in matched}) >= 2 else 0.75 if by_isbn else 0.6
            prop.notes.append("identity scored without Jev")
    else:
        identity = 0.0
    prop.identity = identity
    prop.matched = best.summary() if best else ""
    for k, (name, i) in para_keys.items():
        try:
            cleaners[name].record(i, Jev.probs(answers[k], KINDS))
        except (KeyError, JevError):
            pass

    trusted = identity >= AUTO_IDENTITY

    # 4. Field proposals from matched sources.
    if matched:
        if swapped_any:
            prop.notes.append("Title and author look SWAPPED in the file")
            c = _pick(matched, "title")
            changes = [ch for ch in changes if ch.field not in ("title", "authors")]
            changes.append(Change("title", m.title, clean_title(c.title), c.source, False, "title/author swapped"))
            changes.append(Change("authors", m.authors, clean_authors(c.authors), c.source, False, "title/author swapped"))
        elif broken:
            c = _pick(matched, "title")
            new = c.title + (f": {c.subtitle}" if c.subtitle and len(c.subtitle) < 70 else "")
            changes = [ch for ch in changes if ch.field != "title"]
            changes.append(Change("title", m.title, clean_title(new), c.source, trusted, "current title looks like a filename"))
        if not authors:
            c = _pick(matched, "authors")
            if c:
                changes.append(Change("authors", m.authors, clean_authors(c.authors[:3]), c.source, trusted, "authors missing"))

        if not series:
            c = _pick(matched, "series")
            if c:
                changes.append(Change("series", "", c.series, c.source, trusted, "series missing"))
                if c.series_index:
                    changes.append(Change("series_index", "", c.series_index, c.source, trusted, "series number missing"))

        for fld in ("publisher", "language"):
            if not getattr(m, fld):
                c = _pick(matched, fld)
                if c:
                    changes.append(Change(fld, "", getattr(c, fld), c.source, trusted, f"{fld} missing"))
        if not m.pubdate:
            c = _pick([c for c in matched if c.by_isbn] or matched, "pubdate")
            if c:
                changes.append(Change("pubdate", "", c.pubdate, c.source, trusted, "publication date missing"))
        if not m.isbn:
            confirmed = [i for i in isbns if any(i in c.isbns for c in matched)]
            if confirmed:
                changes.append(Change("isbn", "", confirmed[0], "lookup", trusted, "ISBN missing (confirmed by a source)"))
        if not m.subjects:
            c = _pick(matched, "subjects")
            if c:
                changes.append(Change("subjects", [], clean_subjects(c.subjects)[:5], c.source, trusted, "subjects missing"))

    # 5. Description: keep (cleaned) current if it's a real synopsis, else best cleaned source.
    cur = cleaners.get("current")
    cur_text, cur_conf = cur.result() if cur else ("", True)
    if len(cur_text) >= DESC_MIN:
        if cur_text != m.description:
            changes.append(Change("description", m.description, cur_text, "cleanup", cur_conf,
                                  "removed blurbs / marketing / repeats" if cur_conf else "uncertain paragraph removal"))
    else:
        for name, cl in cleaners.items():
            if name == "current":
                continue
            text, conf = cl.result()
            if len(text) >= DESC_MIN:
                src = cl.candidate.source  # type: ignore[attr-defined]
                changes.append(Change("description", m.description, text, src, trusted and conf,
                                      "description missing" if not m.description else "current description is mostly marketing"))
                break

    # 6. Haiku once-over: description formatting, and mangled/merged/missing fields Jev can't see.
    if ctx.haiku and ctx.haiku.available and not ctx.covers_only:
        try:
            await _haiku_pass(ctx.haiku, book, matched, changes, trusted, prop)
        except HaikuError as e:
            prop.notes.append(f"Haiku unavailable: {e}")

    if ctx.covers_only:
        changes.clear()
        prop.notes = [n for n in prop.notes if "Jev" in n]

    # 7. Cover: the deterministic upgrade rules, then Haiku compares the current cover with every candidate.
    if matched:
        current_cover = covers.inspect(book.cover_bytes, source="current")
        urls = list(dict.fromkeys((u, c.source) for c in matched for u in c.cover_urls))[:6]
        fetched = [f for f in await asyncio.gather(*(covers.fetch(ctx.http, u, s) for u, s in urls)) if f]
        options = covers.distinct(fetched)[:5]
        verdict = None
        if options and ctx.haiku and ctx.haiku.available:
            shown = ([current_cover] if current_cover else []) + options
            try:
                verdict = await ctx.haiku.pick_cover(
                    [(covers.thumbnail(c.data), f"{c.width}x{c.height}") for c in shown], bool(current_cover), m.title, ", ".join(m.authors))
            except (HaikuError, OSError) as e:
                prop.notes.append(f"Haiku cover check unavailable: {e}")
        decision = _cover_decision(current_cover, fetched, options, verdict, by_isbn, prop)
        if decision.chosen:
            prop.cover_file = _cache_cover(ctx, decision.chosen)
            changes.append(Change(
                "cover",
                current_cover.label if current_cover else "none",
                decision.chosen.label,
                decision.chosen.source,
                decision.auto and trusted,
                decision.reason,
            ))
        if options:
            off = 1 if current_cover else 0
            verdicts = verdict["verdicts"] if verdict else {}
            best = verdict["best"] if verdict else -1
            entries = ([(current_cover, "")] if current_cover else []) + [(c, _cache_cover(ctx, c)) for c in options]
            prop.cover_options = [
                {"file": f, "label": c.label, "source": c.source, "problem": verdicts.get(i, ("", ""))[0],
                 "note": verdicts.get(i, ("", ""))[1], "haiku_pick": i == best, "current": i < off}
                for i, (c, f) in enumerate(entries)
            ]

    prop.changes = changes
    if not matched or identity < MIN_IDENTITY:
        prop.status = "nomatch"
        for ch in changes:
            ch.auto = False
        if not matched:
            prop.notes.append(f"{len(cands)} results, none matched '{title_for_lookup}' by {authors}")
    elif ctx.covers_only and any(o["current"] and o["problem"] not in ("", "ok") for o in prop.cover_options):
        prop.status = "review"  # Haiku dislikes the current cover even if no candidate beats it: let the user look
    elif not changes and trusted:
        prop.status = "ok"
    elif trusted and all(ch.auto for ch in changes):
        prop.status = "auto"
    else:
        prop.status = "review"
    return prop
