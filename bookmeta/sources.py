"""Free metadata sources: Open Library, Google Books, Apple Books and (optionally) Hardcover."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from rapidfuzz import fuzz

from .describe import clean_html
from .epub import isbn_valid, to_isbn13
from .normalize import strip_accents

CONTACT = os.environ.get("BOOKMETA_CONTACT", "")
USER_AGENT = f"bookmeta/0.1 (personal KOReader library tool{'; ' + CONTACT if CONTACT else ''})"
GOOGLE_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "")
HARDCOVER_TOKEN = next((os.environ[k] for k in ("HARDCOVER_TOKEN", "HARDCOVER_API_TOKEN", "HARDCOVER_API_KEY") if os.environ.get(k)), "").strip()
# Apple storefronts: the first is used for metadata; all are searched for cover art (UK editions often differ).
APPLE_COUNTRIES = [c.strip().lower() for c in os.environ.get(
    "BOOKMETA_APPLE_COUNTRIES", os.environ.get("BOOKMETA_APPLE_COUNTRY", "us,gb")).split(",") if c.strip()] or ["us"]
APPLE_COUNTRY = APPLE_COUNTRIES[0]
APPLE_LOOKUP = "https://itunes.apple.com/lookup"
APPLE_BATCH = 20  # ISBNs per Apple lookup call
# Same folder as store.DATA_DIR (store imports this module, so it can't be imported here).
CACHE_FILE = Path(os.environ.get("BOOKMETA_HOME", Path.home() / ".local" / "share" / "bookmeta")) / "api-cache.sqlite3"
CACHE_DAYS = float(os.environ.get("BOOKMETA_API_CACHE_DAYS", "30"))


@dataclass
class Candidate:
    source: str
    title: str = ""
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    series: str = ""
    series_index: str = ""
    description: str = ""
    publisher: str = ""
    pubdate: str = ""
    language: str = ""
    isbns: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    cover_urls: list[str] = field(default_factory=list)
    by_isbn: bool = False  # found via an ISBN lookup rather than a text search

    def summary(self) -> str:
        bits = [f"{self.title}" + (f": {self.subtitle}" if self.subtitle else ""), "by " + ", ".join(self.authors)]
        if self.series:
            bits.append(f"series {self.series} #{self.series_index}".rstrip(" #"))
        if self.publisher or self.pubdate:
            bits.append(f"{self.publisher} {self.pubdate[:4]}".strip())
        return " | ".join(bits)


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1 / per_second
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def wait(self) -> None:
        async with self.lock:
            delay = self.last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last = time.monotonic()


LIMITS = {
    "openlibrary": RateLimiter(1 if not CONTACT else 2.5),
    "google": RateLimiter(2),
    "apple": RateLimiter(0.33),  # Apple allows ~20 calls/minute
    "hardcover": RateLimiter(0.9),
}


DISABLED: dict[str, str] = {}


class ResponseCache:
    """Catalogue answers kept on disk, so a re-scan doesn't wait on rate limits for questions already asked. Shared
    by every library; an answer older than CACHE_DAYS is asked again, and 0 turns the cache off."""

    def __init__(self, path: Path, days: float):
        self.path, self.max_age = path, days * 86400
        self.conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self.conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=5)
            self.conn.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, at REAL, body TEXT)")
        return self.conn

    def get(self, key: str):
        if self.max_age <= 0:
            return None
        try:
            row = self._db().execute("SELECT at, body FROM responses WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        return json.loads(row[1]) if row and time.time() - row[0] < self.max_age else None

    def put(self, key: str, data) -> None:
        if self.max_age <= 0:
            return
        try:
            with self._db() as conn:
                conn.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?)", (key, time.time(), json.dumps(data)))
        except sqlite3.Error:
            pass  # a cache that can't be written just means asking again next time


CACHE = ResponseCache(CACHE_FILE, CACHE_DAYS)


def cache_key(url: str, params: dict) -> str:
    """The request without its API key, which shouldn't sit in the cache and doesn't change the answer."""
    return json.dumps([url, sorted((k, str(v)) for k, v in params.items() if k != "key")], ensure_ascii=False)


async def _get_json(client: httpx.AsyncClient, limiter: str, url: str, waited: bool = False, cache: bool = True,
                    **kw) -> dict | None:
    """GET a JSON answer, from the cache if it has one. `waited`: the caller already took this call's rate-limit slot."""
    key = cache_key(url, kw.get("params") or {})
    if cache and (hit := CACHE.get(key)) is not None:
        return hit
    for attempt in range(3):
        if limiter in DISABLED:
            return None
        if attempt or not waited:
            await LIMITS[limiter].wait()
        try:
            r = await client.get(url, timeout=20, **kw)
        except httpx.HTTPError:
            continue
        if r.status_code == 429 and "per day" in r.text:
            DISABLED[limiter] = "daily quota exhausted" + ("" if GOOGLE_KEY else " (set GOOGLE_BOOKS_API_KEY)")
            return None
        if r.status_code in (429, 503):
            await asyncio.sleep(3 * (attempt + 1))
            continue
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        if cache:
            CACHE.put(key, data)
        return data
    return None


