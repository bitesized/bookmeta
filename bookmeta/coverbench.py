"""Replay the cover step on books already scanned, for judging cover choices by eye.

Each book's original file comes from the backups folder (or the library, if it's mounted and the book was never
written), runs through today's cover pipeline in covers-only mode, and gets a comparison sheet: every option with
its source, real size, Haiku's flag and score, the new pick, and the cover the earlier scan picked. Nothing is
written to any book or to the scan state. Open index.html in the output folder to look through them.

    uv run python -m bookmeta.coverbench [--root DATA_DIR] [--limit N] [--match TEXT] [--out DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import random
import re
from pathlib import Path

from . import covers, sources, store
from .epub import read_epub
from .haiku import Haiku
from .jev import Jev
from .sources import make_client
from .verify import Context, analyze


def _data_root(arg: Path | None) -> Path:
    if arg:
        return arg
    found = sorted(store.DATA_DIR.glob("*/state-covers.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not found:
        raise SystemExit(f"No state-covers.json under {store.DATA_DIR}; run a covers scan first or pass --root.")
    return found[0].parent


def _source_file(book: Path, backups: dict[str, Path]) -> Path | None:
    """The book as it was before bookmeta wrote it: its first backup, else the file itself."""
    return backups.get(book.name) or (book if book.exists() else None)


def _old_pick(prop: dict) -> str:
    ch = next((c for c in prop.get("changes", []) if c["field"] == "cover"), None)
    if ch:
        return f"{ch['proposed']} - {ch['reason']}"
    return "kept current" if prop.get("cover_options") else "no options"


async def bench(root: Path, limit: int, match: str, out: Path, seed: int) -> None:
    old = json.loads((root / "state-covers.json").read_text())
    backups: dict[str, Path] = {}
    for p in sorted((root / "backups").rglob("*.epub"), key=lambda p: p.name):
        backups.setdefault(p.name, p)
    books = [(Path(k), v) for k, v in old.items() if v.get("cover_options") and (not match or re.search(match, k, re.I))]
    random.Random(seed).shuffle(books)
    books = [(b, v, f) for b, v in books if (f := _source_file(b, backups))][:limit]
    if not books:
        raise SystemExit("No matching books with a backup or a reachable file.")
    out.mkdir(parents=True, exist_ok=True)
    print(f"Replaying {len(books)} book(s) from {root}; sheets in {out}")

    rows: list[str] = []
    async with make_client() as http:
        ctx = Context(http, Jev(http), root / "covers", Haiku(), include_verified=True, covers_only=True)
        if not ctx.haiku.available:
            print("No ANTHROPIC_API_KEY: scoring on resolution and source only.")
        sem = asyncio.Semaphore(3)

        async def one(n: int, book: Path, before: dict, src: Path) -> None:
            async with sem:
                prop = await analyze(src, ctx)
            change = next((c for c in prop.changes if c.field == "cover"), None)
            new = f"{change.proposed} - {change.reason}" + ("" if change.auto else " [review]") if change else "keep current"
            current = read_epub(src).cover_bytes
            images = []
            for i, o in enumerate(prop.cover_options):
                data = current if o["current"] else Path(o["file"]).read_bytes()
                cap = f"{i + 1}. {o['label']}" + (" CURRENT" if o["current"] else "")
                flag = o["problem"] if o["problem"] not in ("", "ok") else ""
                score = f"score {o['score']:.1f}" if o.get("score") is not None else "vetoed"
                picked = (change and o["file"] == prop.cover_file) or (not change and o["current"])
                images.append((data, f"{cap}\n{score} {flag}" + ("\n>> NEW PICK" if picked else "")))
            old_file = next((o["file"] for o in before.get("cover_options", []) if before.get("cover_file") == o["file"]), "")
            if old_file and Path(old_file).exists():
                images.append((Path(old_file).read_bytes(), "earlier pick"))
            name = f"{n:03d}.jpg"
            if images:
                (out / name).write_bytes(covers.montage(images, height=520))
            notes = "; ".join(prop.notes)
            rows.append(f"<section><h2>{n}. {html.escape(prop.title or book.name)}</h2>"
                        f"<p><b>new:</b> {html.escape(new)}<br><b>earlier:</b> {html.escape(_old_pick(before))}"
                        + (f"<br><small>{html.escape(notes)}</small>" if notes else "")
                        + (f"</p><img src='{name}' loading='lazy'></section>" if images else "</p></section>"))
            print(f"{n:3d}. {prop.title[:50]:50s}  {new[:70]}")

        await asyncio.gather(*(one(n, b, v, f) for n, (b, v, f) in enumerate(books, 1)))

    rows.sort(key=lambda r: int(re.search(r"<h2>(\d+)\.", r).group(1)))
    disabled = "".join(f"<p><b>{k}</b> unavailable: {html.escape(v)}</p>" for k, v in sources.DISABLED.items())
    (out / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Cover replay</title><style>body{font:15px system-ui;margin:24px}"
        "img{max-width:100%;border:1px solid #ccc}section{margin-bottom:40px}</style>"
        f"<h1>Cover replay ({len(rows)} books)</h1>{disabled}" + "".join(rows))
    print(f"Open {out / 'index.html'}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="coverbench", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, help="bookmeta data folder for the library (default: the latest covers scan)")
    ap.add_argument("--limit", type=int, default=20, help="books to replay (default 20)")
    ap.add_argument("--match", default="", help="only books whose path matches this regex")
    ap.add_argument("--seed", type=int, default=0, help="which random sample of books to take")
    ap.add_argument("--out", type=Path, default=Path("coverbench-out"))
    args = ap.parse_args()
    asyncio.run(bench(_data_root(args.root), args.limit, args.match, args.out, args.seed))


if __name__ == "__main__":
    main()
