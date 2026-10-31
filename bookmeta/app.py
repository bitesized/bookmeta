"""Textual TUI: scan a KOReader library, verify every EPUB, auto-apply safe fixes and queue the rest for review."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

from . import store
from .epub import read_epub
from .haiku import Haiku
from .jev import Jev
from .sources import enabled_sources, make_client
from .verify import STATUS_LABEL, Context, Proposal, analyze

STATUS_STYLE = {
    "pending": "dim",
    "flagged": "green",
    "ok": "green",
    "applied": "bold green",
    "kept": "green",
    "auto": "cyan",
    "review": "bold yellow",
    "nomatch": "magenta",
    "error": "bold red",
    "skipped": "dim",
}
FILTERS = ["all", "review", "todo"]  # todo = anything that still needs a human
TODO = {"review", "nomatch", "error"}
CONCURRENCY = 4


def short(v, n: int = 60) -> str:
    s = ", ".join(v) if isinstance(v, list) else str(v or "")
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= n else s[: n - 1] + "…"


class BookMetaApp(App):
    TITLE = "bookmeta"
    CSS = """
    #books { width: 55%; }
    #side { width: 45%; border-left: solid $primary-darken-2; }
    #info { height: auto; padding: 0 1; }
    #changes { height: auto; max-height: 12; }
    #diffwrap { height: 1fr; }
    #diff { padding: 0 1; }
    """
    BINDINGS = [
        Binding("s", "start", "Scan"),
        Binding("a", "approve", "Apply"),
        Binding("k", "keep", "Keep as-is"),
        Binding("x", "skip", "Skip"),
        Binding("space", "toggle", "Toggle field"),
        Binding("o", "open_covers", "Covers"),
        Binding("f", "filter", "Filter"),
        Binding("r", "reanalyze", "Re-check"),
        Binding("tab", "focus_next", "Switch pane", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, library: Path, dry_run: bool, auto_apply: bool, stats_db: Path | None, recheck: bool, include_verified: bool = False):
        super().__init__()
        self.library = library.resolve()
        self.dry_run = dry_run
        self.auto_apply = auto_apply and not dry_run
        self.stats_db = stats_db
        self.recheck = recheck
        self.include_verified = include_verified
        self.root = store.library_dir(self.library)
        self.state = store.State(self.root)
        self.props: dict[str, Proposal] = {}
        self.order: list[str] = []
        self.unsupported: list[Path] = []
        self.filter = "all"
        self.running = False
        self.done = 0
        self.write_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="books", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield Static(id="info")
                yield DataTable(id="changes", cursor_type="row")
                with VerticalScroll(id="diffwrap"):
                    yield Static(id="diff")
        yield Footer()

    def on_mount(self) -> None:
        books = self.query_one("#books", DataTable)
        books.add_column("Status", key="status", width=20)
        books.add_column("Title", key="title", width=42)
        books.add_column("Author", key="author", width=22)
        books.add_column("Δ", key="n", width=3)
        ch = self.query_one("#changes", DataTable)
        ch.add_column("", key="on", width=3)
        ch.add_column("Field", key="field", width=12)
        ch.add_column("Proposed", key="proposed")
        ch.add_column("From", key="src", width=10)

        paths, self.unsupported = store.scan(self.library)
        for p in paths:
            prop = None if self.recheck else self.state.get(p)
            if prop is not None and self.include_verified and prop.status == "flagged":
                prop = None  # re-offer already-verified books instead of skipping them
            if prop is None:
                prop = Proposal(path=str(p), title=p.name)
            self.props[str(p)] = prop
            self.order.append(str(p))
        self.rebuild()
        mode = "DRY RUN — nothing will be written" if self.dry_run else ("auto-apply on" if self.auto_apply else "auto-apply off")
        sources = ", ".join(enabled_sources())
        self.notify(f"{len(paths)} EPUBs, {len(self.unsupported)} unsupported files skipped. Sources: {sources}. {mode}.", timeout=8)
        self.update_subtitle()
        self.action_start()

    # ---- table rendering -------------------------------------------------

    def visible(self, prop: Proposal) -> bool:
        if self.filter == "review":
            return prop.status == "review"
        if self.filter == "todo":
            return prop.status in TODO
        return True

    def row(self, prop: Proposal) -> tuple:
        st = Text(STATUS_LABEL.get(prop.status, prop.status), style=STATUS_STYLE.get(prop.status, ""))
        warn = " ⚠" if prop.notes and any("custom metadata" in n or "SWAPPED" in n for n in prop.notes) else ""
        pending = [c for c in prop.changes if c.accepted] if prop.status in ("review", "auto") else []
        return st, short(prop.title, 42) + warn, short(prop.authors, 22), str(len(pending) or "")

    def rebuild(self) -> None:
        t = self.query_one("#books", DataTable)
        cur = self.current_path()
        t.clear()
        for path in self.order:
            prop = self.props[path]
            if self.visible(prop):
                t.add_row(*self.row(prop), key=path)
        if cur and cur in t.rows:
            t.move_cursor(row=t.get_row_index(cur))
        self.show_detail()

    def refresh_row(self, prop: Proposal) -> None:
        t = self.query_one("#books", DataTable)
        if prop.path not in t.rows or not self.visible(prop):
            if self.visible(prop) or prop.path in t.rows:
                self.rebuild()
            self.update_subtitle()
            return
        for col, val in zip(("status", "title", "author", "n"), self.row(prop)):
            t.update_cell(prop.path, col, val)
        if prop.path == self.current_path():
            self.show_detail()
        self.update_subtitle()

    def update_subtitle(self) -> None:
        c = Counter(p.status for p in self.props.values())
        parts = [f"{self.done}/{len(self.props)} checked" if self.running else f"{len(self.props)} books"]
        for k in ("applied", "flagged", "ok", "auto", "review", "nomatch", "error"):
            if c[k]:
                parts.append(f"{k} {c[k]}")
        self.sub_title = f"{self.library.name}  ·  " + "  ".join(parts) + f"  ·  filter: {self.filter}"

    def current_path(self) -> str | None:
        t = self.query_one("#books", DataTable)
        if not t.row_count:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    def current(self) -> Proposal | None:
        p = self.current_path()
        return self.props.get(p) if p else None

    def show_detail(self) -> None:
        prop = self.current()
        info = self.query_one("#info", Static)
        ch = self.query_one("#changes", DataTable)
        ch.clear()
        if not prop:
            info.update("")
            self.query_one("#diff", Static).update("")
            return
        rel = Path(prop.path).relative_to(self.library)
        lines = [
            f"[b]{escape(prop.title)}[/b]",
            f"[dim]{escape(prop.authors)}[/dim]",
            f"[dim]{escape(str(rel))}[/dim]",
            f"Status: [{STATUS_STYLE.get(prop.status, '')}]{STATUS_LABEL.get(prop.status, prop.status)}[/]"
            + (f"   identity {prop.identity:.2f}" if prop.identity else ""),
        ]
        if prop.matched:
            lines.append(f"Matched: {escape(prop.matched)}")
        lines += [f"[yellow]• {escape(n)}[/yellow]" for n in prop.notes]
        if prop.error:
            lines.append(f"[red]{escape(prop.error)}[/red]")
        info.update("\n".join(lines))
        for i, c in enumerate(prop.changes):
            mark = Text("☑" if c.accepted else "☐", style="green" if c.accepted else "dim")
            fld = Text(c.field, style="" if c.auto else "yellow")
            ch.add_row(mark, fld, short(c.proposed, 200), c.source, key=str(i))
        self.show_change()

    def show_change(self) -> None:
        prop = self.current()
        diff = self.query_one("#diff", Static)
        ch = self.query_one("#changes", DataTable)
        if not prop or not prop.changes or not ch.row_count:
            diff.update("" if not prop or prop.changes else "[dim]No changes proposed.[/dim]")
            return
        c = prop.changes[min(ch.cursor_row, len(prop.changes) - 1)]
        fmt = lambda v: escape("\n".join(v) if isinstance(v, list) else str(v or "—"))
        diff.update(
            f"[b]{c.field}[/b] [dim]({c.source}; {escape(c.reason)}; {'auto' if c.auto else 'needs review'})[/dim]\n\n"
            f"[red]Current[/red]\n{fmt(c.current)}\n\n[green]Proposed[/green]\n{fmt(c.proposed)}"
        )

    @on(DataTable.RowHighlighted, "#books")
    def _book_moved(self) -> None:
        self.show_detail()

    @on(DataTable.RowHighlighted, "#changes")
    def _change_moved(self) -> None:
        self.show_change()

    @on(DataTable.RowSelected, "#changes")
    def _change_selected(self) -> None:
        self.action_toggle()

    # ---- scanning --------------------------------------------------------

    @work(exclusive=True, group="scan")
    async def action_start(self) -> None:
        if self.running:
            return
        todo = [p for p in self.order if self.props[p].status in ("pending", "auto", "ok")]
        if not todo:
            self.update_subtitle()
            return
        self.running, self.done = True, len(self.order) - len(todo)
        self.update_subtitle()
        async with make_client() as http:
            ctx = Context(http, Jev(http), self.root / "covers", Haiku(), self.include_verified)
            if not ctx.jev.available:
                self.notify("No TYPESAFE_API_KEY: identity checks fall back to rules and descriptions are only rule-cleaned.", severity="warning", timeout=10)
            sem = asyncio.Semaphore(CONCURRENCY)

            async def one(path: str) -> None:
                async with sem:
                    prop = self.props[path]
                    if prop.status == "pending":
                        try:
                            prop = await analyze(Path(path), ctx)
                        except Exception as e:  # one bad book must not stop the scan
                            prop.status, prop.error = "error", f"{type(e).__name__}: {e}"
                        if store.custom_metadata(Path(path)):
                            prop.notes.append("KOReader custom metadata (custom_metadata.lua) overrides the file's metadata")
                        self.props[path] = prop
                        self.state.put(prop)
                    if self.auto_apply and prop.status in ("auto", "ok"):
                        await self.write(prop, "verified")
                    self.done += 1
                    self.refresh_row(prop)
                    if self.done % 10 == 0:
                        self.state.save()

            await asyncio.gather(*(one(p) for p in todo))
        self.running = False
        self.state.save()
        self.update_subtitle()
        n = sum(p.status in TODO for p in self.props.values())
        self.notify(f"Scan finished. {n} book(s) need your attention — press f to filter." if n else "Scan finished. Nothing needs review.")

    async def write(self, prop: Proposal, flag: str) -> bool:
        if self.dry_run:
            self.notify("Dry run: not writing.", severity="warning")
            return False
        async with self.write_lock:
            try:
                await asyncio.to_thread(store.apply, prop, self.library, self.root, flag, self.stats_db)
            except Exception as e:
                prop.status, prop.error = "error", f"write failed: {type(e).__name__}: {e}"
                self.state.put(prop)
                return False
        self.state.put(prop)
        return True

    # ---- review actions --------------------------------------------------

    @work(group="write")
    async def action_approve(self) -> None:
        prop = self.current()
        if not prop or prop.status not in ("review", "auto", "ok", "nomatch"):
            return
        if prop.status == "nomatch" and not any(c.accepted for c in prop.changes):
            self.notify("No verified match; use k to mark it as kept.")
            return
        if await self.write(prop, "verified"):
            self.notify(f"Updated: {prop.title}")
        self.state.save()
        self.refresh_row(prop)

    @work(group="write")
    async def action_keep(self) -> None:
        prop = self.current()
        if not prop or prop.status in ("pending", "flagged", "applied", "kept"):
            return
        if await self.write(prop, "kept"):
            self.notify(f"Kept as-is and flagged: {prop.title}")
        self.state.save()
        self.refresh_row(prop)

    def action_skip(self) -> None:
        prop = self.current()
        if prop and prop.status in TODO | {"auto"}:
            prop.status = "skipped"
            self.refresh_row(prop)

    def action_toggle(self) -> None:
        prop = self.current()
        ch = self.query_one("#changes", DataTable)
        if not prop or not prop.changes or prop.status not in ("review", "auto", "nomatch"):
            return
        i = min(ch.cursor_row, len(prop.changes) - 1)
        prop.changes[i].accepted = not prop.changes[i].accepted
        row = ch.cursor_row
        self.show_detail()
        ch.move_cursor(row=row)
        self.refresh_row(prop)

    def action_open_covers(self) -> None:
        prop = self.current()
        if not prop:
            return
        files = []
        try:
            book = read_epub(Path(prop.path))
            if book.cover_bytes:
                cur = Path(tempfile.gettempdir()) / "bookmeta-current-cover.jpg"
                cur.write_bytes(book.cover_bytes)
                files.append(str(cur))
        except Exception:
            pass
        if prop.cover_file:
            files.append(prop.cover_file)
        if not files:
            self.notify("No covers to show.")
            return
        opener = ["open", "-a", "Preview"] if sys.platform == "darwin" else ["xdg-open"]
        for f in files if opener[0] == "xdg-open" else [None]:
            subprocess.Popen(opener + ([f] if f else files), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def action_filter(self) -> None:
        self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
        self.rebuild()
        self.update_subtitle()

    def action_reanalyze(self) -> None:
        prop = self.current()
        if not prop or prop.status in ("applied", "kept", "flagged"):
            return
        self.props[prop.path] = Proposal(path=prop.path, title=prop.title, authors=prop.authors)
        self.refresh_row(self.props[prop.path])
        if self.running:
            self.notify("Will re-check after the current scan; press s then.")
        else:
            self.action_start()


async def run_headless(library: Path, dry_run: bool, auto_apply: bool, stats_db: Path | None) -> None:
    """--force: no TUI. Every book is re-run through Jev and Haiku, ignoring cached results and the verified flag."""
    library = library.resolve()
    root = store.library_dir(library)
    state = store.State(root)
    books, other = store.scan(library)
    print(f"{len(books)} EPUBs, {len(other)} unsupported files skipped. Forcing a full re-check of every book.")

    counts: Counter = Counter()
    review: list[Proposal] = []
    write_lock = asyncio.Lock()
    done = 0

    async with make_client() as http:
        ctx = Context(http, Jev(http), root / "covers", Haiku(), include_verified=True)
        if not ctx.jev.available:
            print("No TYPESAFE_API_KEY: identity checks fall back to rules and descriptions are only rule-cleaned.")
        if not ctx.haiku.available:
            print("No ANTHROPIC_API_KEY: skipping the Haiku pass.")
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(path: Path) -> None:
            nonlocal done
            async with sem:
                try:
                    prop = await analyze(path, ctx)
                except Exception as e:  # one bad book must not stop the run
                    prop = Proposal(path=str(path), title=path.name, status="error", error=f"{type(e).__name__}: {e}")
                if store.custom_metadata(path):
                    prop.notes.append("KOReader custom metadata (custom_metadata.lua) overrides the file's metadata")
                if auto_apply and not dry_run and prop.status in ("auto", "ok"):
                    async with write_lock:
                        try:
                            prop = await asyncio.to_thread(store.apply, prop, library, root, "verified", stats_db)
                        except Exception as e:
                            prop.status, prop.error = "error", f"write failed: {type(e).__name__}: {e}"
                state.put(prop)
                counts[prop.status] += 1
                if prop.status in TODO | {"flagged"} or prop.notes:
                    review.append(prop)
                done += 1
                print(f"[{done}/{len(books)}] {STATUS_LABEL.get(prop.status, prop.status):<22} {short(prop.title, 55)}")
                if done % 10 == 0:
                    state.save()

        await asyncio.gather(*(one(p) for p in books))
    state.save()

    print(f"\nDone: " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
    flagged_for_review = [p for p in review if p.status in TODO or (p.notes and p.status != "flagged")]
    if flagged_for_review:
        print(f"\n{len(flagged_for_review)} book(s) need attention:")
        for p in flagged_for_review:
            print(f"  {p.title} — {p.status}" + (f": {'; '.join(p.notes)}" if p.notes else ""))
        print("\nOpen the TUI (without --force) to review and apply these.")


def main() -> None:
    ap = argparse.ArgumentParser(prog="bookmeta", description="Verify and fix EPUB metadata in a KOReader library.")
    ap.add_argument("library", type=Path, help="library folder (searched recursively)")
    ap.add_argument("--dry-run", action="store_true", help="analyse only, never write to books")
    ap.add_argument("--no-auto", action="store_true", help="queue everything for review instead of auto-applying")
    ap.add_argument("--stats", type=Path, metavar="DB", help="KOReader statistics.sqlite3 to keep in sync with title/author fixes")
    ap.add_argument("--recheck", action="store_true", help="ignore cached results from earlier runs")
    ap.add_argument("--include-verified", action="store_true", help="re-check books already flagged as verified/kept instead of skipping them")
    ap.add_argument("--force", action="store_true", help="headless (no TUI): ignore all cached results and the verified flag, re-run Jev and Haiku over every book in the library")
    args = ap.parse_args()
    if not args.library.is_dir():
        ap.error(f"{args.library} is not a folder")
    if args.stats and not args.stats.is_file():
        ap.error(f"{args.stats} not found")
    if args.force:
        asyncio.run(run_headless(args.library, args.dry_run, not args.no_auto, args.stats))
        return
    BookMetaApp(args.library, args.dry_run, not args.no_auto, args.stats, args.recheck, args.include_verified).run()


if __name__ == "__main__":
    main()