def _isbns(values) -> list[str]:
    out = []
    for v in values or []:
        d = re.sub(r"[^0-9Xx]", "", str(v))
        if isbn_valid(d):
            i = to_isbn13(d)
            if i not in out:
                out.append(i)
    return out


# --------------------------------------------------------------------------- Open Library

OL_FIELDS = "key,title,subtitle,author_name,first_publish_year,isbn,cover_i,publisher,language,subject"


async def openlibrary(client, title: str, author: str, isbn: str | None) -> list[Candidate]:
    params = {"fields": OL_FIELDS, "limit": 5}
    if isbn:
        params["isbn"] = isbn
    else:
        params.update(title=title, author=author, language="eng")
    data = await _get_json(client, "openlibrary", "https://openlibrary.org/search.json", params=params)
    if not isbn and not (data or {}).get("docs"):
        params.pop("language")  # nothing in English; widen rather than find nothing
        data = await _get_json(client, "openlibrary", "https://openlibrary.org/search.json", params=params)
    out = []
    for d in (data or {}).get("docs", [])[:5]:
        c = Candidate(
            source="openlibrary",
            title=d.get("title", ""),
            subtitle=d.get("subtitle", ""),
            authors=d.get("author_name", [])[:4],
            pubdate=str(d.get("first_publish_year", "")),
            isbns=[isbn] if isbn else _isbns(d.get("isbn", [])[:20]),
            by_isbn=bool(isbn),
        )
        if d.get("cover_i"):
            # No size suffix: the original upload; -L is capped at 500px tall.
            c.cover_urls.append(f"https://covers.openlibrary.org/b/id/{d['cover_i']}.jpg")
        out.append(c)
    return out


# --------------------------------------------------------------------------- Google Books


async def google(client, title: str, author: str, isbn: str | None) -> list[Candidate]:
    # Google returns nothing for quoted phrases, so fielded terms are unquoted, with a plain search as fallback.
    last = author.split()[-1] if author.split() else ""
    queries = [f"isbn:{isbn}"] if isbn else [f"intitle:{title}" + (f" inauthor:{last}" if last else ""), f"{title} {author}".strip()]

    async def search(restrict_lang: bool) -> dict | None:
        data = None
        for q in queries:
            params = {"q": q, "maxResults": 5, "printType": "books"}
            if restrict_lang:
                params["langRestrict"] = "en"
            if GOOGLE_KEY:
                params["key"] = GOOGLE_KEY
            data = await _get_json(client, "google", "https://www.googleapis.com/books/v1/volumes", params=params)
            if data and data.get("items"):
                break
        return data

    data = await search(restrict_lang=not isbn)
    if not isbn and not (data or {}).get("items"):
        data = await search(restrict_lang=False)  # nothing in English; widen rather than find nothing
    out = []
    for item in (data or {}).get("items", [])[:5]:
        v = item.get("volumeInfo", {})
        c = Candidate(
            source="google",
            title=v.get("title", ""),
            subtitle=v.get("subtitle", ""),
            authors=v.get("authors", []),
            description=clean_html(v.get("description", "")),
            publisher=v.get("publisher", ""),
            pubdate=v.get("publishedDate", ""),
            language=v.get("language", ""),
            isbns=_isbns(i.get("identifier") for i in v.get("industryIdentifiers", [])),
            subjects=v.get("categories", []),
            by_isbn=bool(isbn),
        )
        # zoom=0 asks Google for the largest scan it has; tiny placeholders are filtered later.
        c.cover_urls.append(f"https://books.google.com/books/content?id={item['id']}&printsec=frontcover&img=1&zoom=0")
        out.append(c)
    return out


# --------------------------------------------------------------------------- Apple Books


