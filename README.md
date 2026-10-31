# bookmeta

Fetch, verify and fix EPUB metadata for a KOReader library — without the Calibre-style damage
(worse covers, swapped title/author, good metadata replaced by worse).

```
uv run bookmeta /Volumes/KOBOeReader/.adds/koreader/books
```

The library is scanned recursively. Each EPUB is looked up (by ISBN first, then title/author) in
Apple Books, Open Library, Google Books and Hardcover, the match is verified by Jev, and the
result lands in one of these states:

| Status | Meaning |
|---|---|
| ✓ already verified | carries the `bookmeta:status` flag from an earlier run — skipped |
| ✓ good as-is / → auto-apply | confidently identified and every change is safe; written automatically and flagged |
| ? needs review | something needs a human: a different cover edition, an uncertain description edit, weaker identity |
| ✗ no match | nothing matched confidently; nothing is proposed for writing |

## What it changes, and what it never does

- **Missing fields** are filled: description, series, ISBN, publisher, date, language, subjects.
- **Covers** are replaced automatically only by a *larger copy of the same artwork* (perceptual
  hash match), or when the current cover is missing/tiny and two sources agree. A different
  edition's artwork always goes to review. Covers are stored at most 2000px tall.
- **Descriptions** are split into paragraphs; praise quotes, bestseller/award banners, film/TV
  tie-ins, "about the author", pasted first chapters and repeated paragraphs are removed (rules
  first, then Jev classifies the rest). If a dropped paragraph might have been synopsis, it goes
  to review.
- **Repeats** are cleaned: "Title: A Novel", series tags in titles, "Last, First" authors,
  duplicate authors/subjects.
- **Haiku once-over** (optional, `ANTHROPIC_API_KEY`): after Jev, Claude Haiku gives the description a
  second pass — fixing formatting (paragraph breaks, run-together words, encoding junk) and dropping
  whole paragraphs Jev's rules missed (praise quotes, movie/TV tie-ins, author bios, "also by" lists,
  discussion-guide notices, repeated paragraphs) — and checks the final metadata against the source
  records for misspelled or mangled titles/names, merged authors, translators listed as authors and
  missing fields. A description edit is verified paragraph-by-paragraph before it's trusted: paragraphs
  may be dropped or split, but never reworded or invented, and the edit is ignored if it fails that
  check. Field fixes are auto-applied only when a source record states that exact value; otherwise
  everything goes to review.
- **Title and author are only replaced** when the current value is clearly broken (a filename,
  all-caps) or swapped — and swaps always go to review.
- Every rewrite is verified before it replaces the original, and the original is backed up once to
  `~/.local/share/bookmeta/<library>/backups/`. All changes are logged to `changes.jsonl` there.

## Keys

`s` scan · `a` apply the ticked changes · `k` keep the book as-is (flags it so it's skipped next
time) · `x` skip for this session · `tab` then `space`/`enter` toggle individual fields · `o` open
current and proposed covers in Preview · `f` cycle filter (all / review / anything needing you) ·
`r` re-check a book · `q` quit

Options: `--dry-run` (never write), `--no-auto` (review everything), `--recheck` (ignore cached
results), `--include-verified` (re-check books already flagged as verified/kept instead of skipping
them), `--force` (headless: re-scan and re-check the whole library with Jev and Haiku, ignoring all
cached state, printing plain progress instead of opening the TUI), `--stats PATH` (see below).

## Configuration (`.env` in the project folder, or `~/.config/bookmeta/.env`)

| Variable | |
|---|---|
| `TYPESAFE_API_KEY` | Jev, for identity checks and description cleaning. Without it, rules only and more goes to review. |
| `ANTHROPIC_API_KEY` | Optional Claude Haiku once-over (about $0.0005 a book). Without it, this step is skipped. `BOOKMETA_HAIKU_MODEL` overrides the model. |
| `GOOGLE_BOOKS_API_KEY` | Recommended (free, Google Cloud console). The keyless quota is shared and usually exhausted. |
| `HARDCOVER_API_KEY` (or `HARDCOVER_TOKEN`) | Recommended for **series** data (free, hardcover.app → Settings → API; expires yearly). |
| `BOOKMETA_CONTACT` | An email for Open Library's User-Agent; raises their rate limit from 1 to 3 req/s. |
| `BOOKMETA_APPLE_COUNTRY` | Apple Books storefront (default `us`; e.g. `gb`). |

## KOReader notes

- **Reading progress, highlights and KOSync are unaffected.** KOReader keys them on the
  `partial_md5_checksum` cached in each book's `.sdr` folder on first open, not on the current file.
- **Reading statistics** are keyed on title + authors + that checksum, so a title/author fix
  would start a new statistics row. Pass `--stats /Volumes/KOBOeReader/.adds/koreader/settings/statistics.sqlite3`
  to re-point the existing row instead (the database is backed up first). Do this with KOReader closed.
- Books with a `custom_metadata.lua` in their `.sdr` are flagged in the TUI: KOReader will keep
  showing the custom values over the file's.
- KOReader notices the changed files (size/mtime) and re-extracts covers and metadata in the
  cover browser by itself; if a cover looks stale, use *Refresh cached book information* on it.
- PDFs and other formats are listed as unsupported and left alone.
