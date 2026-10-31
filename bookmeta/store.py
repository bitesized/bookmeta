"""Scan cache, applying proposals to files, the change log, and KOReader sidecar/statistics helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .describe import to_html
from .epub import read_epub, write_epub
from .verify import Proposal

DATA_DIR = Path(os.environ.get("BOOKMETA_HOME", Path.home() / ".local" / "share" / "bookmeta"))
SKIP_DIRS = {".sdr", ".kobo", ".adds", ".git"}


def library_dir(library: Path) -> Path:
    """Per-library folder for state, cached covers, backups and the change log."""
    h = hashlib.sha1(str(library.resolve()).encode()).hexdigest()[:8]
    d = DATA_DIR / f"{library.resolve().name}-{h}"
    for sub in ("covers", "backups"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def scan(library: Path) -> tuple[list[Path], list[Path]]:
    """Every EPUB below `library` (recursively), plus the unsupported files we saw."""
    books, other = [], []
    for dirpath, dirnames, filenames in os.walk(library):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and not d.endswith(".sdr") and d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.startswith(".") or name.startswith("._"):  # includes macOS AppleDouble files and our temp files
                continue
            p = Path(dirpath) / name
            if name.lower().endswith(".epub"):
                books.append(p)
            elif name.lower().endswith((".pdf", ".mobi", ".azw3", ".cbz", ".fb2", ".djvu")):
                other.append(p)
    return books, other


class State:
    """Proposals cached by path; an entry is reused only while the file's size and mtime are unchanged."""

    def __init__(self, root: Path, name: str = "state.json"):
        self.file = root / name
        try:
            raw = json.loads(self.file.read_text())
        except (OSError, ValueError):
            raw = {}
        self.items: dict[str, Proposal] = {}
        for k, v in raw.items():
            try:
                self.items[k] = Proposal.from_json(v)
            except TypeError:
                pass

    def get(self, path: Path) -> Proposal | None:
        p = self.items.get(str(path))
        if not p:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
        if p.size != st.st_size or abs(p.mtime - st.st_mtime) > 1e-3:
            return None
        if p.cover_file and not Path(p.cover_file).exists():
            return None
        return p

    def put(self, prop: Proposal) -> None:
        self.items[prop.path] = prop

    def save(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v.to_json() for k, v in self.items.items()}, ensure_ascii=False))
        os.replace(tmp, self.file)


def sidecar_dir(book: Path) -> Path:
    """KOReader's 'next to the book' sidecar: only the last extension is replaced (X.kepub.epub -> X.kepub.sdr)."""
    return book.with_name(book.name.rsplit(".", 1)[0] + ".sdr")


def custom_metadata(book: Path) -> bool:
    return (sidecar_dir(book) / "custom_metadata.lua").exists()


def partial_md5(book: Path) -> str | None:
    """The digest KOReader cached on first open; statistics and KOSync key on it."""
    sdr = sidecar_dir(book)
    for f in sdr.glob("metadata.*.lua"):
        if f.name.endswith(".old"):
            continue
        m = re.search(r'\["partial_md5_checksum"\]\s*=\s*"([0-9a-f]{32})"', f.read_text(errors="replace"))
        if m:
            return m.group(1)
    return None


def build_changes(prop: Proposal) -> tuple[dict, bytes | None]:
    changes: dict = {}
    cover = None
    for ch in prop.changes:
        if not ch.accepted:
            continue
        if ch.field == "cover":
            cover = Path(prop.cover_file).read_bytes() if prop.cover_file else None
        elif ch.field == "description":
            changes["description"] = to_html(ch.proposed)
        else:
            changes[ch.field] = ch.proposed
    return changes, cover


def apply(prop: Proposal, library: Path, root: Path, flag: str | None = "verified", stats_db: Path | None = None) -> Proposal:
    """Write accepted changes plus the flag. `flag='kept'` writes only the flag; `flag=None` writes the changes
    and leaves the book's flag as it was (cover-only runs)."""
    path = Path(prop.path)
    book = read_epub(path)
    old = {"title": book.meta.title, "authors": book.meta.authors}
    changes, cover = build_changes(prop) if flag != "kept" else ({}, None)
    backup = write_epub(book, changes, cover, flag, library, root / "backups")

    st = path.stat()
    prop.size, prop.mtime = st.st_size, st.st_mtime
    new = read_epub(path).meta
    prop.title, prop.authors = new.title, ", ".join(new.authors)
    prop.status = "applied" if flag != "kept" else "kept"
    applied = {k: (v if k != "description" else "<html>") for k, v in changes.items()}
    entry = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "path": str(path),
        "flag": flag if flag is not None else book.flag,
        "backup": str(backup),
        "changes": [
            {"field": c.field, "from": c.current, "to": c.proposed, "source": c.source}
            for c in prop.changes
            if c.accepted and flag != "kept"
        ],
        "fields_written": sorted(applied),
    }
    if stats_db and ("title" in changes or "authors" in changes):
        entry["stats"] = fix_statistics(stats_db, path, old, changes, root)
    with open(root / "changes.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return prop


def fix_statistics(db: Path, book: Path, old: dict, changes: dict, root: Path) -> str:
    """Point the existing reading-statistics row at the new title/authors so history isn't split."""
    md5 = partial_md5(book)
    if not md5:
        return "skipped: book never opened in KOReader"
    backup = root / "statistics.sqlite3.bak"
    if not backup.exists():
        shutil.copyfile(db, backup)
    new_title = changes.get("title", old["title"])
    new_authors = "\n".join(changes.get("authors", old["authors"]))
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT id, title, authors FROM book WHERE md5 = ?", (md5,)).fetchall()
        if len(rows) != 1:
            return f"skipped: {len(rows)} rows for md5"
        if conn.execute("SELECT 1 FROM book WHERE title = ? AND authors = ? AND md5 = ?", (new_title, new_authors, md5)).fetchone():
            return "skipped: target row already exists"
        conn.execute("UPDATE book SET title = ?, authors = ? WHERE id = ?", (new_title, new_authors, rows[0][0]))
    return f"updated row {rows[0][0]}"