def _resemblance(r: dict, title: str, author: str) -> float:
    norm = lambda s: strip_accents(s).lower()
    return (0.7 * fuzz.token_set_ratio(norm(title), norm(r.get("trackName", "")))
            + 0.3 * fuzz.token_set_ratio(norm(author), norm(r.get("artistName", ""))))


class AppleISBNBatcher:
    """Apple's rate limit is the slowest part of a scan, but its lookup takes many ISBNs at once, so the ISBN
    lookups of every book being scanned queue up together and go out as one call when Apple's turn comes. The
    answer doesn't say which ISBN each result was for, so each result goes to the queued book whose title and
    author it resembles most. Every result belongs to one of the ISBNs asked for, so only the relative match
    matters, and a misspelled title still finds its book."""

    def __init__(self):
        self.pending: dict[str, list[tuple[str, str, str, asyncio.Future]]] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def lookup(self, client, isbn: str, title: str, author: str, country: str) -> list[dict]:
        hit = CACHE.get(cache_key(APPLE_LOOKUP, {"isbn": isbn, "country": country}))
        if hit is not None:
            return hit.get("results", [])
        fut = asyncio.get_running_loop().create_future()
        self.pending.setdefault(country, []).append((isbn, title, author, fut))
        if country not in self.tasks:
            self.tasks[country] = asyncio.create_task(self._flush(client, country))
        return await fut

    async def _flush(self, client, country: str) -> None:
        queue = self.pending[country]
        try:
            while queue:
                await LIMITS["apple"].wait()  # the queue keeps filling while this waits its turn
                batch = queue[:APPLE_BATCH]
                del queue[:len(batch)]
                found: dict[str, list[dict]] = {}
                try:
                    found = await self._ask(client, country, batch)
                finally:
                    for isbn, _, _, fut in batch:
                        if not fut.done():
                            fut.set_result(found.get(isbn, []))
        finally:
            del self.tasks[country]

    async def _ask(self, client, country: str, batch: list) -> dict[str, list[dict]]:
        isbns = list(dict.fromkeys(i for i, *_ in batch))
        data = await _get_json(client, "apple", APPLE_LOOKUP, waited=True, cache=False,
                               params={"isbn": ",".join(isbns), "country": country})
        if data is None:
            return {}
        found: dict[str, list[dict]] = {i: [] for i in isbns}
        for r in data.get("results", []):
            if len(isbns) == 1:
                found[isbns[0]].append(r)
            else:
                best = max(batch, key=lambda b: _resemblance(r, b[1], b[2]))
                found[best[0]].append(r)
        for isbn, results in found.items():  # cached as if each ISBN had been looked up alone
            CACHE.put(cache_key(APPLE_LOOKUP, {"isbn": isbn, "country": country}), {"results": results})
        return found


APPLE_ISBNS = AppleISBNBatcher()


async def apple(client, title: str, author: str, isbn: str | None, country: str = APPLE_COUNTRY) -> list[Candidate]:
    if isbn:
        results = await APPLE_ISBNS.lookup(client, isbn, title, author, country)
    else:
        term = f"{title} {author}".strip()
        data = await _get_json(
            client, "apple", "https://itunes.apple.com/search",
            params={"term": term, "media": "ebook", "entity": "ebook", "limit": 5, "country": country},
        )
        results = (data or {}).get("results", [])
    out = []
    for r in results[:5]:
        if r.get("kind") not in (None, "ebook"):
            continue
        c = Candidate(
            source="apple",
            title=r.get("trackName", ""),
            authors=[a.strip() for a in re.split(r"\s*(?:&|,| and )\s*", r.get("artistName", "")) if a.strip()],
            description=clean_html(r.get("description", "")),
            pubdate=(r.get("releaseDate") or "")[:10],
            subjects=[g for g in r.get("genres", []) if g != "Books"],
            by_isbn=bool(isbn),
            isbns=[isbn] if isbn else [],
        )
        art = r.get("artworkUrl100") or r.get("artworkUrl60")
        if art:
            c.cover_urls.append(re.sub(r"/\d+x\d+(bb)?\.(jpg|png)$", "/10000x10000bb.jpg", art))
        out.append(c)
    return out


# --------------------------------------------------------------------------- Hardcover

HC_QUERY = "query($q: String!) { search(query: $q, query_type: \"Book\", per_page: 5) { results } }"


