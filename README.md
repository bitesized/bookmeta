# bookmeta

Fetch, verify and fix the metadata of the EPUBs in a KOReader library (titles, authors, series,
descriptions, covers) without the Calibre-style damage: worse covers, swapped title and author, or good
metadata replaced by worse.

## Quick start

1. Install [uv](https://docs.astral.sh/uv/), then clone this repository.
2. Create a `.env` file in the project folder with at least your Jev key:

   ```
   TYPESAFE_API_KEY=...
   ANTHROPIC_API_KEY=...      # optional: Haiku description clean-up and cover comparison
   GOOGLE_BOOKS_API_KEY=...   # optional, recommended
   HARDCOVER_API_KEY=...      # optional, recommended for series
   ```

3. Plug in your Kobo and run:

   ```
   uv run bookmeta
   ```

   With no folder given it opens the library you used last time, or the Kobo's
   `/Volumes/KOBOeReader/.adds/koreader/books`. You can also pass a folder: `uv run bookmeta ~/Books`.

4. Wait for the scan. Safe fixes are written automatically. Press `f` to show only the books that need you.
   For each one, read the right-hand pane, then press:
   - `a` to write the ticked changes, or untick a change first with `tab` and `space`
   - `k` to keep the book as it is
   - `o` to compare covers side by side and pick one with `1`–`9`
   - `m` to remove KOReader's custom metadata if the book has it (marked ⚠)

5. Eject the Kobo. KOReader picks up the changed files by itself.

Press `ctrl+p` for every other command, including covers-only mode, dry run and re-checking the whole
library.

### Keys

| Key | |
|---|---|
| `s` | scan (resumes where it left off) |
| `a` | apply the ticked changes to this book |
| `k` | keep the book as-is (flags it so it's skipped next time) |
| `x` | skip it for this session |
| `tab`, then `space`/`enter` | move to the change list and tick/untick a change |
| `o` | compare covers: opens a numbered sheet in Preview, then `1`–`9` picks one |
| `m` | remove KOReader's custom metadata from this book |
| `f` | filter: all / needs review / anything needing you |
| `r` | re-check this book |
| `c` | switch covers-only mode on/off |
| `v` | include already-verified books on/off |
| `ctrl+p` | command palette |
| `q` | quit |

### Command palette

`ctrl+p` opens a searchable list of commands. Type a few letters, for example `dry` or `cover`, and press
`enter`.

| Command | What it does |
|---|---|
| Covers-only mode | re-check only covers across the whole library, verified books included (same as `--covers`) |
| Include verified books | re-check books already flagged verified/kept (same as `--include-verified`) |
| Auto-apply | write safe changes automatically, or queue everything for review (`--no-auto`) |
| Dry run | never write to the books (`--dry-run`) |
| Statistics sync | keep KOReader reading statistics attached when a title or author changes (`--stats`); finds `statistics.sqlite3` next to the library |
| Re-check the library | ignore cached results and check every book again (`--recheck`) |
| Force re-check everything | ignore the cache and the verified flag, and run Jev and Haiku over every book (`--force`, without leaving the TUI) |
| Remove custom metadata from this book / from all books | see [KOReader custom metadata](#koreader-custom-metadata) |
| Compare covers | same as `o` |
| Filter | same as `f` |
| Open the data folder | backups, cached covers and the change log |
| Theme | pick a colour theme; it's remembered (see [Colours and themes](#colours-and-themes)) |
| Keys | show every key binding |

---

## In more detail

### How it works

The library folder is scanned recursively. Each EPUB is looked up, by ISBN first and then by title and
author, in Apple Books, Open Library, Google Books and Hardcover. Jev checks that the match really is the
same book and cleans the description. Then, if `ANTHROPIC_API_KEY` is set, Claude Haiku gives everything
a once-over and compares the covers. Each book ends up in one of these states:

| Status | Meaning |
|---|---|
| ✓ already verified | carries the `bookmeta:status` flag from an earlier run, so it's skipped |
| ✓ good as-is / → auto-apply | confidently identified and every change is safe; written automatically and flagged |
| ? needs review | something needs a human: a different cover edition, an uncertain description edit, weaker identity |
| ✗ no match | nothing matched confidently; nothing is proposed for writing |
| ✓ updated / ✓ kept current | written (or flagged as-is) in this session |

### Reading the screen

- The **Changes** column lists the fields that will be written (for books waiting on you) or were written
  (green, for updated books). ⚠ after a title means KOReader custom metadata or a swapped title/author.
- The right-hand pane starts with a summary: **Will write…**, **Written…**, **Not writing…** (unticked
  changes) or **Nothing to change**. Below it come notes from Jev and Haiku, then what bookmeta has done
  to this book in earlier sessions, from the change log.
- The change list shows each field's **Before** and **After**: ☑ will be written, ☐ won't, ✓ was written.
  Yellow fields need your check. Move onto a change to see its full before/after text and why it was
  proposed.

### What it changes, and what it never does

- **Missing fields** are filled: description, series, ISBN, publisher, date, language, subjects.
- **Covers** are replaced automatically only by a *larger copy of the same artwork* (perceptual hash
  match), or when the current cover is missing or tiny and two sources agree. A different edition's
  artwork always goes to review. Covers are stored at most 2000px tall.
- **Cover comparison** (with `ANTHROPIC_API_KEY`): Haiku looks at the current cover and every distinct
  candidate side by side and flags movie/TV tie-ins, adverts (bestseller banners, badges, review quotes),
  blank/Gutenberg/placeholder covers, wrong books and poor scans. Flagged candidates are never chosen, and
  Haiku picks the nicest-looking remaining cover even if it is from another edition. Its pick goes to
  review unless it is a larger copy of the current art, and it never proposes a smaller cover than the
  current one.
- **Descriptions** are split into paragraphs. Praise quotes, bestseller/award banners, film/TV tie-ins,
  "about the author", pasted first chapters and repeated paragraphs are removed: rules first, then Jev
  classifies the rest. If a dropped paragraph might have been synopsis, the book goes to review.
- **Repeats** are cleaned: "Title: A Novel", series tags in titles, "Last, First" authors, duplicate
  authors and subjects.
- **Haiku once-over** (optional, `ANTHROPIC_API_KEY`): after Jev, Claude Haiku gives the description a
  second pass. It fixes formatting (paragraph breaks, run-together words, encoding junk) and drops whole
  paragraphs Jev's rules missed (praise quotes, movie/TV tie-ins, author bios, "also by" lists,
  discussion-guide notices, repeated paragraphs). It also checks the final metadata against the source
  records for misspelled or mangled titles and names, merged authors, translators listed as authors and
  missing fields. A description edit is verified paragraph by paragraph before it's trusted: paragraphs
  may be dropped or split, but never reworded or invented, and the edit is ignored if it fails that check.
  Field fixes are auto-applied only when a source record states that exact value; otherwise they go to
  review.
- **Title and author are only replaced** when the current value is clearly broken (a filename, all caps)
  or swapped, and swaps always go to review.
- Every rewrite is verified before it replaces the original, and the original is backed up once.

### Covers-only mode

`--covers`, `c` or the palette re-checks only the covers of every book, verified ones included, so you can
upgrade the covers of a library that's already been through bookmeta. Other metadata and the verified flag
are left alone, and its results are cached separately from a full check.

### KOReader custom metadata

When you edit a book's details in KOReader (*Book information → Set custom …*), KOReader saves them in
`custom_metadata.lua` in the book's `.sdr` folder and shows those values instead of the file's. That
hides anything bookmeta writes. Such books are marked ⚠, and the right-hand pane lists the custom values.

`m` (or *Remove custom metadata from this book* in the palette) moves that file into the backups folder,
and *Remove custom metadata from all books* does every book after asking. The removal is logged. To undo
it, copy the backed-up file back into the book's `.sdr` folder as `custom_metadata.lua`. If KOReader still
shows the old values, long-press the book and use *Refresh cached book information*.

### Command-line options

All of these can also be switched from the command palette while the TUI is running.

| Option | |
|---|---|
| `--dry-run` | never write |
| `--no-auto` | review everything instead of auto-applying |
| `--recheck` | ignore cached results |
| `--include-verified` | re-check books already flagged as verified/kept instead of skipping them |
| `--covers` | start in covers-only mode |
| `--stats PATH` | KOReader's `statistics.sqlite3` to keep in sync (see below) |
| `--force` | headless, no TUI: re-check the whole library with Jev and Haiku, ignoring all cached state, and print plain progress |

### Configuration (`.env` in the project folder, or `~/.config/bookmeta/.env`)

| Variable | |
|---|---|
| `TYPESAFE_API_KEY` | Jev, for identity checks and description cleaning. Without it, rules only and more goes to review. |
| `ANTHROPIC_API_KEY` | Optional Claude Haiku once-over and cover comparison (about $0.001 a book). Without it, this step is skipped. `BOOKMETA_HAIKU_MODEL` overrides the model. |
| `GOOGLE_BOOKS_API_KEY` | Recommended (free, Google Cloud console). The keyless quota is shared and usually exhausted. |
| `HARDCOVER_API_KEY` (or `HARDCOVER_TOKEN`) | Recommended for **series** data (free, hardcover.app → Settings → API; expires yearly). |
| `BOOKMETA_CONTACT` | An email for Open Library's User-Agent; raises their rate limit from 1 to 3 req/s. |
| `BOOKMETA_APPLE_COUNTRY` | Apple Books storefront (default `us`; e.g. `gb`). |
| `BOOKMETA_HOME` | Where state, backups and the log are kept (default `~/.local/share/bookmeta`). |
| `BOOKMETA_CONFIG` | The settings file (default `~/.config/bookmeta/settings.json`). |

### Colours and themes

Pick a theme with *Theme* in the command palette (`ctrl+p`). It's saved and restored next time. Settings
live in `~/.config/bookmeta/settings.json`, which you can also edit by hand to define your own palette and
status colours:

```json
{
  "theme": "bookshelf",
  "themes": {
    "bookshelf": {
      "primary": "#C8A26B",
      "secondary": "#7A9E7E",
      "accent": "#E07A5F",
      "background": "#1E1B18",
      "surface": "#2A2622",
      "panel": "#3A332C",
      "foreground": "#EDE6DA",
      "success": "#8FBF7F",
      "warning": "#E9C46A",
      "error": "#E76F51",
      "dark": true
    }
  },
  "status_colors": {
    "review": "bold #E9C46A",
    "applied": "bold #8FBF7F",
    "nomatch": "#B392AC"
  }
}
```

- `theme` is the active theme: a built-in one (`textual-dark`, `textual-light`, `nord`, `gruvbox`,
  `dracula`, `tokyo-night`, `monokai`, `catppuccin-mocha`, `solarized-light` and more; *Theme* in the
  palette lists them) or one of yours.
- `themes` defines your own. `primary` is required; the rest are optional and derived from it when left
  out. Set `"dark": false` for a light theme. These are [Textual themes](https://textual.textualize.io/guide/design/).
- `status_colors` overrides the colour of a status in the book list: `pending`, `flagged`, `ok`, `auto`,
  `review`, `nomatch`, `error`, `applied`, `kept`, `skipped`. Values are Rich styles such as
  `"bold yellow"` or `"#FF8800"`.

The file also remembers the last library you opened.

### Files it keeps

Per library, in `~/.local/share/bookmeta/<library>-<hash>/` (*Open the data folder* in the palette):

- `backups/`: each book's original, copied once before the first rewrite, and removed
  `custom_metadata/` files
- `changes.jsonl`: every write and removal, with before/after values
- `covers/`: cached cover candidates
- `state.json`, `state-covers.json`: cached results, reused while a file's size and date are unchanged

### KOReader notes

- **Reading progress, highlights and KOSync are unaffected.** KOReader keys them on the
  `partial_md5_checksum` cached in each book's `.sdr` folder on first open, not on the current file.
- **Reading statistics** are keyed on title, authors and that checksum, so a title or author fix would start a
  new statistics row. Turn on *Statistics sync* (or pass
  `--stats /Volumes/KOBOeReader/.adds/koreader/settings/statistics.sqlite3`) to re-point the existing row
  instead. The database is backed up first. Do this with KOReader closed.
- KOReader notices the changed files (size/mtime) and re-extracts covers and metadata in the cover browser
  by itself. If a cover looks stale, use *Refresh cached book information* on it.
- PDFs and other formats are listed as unsupported and left alone.

## Licence

bookmeta is free software: you can redistribute it and/or modify it under the terms of the GNU General
Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
option) any later version. It is distributed WITHOUT ANY WARRANTY; see [LICENSE](LICENSE) for details.

SPDX-License-Identifier: GPL-3.0-or-later