async def hardcover(client, title: str, author: str, isbn: str | None) -> list[Candidate]:
    if not HARDCOVER_TOKEN:
        return []
    token = HARDCOVER_TOKEN if HARDCOVER_TOKEN.lower().startswith("bearer ") else f"Bearer {HARDCOVER_TOKEN}"
    q = isbn or f"{title} {author}".strip()
    key = cache_key("https://api.hardcover.app/v1/graphql", {"q": q})
    results = CACHE.get(key)
    if results is None:
        await LIMITS["hardcover"].wait()
        try:
            r = await client.post(
                "https://api.hardcover.app/v1/graphql",
                json={"query": HC_QUERY, "variables": {"q": q}},
                headers={"authorization": token},
                timeout=30,
            )
            results = r.json()["data"]["search"]["results"]
        except Exception:
            return []
        if isinstance(results, str):
            results = json.loads(results)
        CACHE.put(key, results)
    out = []
    for hit in (results or {}).get("hits", [])[:5]:
        d = hit.get("document", {})
        fs = d.get("featured_series") or {}
        series = (fs.get("series") or {}).get("name", "") if isinstance(fs, dict) else ""
        pos = fs.get("position") if isinstance(fs, dict) else None
        if not series and d.get("series_names"):
            series = d["series_names"][0]
        c = Candidate(
            source="hardcover",
            title=d.get("title", ""),
            subtitle=d.get("subtitle") or "",
            authors=d.get("author_names", [])[:4],
            series=series or "",
            series_index=_fmt_index(pos),
            description=clean_html(d.get("description") or ""),
            pubdate=str(d.get("release_year") or ""),
            isbns=_isbns(d.get("isbns", [])[:30]),
            subjects=(d.get("genres") or [])[:5],
            by_isbn=bool(isbn),
        )
        img = (d.get("image") or {}).get("url")
        if img:
            c.cover_urls.append(img)
        out.append(c)
    return out


def _fmt_index(pos) -> str:
    if pos in (None, ""):
        return ""
    try:
        f = float(pos)
    except (TypeError, ValueError):
        return str(pos)
    return str(int(f)) if f.is_integer() else str(f)


# --------------------------------------------------------------------------- Amazon (cover art only)


def isbn10(isbn13: str) -> str | None:
    """The ISBN-10 for a 978- ISBN-13; 979- numbers have none."""
    if len(isbn13) != 13 or not isbn13.startswith("978"):
        return None
    core = isbn13[3:12]
    check = (11 - sum((10 - k) * int(d) for k, d in enumerate(core)) % 11) % 11
    return core + ("X" if check == 10 else str(check))


def amazon_covers(isbns: list[str]) -> list[Candidate]:
    """Amazon has no open search, but its image server gives the publisher's original upload for an ISBN-10
    (`_SCRM` asks for full size; other modifiers cap it at 500px). An unknown ISBN returns a 1x1 GIF, which
    the cover checks throw away, so no lookup is needed first."""
    out = []
    for i in isbns:
        if (i10 := isbn10(i)):
            c = Candidate(source="amazon", isbns=[i], by_isbn=True)
            c.cover_urls.append(f"https://m.media-amazon.com/images/P/{i10}.01._SCRMZZZZZZ_.jpg")
            out.append(c)
    return out


SOURCES = {"openlibrary": openlibrary, "google": google, "apple": apple, "hardcover": hardcover}


def enabled_sources() -> list[str]:
    return [s for s in SOURCES if s != "hardcover" or HARDCOVER_TOKEN]


async def gather(client, title: str, author: str, isbns: list[str]) -> list[Candidate]:
    """Query every source, ISBN lookups first, falling back to title/author search."""

    async def one(name: str) -> list[Candidate]:
        fn = SOURCES[name]
        for isbn in isbns[:2]:
            found = await fn(client, title, author, isbn)
            if found:
                return found
        return await fn(client, title, author, None) if title else []

    results = await asyncio.gather(*(one(n) for n in enabled_sources()), return_exceptions=True)
    return [c for r in results if isinstance(r, list) for c in r]


async def more_covers(client, title: str, author: str, searched_title: bool) -> list[Candidate]:
    """Extra Apple results for cover art only: a title search in every storefront, since an ISBN lookup finds
    just the one edition. Skips the main storefront's title search when gather() already ran it."""
    if not title:
        return []
    countries = APPLE_COUNTRIES[1:] if searched_title else APPLE_COUNTRIES
    results = await asyncio.gather(*(apple(client, title, author, None, c) for c in countries), return_exceptions=True)
    return [c for r in results if isinstance(r, list) for c in r]


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, http2=False)
