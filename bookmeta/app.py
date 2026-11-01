"""Textual TUI: scan a KOReader library, verify every EPUB, auto-apply safe fixes and queue the rest for review."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Header, Static

from . import covers, settings, store
from .epub import read_epub
from .haiku import Haiku
from .jev import Jev
from .sources import enabled_sources, make_client
from .verify import STATUS_LABEL, Change, Context, Proposal, analyze

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
SHORT_FIELD = {"description": "desc", "authors": "author", "series_index": "series#", "publisher": "pub",
               "pubdate": "date", "language": "lang", "subjects": "subj"}
KOBO_BOOKS = Path("/Volumes/KOBOeReader/.adds/koreader/books")


def open_path(path: Path) -> None:
    opener = ["open"] if sys.platform == "darwin" else ["xdg-open"]
    subprocess.Popen(opener + [str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fields(changes: list[Change]) -> str:
    return ", ".join(SHORT_FIELD.get(c.field, c.field) for c in changes)


def short(v, n: int = 60) -> str:
    s = ", ".join(v) if isinstance(v, list) else str(v or "")
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= n else s[: n - 1] + "…"


class CoverPicker(ModalScreen[int | None]):
    """Lists the covers shown side by side in the comparison sheet; a number key picks one."""

    CSS = """
    CoverPicker { align: center middle; }
    #picker { width: 80; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    """
    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, options: list[dict], chosen: int):
        super().__init__()
        self.options, self.chosen = options, chosen

    def compose(self) -> ComposeResult:
        lines = ["[b]Choose a cover[/b] [dim](numbers match the comparison sheet in Preview)[/dim]", ""]
        for i, o in enumerate(self.options):
            tags = [t for t in ("current" if o["current"] else "", "Haiku's pick" if o["haiku_pick"] else "",
                                "selected" if i == self.chosen else "") if t]
            problem = f" [red]{o['problem'].replace('_', ' ')}[/red]" if o["problem"] not in ("", "ok") else ""
            lines.append(f"[b]{i + 1}[/b]  {escape(o['label'])}{problem}"
                         + (f"  [cyan]{', '.join(tags)}[/cyan]" if tags else ""))
            if o["note"]:
                lines.append(f"   [dim]{escape(o['note'])}[/dim]")
        lines += ["", "[dim]1-9 choose · esc cancel[/dim]"]
        yield Static("\n".join(lines), id="picker")

    def on_key(self, event) -> None:
        if event.character and event.character.isdigit() and 1 <= int(event.character) <= len(self.options):
            event.stop()
            self.dismiss(int(event.character) - 1)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


class Confirm(ModalScreen[bool]):
    CSS = """
    Confirm { align: center middle; }
    #confirm { width: 70; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    """
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        yield Static(f"{self.text}\n\n[b]y[/b] yes · [b]n[/b] no", id="confirm")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class BookMetaApp(App):
    TITLE = "bookmeta"
    CSS = """
    #books { width: 55%; }
    #side { width: 45%; border-left: solid $primary-darken-2; }
    #info { height: auto; padding: 0 1; }
    #changes { height: auto; max-height: 12; }
    #legend { height: 1; padding: 0 1; color: $text-muted; }
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
        Binding("m", "drop_custom", "Drop custom meta"),
        Binding("f", "filter", "Filter"),
        Binding("r", "reanalyze", "Re-check"),
        Binding("c", "toggle_covers_mode", "Covers mode", show=False),
        Binding("v", "toggle_verified", "Include verified", show=False),
        Binding("tab", "focus_next", "Switch pane", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, library: Path, dry_run: bool, auto_apply: bool, stats_db: Path | None, recheck: bool,
                 include_verified: bool = False, covers_only: bool = False):
        super().__init__()
        self.library = library.resolve()
        self.dry_run = dry_run
        self.auto_apply = auto_apply
        self.stats_db = stats_db
        self.recheck = recheck
        self.covers_only = covers_only
        self.want_verified = include_verified  # the user's choice; covers-only mode always includes them
        self.root = store.library_dir(self.library)
        self.state = self.open_state()
        self.props: dict[str, Proposal] = {}
        self.order: list[str] = []
        self.unsupported: list[Path] = []
        self.custom: set[str] = set()  # books with KOReader custom metadata
        self.changelog: dict[str, list[dict]] = {}
        self.log_size = -1
        self.filter = "all"
        self.running = False
        self.generation = 0  # bumped to make a running scan stop picking up new books
        self.done = 0
        self.write_lock = asyncio.Lock()
        self.load_palette()

    @property
    def include_verified(self) -> bool:
        return self.want_verified or self.covers_only

    def open_state(self) -> store.State:
        # Cover-only results are cached apart so they never stand in for a full metadata check.
        return store.State(self.root, "state-covers.json" if self.covers_only else "state.json")

    def load_palette(self) -> None:
        """Custom themes and status colours from settings.json; the chosen theme is restored in on_mount."""
        cfg = settings.load()
        self.palette_errors = []
        for name, spec in (cfg.get("themes") or {}).items():
            try:
                self.register_theme(Theme(name=name, **spec))
            except (TypeError, ValueError) as e:
                self.palette_errors.append(f"theme {name!r}: {e}")
        STATUS_STYLE.update({k: v for k, v in (cfg.get("status_colors") or {}).items() if isinstance(v, str)})
        self.saved_theme = cfg.get("theme")

    def watch_theme(self, theme: str) -> None:
        if self.is_mounted:
            settings.save(theme=theme)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="books", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield Static(id="info")
                yield DataTable(id="changes", cursor_type="row")
                yield Static(id="legend")
                with VerticalScroll(id="diffwrap"):
                    yield Static(id="diff")
        yield Footer()

    def on_mount(self) -> None:
        if self.saved_theme in self.available_themes:
            self.theme = self.saved_theme
        for err in self.palette_errors:
            self.notify(f"settings.json: {err}", severity="error", timeout=10)
        books = self.query_one("#books", DataTable)
        books.add_column("Status", key="status", width=20)
        books.add_column("Title", key="title", width=36)
        books.add_column("Author", key="author", width=20)
        books.add_column("Changes", key="n", width=18)
        ch = self.query_one("#changes", DataTable)
        ch.add_column("", key="on", width=2)
        ch.add_column("Field", key="field", width=11)
        ch.add_column("Before", key="current", width=18)
        ch.add_column("After", key="proposed")
        ch.add_column("From", key="src", width=9)

        self.load_props()
        self.rebuild()
        sources = ", ".join(enabled_sources())
        self.notify(f"{len(self.order)} EPUBs, {len(self.unsupported)} unsupported files skipped. Sources: {sources}. "
                    f"{self.mode_text()}. ctrl+p for every command.", timeout=8)
        self.update_subtitle()
        self.action_start()

    def load_props(self, recheck: bool = False) -> None:
        paths, self.unsupported = store.scan(self.library)
        self.custom = {str(p) for p in paths if store.custom_metadata(p)}
        self.props, self.order = {}, []
        for p in paths:
            prop = None if recheck or self.recheck else self.state.get(p)
            if prop is not None and self.include_verified and prop.status == "flagged":
                prop = None  # re-offer already-verified books instead of skipping them
            if prop is None:
                prop = Proposal(path=str(p), title=p.name)
            self.props[str(p)] = prop
            self.order.append(str(p))
        self.recheck = False

    def mode_text(self) -> str:
        parts = ["DRY RUN, nothing will be written" if self.dry_run else f"auto-apply {'on' if self.auto_apply else 'off'}"]
        if self.covers_only:
            parts.insert(0, "covers only (o to compare and pick)")
        elif self.want_verified:
            parts.append("including verified books")
        if self.stats_db:
            parts.append("statistics sync on")
        return ", ".join(parts)

    # ---- table rendering -------------------------------------------------

    def visible(self, prop: Proposal) -> bool:
        if self.filter == "review":
            return prop.status == "review"
        if self.filter == "todo":
            return prop.status in TODO
        return True

    def row(self, prop: Proposal) -> tuple:
        st = Text(STATUS_LABEL.get(prop.status, prop.status), style=STATUS_STYLE.get(prop.status, ""))
        warn = " ⚠" if prop.path in self.custom or any("SWAPPED" in n for n in prop.notes) else ""
        changed = [c for c in prop.changes if c.accepted] if prop.status in ("review", "auto", "applied", "nomatch") else []
        style = "green" if prop.status == "applied" else ""
        return st, short(prop.title, 36) + warn, short(prop.authors, 20), Text(short(fields(changed), 18), style=style)

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
        flags = [f for f, on in (("covers only", self.covers_only), ("incl. verified", self.want_verified and not self.covers_only),
                                 ("DRY RUN", self.dry_run), ("auto-apply off", not self.auto_apply and not self.dry_run),
                                 ("stats sync", bool(self.stats_db))) if on]
        self.sub_title = "  ·  ".join([self.library.name, *flags, "  ".join(parts), f"filter: {self.filter}"])

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

    def history(self, path: str) -> list[dict]:
        try:
            size = (self.root / "changes.jsonl").stat().st_size
        except OSError:
            size = 0
        if size != self.log_size:
            self.changelog, self.log_size = store.history(self.root), size
        return self.changelog.get(path, [])

    def summary(self, prop: Proposal) -> list[str]:
        """What has been, or will be, written to this book, in a line or two."""
        on = [c for c in prop.changes if c.accepted]
        off = [c for c in prop.changes if not c.accepted]
        if prop.status == "applied":
            return [f"[b green]Written:[/] {escape(fields(on)) or 'the verified flag only'}"]
        if prop.status == "kept":
            return ["[b green]Kept as-is[/] (flagged so it's skipped next time)"]
        if prop.status == "flagged":
            return ["[green]Already verified; nothing to do.[/]"]
        if prop.status in ("pending", "skipped", "error"):
            return []
        out = []
        if on:
            verb = "Will write automatically" if prop.status == "auto" and self.auto_apply and not self.dry_run else "Will write when you press a"
            out.append(f"[b]{verb}:[/b] {escape(fields(on))}")
        elif prop.status != "nomatch":
            out.append("[green]Nothing to change.[/]" if not off else "[b]Nothing ticked to write.[/b]")
        if off:
            out.append(f"[dim]Not writing (tick with space): {escape(fields(off))}[/dim]")
        return out

    def show_detail(self) -> None:
        prop = self.current()
        info = self.query_one("#info", Static)
        ch = self.query_one("#changes", DataTable)
        ch.clear()
        if not prop:
            info.update("")
            self.query_one("#legend", Static).update("")
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
        lines += self.summary(prop)
        if prop.path in self.custom:
            custom = store.custom_fields(Path(prop.path))
            shown = "; ".join(f"{k} “{short(v, 40)}”" for k, v in custom.items()) or "no fields"
            lines.append(f"[yellow]• KOReader shows its custom metadata instead of the file's ({escape(shown)}). "
                         f"Press m to remove it.[/yellow]")
        lines += [f"[yellow]• {escape(n)}[/yellow]" for n in prop.notes if "custom_metadata.lua" not in n]
        if prop.error:
            lines.append(f"[red]{escape(prop.error)}[/red]")
        for e in self.history(prop.path)[-4:]:
            when = e.get("time", "")[:16].replace("T", " ")
            if e.get("removed"):
                what = f"removed {e['removed']}"
            elif e.get("changes"):
                what = "wrote " + ", ".join(SHORT_FIELD.get(c["field"], c["field"]) for c in e["changes"])
            else:
                what = f"flagged {e.get('flag') or ''}".strip()
            lines.append(f"[dim]{when}  {escape(what)}[/dim]")
        info.update("\n".join(lines))
        written = prop.status == "applied"
        for i, c in enumerate(prop.changes):
            mark = Text("✓" if written and c.accepted else "☑" if c.accepted else "☐",
                        style="green" if c.accepted else "dim")
            fld = Text(c.field, style="" if c.auto else "yellow")
            ch.add_row(mark, fld, Text(short(c.current, 18) or "—", style="dim"), short(c.proposed, 200), c.source, key=str(i))
        legend = ""
        if prop.changes:
            legend = ("✓ written" if written else "☑ will be written · ☐ won't · yellow = needs your check · tab, space to toggle")
        self.query_one("#legend", Static).update(legend)
        self.show_change()

    def show_change(self) -> None:
        prop = self.current()
        diff = self.query_one("#diff", Static)
        ch = self.query_one("#changes", DataTable)
        if not prop or not prop.changes or not ch.row_count:
            diff.update("" if not prop or prop.changes or prop.status == "pending" else "[dim]No changes proposed.[/dim]")
            return
        c = prop.changes[min(ch.cursor_row, len(prop.changes) - 1)]
        fmt = lambda v: escape("\n".join(v) if isinstance(v, list) else str(v or "—"))
        state = "written" if prop.status == "applied" and c.accepted else (
            "will be written" if c.accepted else "not ticked") + ("" if c.auto else ", needs review")
        diff.update(
            f"[b]{c.field}[/b] [dim]({c.source}; {escape(c.reason)}; {state})[/dim]\n\n"
            f"[red]Before[/red]\n{fmt(c.current)}\n\n[green]After[/green]\n{fmt(c.proposed)}"
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
        gen, state, props = self.generation, self.state, self.props
        self.update_subtitle()
        async with make_client() as http:
            ctx = Context(http, Jev(http), self.root / "covers", Haiku(), self.include_verified, self.covers_only)
            if not ctx.jev.available:
                self.notify("No TYPESAFE_API_KEY: identity checks fall back to rules and descriptions are only rule-cleaned.", severity="warning", timeout=10)
            sem = asyncio.Semaphore(CONCURRENCY)

            async def one(path: str) -> None:
                async with sem:
                    if gen != self.generation:
                        return  # the mode changed: leave the rest for the new scan
                    prop = props[path]
                    if prop.status == "pending":
                        try:
                            prop = await analyze(Path(path), ctx)
                        except Exception as e:  # one bad book must not stop the scan
                            prop.status, prop.error = "error", f"{type(e).__name__}: {e}"
                        props[path] = prop
                        state.put(prop)
                    if gen != self.generation:
                        return
                    if self.auto_apply and not self.dry_run and prop.status in ("auto", "ok") and (prop.changes or not self.covers_only):
                        await self.write(prop, "verified")
                    self.done += 1
                    self.refresh_row(prop)
                    if self.done % 10 == 0:
                        state.save()

            await asyncio.gather(*(one(p) for p in todo))
        self.running = False
        state.save()
        if gen != self.generation:
            return
        self.update_subtitle()
        n = sum(p.status in TODO for p in self.props.values())
        self.notify(f"Scan finished. {n} book(s) need your attention — press f to filter." if n else "Scan finished. Nothing needs review.")

    async def write(self, prop: Proposal, flag: str | None) -> bool:
        if self.covers_only:
            flag = None  # a new cover says nothing about the rest of the metadata: leave the flag alone
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
        if self.covers_only and not any(c.accepted for c in prop.changes):
            self.notify("No new cover selected; press o to compare covers.")
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
        if self.covers_only:  # don't flag the metadata as reviewed from a cover-only run
            self.action_skip()
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
        try:
            current = read_epub(Path(prop.path)).cover_bytes
        except Exception:
            current = None
        options = [o for o in prop.cover_options if not o["current"] or current]
        if not options:  # scanned before covers were compared: show what we have
            options = ([{"file": "", "label": "current", "source": "current", "problem": "", "note": "",
                         "haiku_pick": False, "current": True}] if current else [])
            if prop.cover_file:
                options.append({"file": prop.cover_file, "label": "proposed", "source": "", "problem": "", "note": "",
                                "haiku_pick": False, "current": False})
        images = []
        for o in options:
            try:
                data = current if o["current"] else Path(o["file"]).read_bytes()
            except OSError:
                continue
            caption = f"{len(images) + 1}. {o['label']}" + ("\ncurrent" if o["current"] else "")
            caption += ("  * Haiku's pick" if o["haiku_pick"] else "")
            if o["problem"] not in ("", "ok"):
                caption += f"\n{o['problem'].replace('_', ' ')}"
            images.append((data, caption, o))
        if not images:
            self.notify("No covers to show.")
            return
        sheet = Path(tempfile.gettempdir()) / f"bookmeta-covers-{abs(hash(prop.path))}.jpg"
        try:
            sheet.write_bytes(covers.montage([(d, c) for d, c, _ in images]))
        except Exception as e:
            self.notify(f"Couldn't build the comparison: {e}", severity="error")
            return
        opener = ["open", "-a", "Preview"] if sys.platform == "darwin" else ["xdg-open"]
        subprocess.Popen(opener + [str(sheet)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if prop.status not in ("review", "auto", "ok", "nomatch") or len(images) < 2:
            return
        shown = [o for _, _, o in images]
        cover_change = next((c for c in prop.changes if c.field == "cover" and c.accepted), None)
        chosen = next((i for i, o in enumerate(shown) if cover_change and o["file"] == prop.cover_file),
                      next((i for i, o in enumerate(shown) if o["current"]), -1))
        self.push_screen(CoverPicker(shown, chosen), lambda i: self.choose_cover(prop, shown, i))

    def choose_cover(self, prop: Proposal, shown: list[dict], i: int | None) -> None:
        if i is None:
            return
        o = shown[i]
        prop.changes = [c for c in prop.changes if c.field != "cover"]
        if o["current"]:
            prop.cover_file = ""
            self.notify("Keeping the current cover.")
        else:
            current = next((x["label"] for x in shown if x["current"]), "none")
            prop.cover_file = o["file"]
            prop.changes.append(Change("cover", current, o["label"], o["source"], False, "chosen by you"))
            self.notify(f"Cover {i + 1} selected; press a to apply.")
        if prop.status in ("auto", "ok"):
            prop.status = "review"
        self.state.put(prop)
        self.show_detail()
        self.refresh_row(prop)

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


    # ---- modes and commands (also in the command palette, ctrl+p) ----------

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        turn = lambda on: "turn off" if on else "turn on"
        yield SystemCommand(f"Covers-only mode: {turn(self.covers_only)}",
                            "Re-check only covers across the whole library, verified books included (c)",
                            self.action_toggle_covers_mode)
        yield SystemCommand(f"Include verified books: {turn(self.want_verified)}",
                            "Re-check books already flagged verified/kept instead of skipping them (v)",
                            self.action_toggle_verified)
        yield SystemCommand(f"Auto-apply: {turn(self.auto_apply)}",
                            "Write safe changes automatically, or queue every book for review", self.action_toggle_auto)
        yield SystemCommand(f"Dry run: {turn(self.dry_run)}", "Never write anything to the books", self.action_toggle_dry_run)
        yield SystemCommand(f"Statistics sync: {turn(bool(self.stats_db))}",
                            "Keep KOReader reading statistics attached when a title/author changes (close KOReader first)",
                            self.action_toggle_stats)
        yield SystemCommand("Re-check the library", "Ignore cached results and check every book again", self.action_recheck_all)
        yield SystemCommand("Force re-check everything",
                            "Ignore cached results and the verified flag: run Jev and Haiku over every book",
                            self.action_force)
        yield SystemCommand("Remove custom metadata from this book",
                            "Move KOReader's custom_metadata.lua into the backups so the file's metadata shows (m)",
                            self.action_drop_custom)
        yield SystemCommand(f"Remove custom metadata from all books ({len(self.custom)})",
                            "Same, for every book in the library that has it", self.action_drop_custom_all)
        yield SystemCommand("Refresh KOReader thumbnails",
                            "Clear KOReader's cached covers and metadata for books changed since it last looked (close KOReader first)",
                            self.action_refresh_thumbnails)
        yield SystemCommand("Compare covers", "Open the comparison sheet and pick a cover (o)", self.action_open_covers)
        yield SystemCommand(f"Filter: {FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]}",
                            "Cycle all / needs review / anything needing you (f)", self.action_filter)
        yield SystemCommand("Open the data folder", "Backups, cached covers and the change log (changes.jsonl)",
                            lambda: open_path(self.root))

    @work(exclusive=True, group="restart")
    async def restart(self, recheck: bool = False, covers_only: bool | None = None) -> None:
        """Stop the scan at the next book, then reload the library under the new mode and scan again."""
        self.generation += 1
        scans = [w for w in self.workers if w.group == "scan" and w.is_running]
        if scans:
            self.notify("Finishing the books in progress…", timeout=3)
            for w in scans:
                await w.wait()
        self.running = False
        self.state.save()
        if covers_only is not None and covers_only != self.covers_only:
            self.covers_only = covers_only
            self.state = self.open_state()
        self.load_props(recheck)
        self.rebuild()
        self.update_subtitle()
        self.notify(self.mode_text().capitalize() + ".")
        self.action_start()

    def action_toggle_covers_mode(self) -> None:
        self.restart(covers_only=not self.covers_only)

    def action_toggle_verified(self) -> None:
        self.want_verified = not self.want_verified
        if self.covers_only:
            self.notify("Covers-only mode always includes verified books.")
            self.update_subtitle()
            return
        self.restart()

    def action_recheck_all(self) -> None:
        self.restart(recheck=True)

    def action_force(self) -> None:
        self.want_verified = True
        self.restart(recheck=True)

    def action_toggle_auto(self) -> None:
        self.auto_apply = not self.auto_apply
        self.notify(self.mode_text().capitalize() + ".")
        self.update_subtitle()
        self.show_detail()
        if self.auto_apply and not self.dry_run:
            self.action_start()  # writes the books already marked auto-apply

    def action_toggle_dry_run(self) -> None:
        self.dry_run = not self.dry_run
        self.notify(self.mode_text().capitalize() + ".")
        self.update_subtitle()
        self.show_detail()
        if not self.dry_run and self.auto_apply:
            self.action_start()

    def action_toggle_stats(self) -> None:
        if self.stats_db:
            self.stats_db = None
            self.notify("Statistics sync off.")
        elif db := store.find_stats_db(self.library):
            self.stats_db = db
            self.notify(f"Statistics sync on: {db}. Keep KOReader closed while writing.", timeout=8)
        else:
            self.notify("Couldn't find KOReader's statistics.sqlite3 near this library; pass --stats PATH.",
                        severity="warning")
        self.update_subtitle()

    def drop_custom(self, path: str) -> bool:
        try:
            removed = store.remove_custom_metadata(Path(path), self.library, self.root)
        except OSError as e:
            self.notify(f"Couldn't remove custom metadata: {e}", severity="error")
            return False
        self.custom.discard(path)
        if path in self.props:
            self.refresh_row(self.props[path])
        return removed

    def action_drop_custom(self) -> None:
        prop = self.current()
        if not prop:
            return
        if prop.path not in self.custom:
            self.notify("This book has no KOReader custom metadata.")
        elif self.dry_run:
            self.notify("Dry run: not removing.", severity="warning")
        elif self.drop_custom(prop.path):
            self.notify("Custom metadata removed (backed up). If KOReader still shows the old values, use "
                        "Refresh cached book information on the book.", timeout=8)

    def action_drop_custom_all(self) -> None:
        if not self.custom:
            self.notify("No books have KOReader custom metadata.")
            return
        if self.dry_run:
            self.notify("Dry run: not removing.", severity="warning")
            return

        def go(yes: bool | None) -> None:
            if yes:
                n = sum(self.drop_custom(p) for p in sorted(self.custom))
                self.notify(f"Removed custom metadata from {n} book(s); backups are in the data folder.")

        self.push_screen(Confirm(f"Remove KOReader custom metadata from {len(self.custom)} book(s)?\n"
                                 "KOReader will show each file's own metadata again. Every file is backed up first."), go)

    def action_refresh_thumbnails(self) -> None:
        db = store.find_bookinfo_db(self.library)
        if not db:
            self.notify("Couldn't find KOReader's bookinfo_cache.sqlite3 near this library.", severity="warning")
            return
        if self.dry_run:
            self.notify("Dry run: not changing KOReader's cache.", severity="warning")
            return
        try:
            n = store.prune_stale_bookinfo(db, [Path(p) for p in self.order], self.root)
        except (OSError, sqlite3.Error) as e:
            self.notify(f"Couldn't update KOReader's cache: {e}", severity="error")
            return
        self.notify(f"Cleared {n} stale thumbnail(s); KOReader rebuilds them the next time it shows those books.")


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
    ap.add_argument("library", type=Path, nargs="?", help="library folder, searched recursively (default: the last one "
                    "you used, else the Kobo's KOReader books folder)")
    ap.add_argument("--dry-run", action="store_true", help="analyse only, never write to books")
    ap.add_argument("--no-auto", action="store_true", help="queue everything for review instead of auto-applying")
    ap.add_argument("--stats", type=Path, metavar="DB", help="KOReader statistics.sqlite3 to keep in sync with title/author fixes")
    ap.add_argument("--recheck", action="store_true", help="ignore cached results from earlier runs")
    ap.add_argument("--include-verified", action="store_true", help="re-check books already flagged as verified/kept instead of skipping them")
    ap.add_argument("--covers", action="store_true", help="TUI that re-checks only covers across the whole library (verified books included), to compare and upgrade them")
    ap.add_argument("--force", action="store_true", help="headless (no TUI): ignore all cached results and the verified flag, re-run Jev and Haiku over every book in the library")
    args = ap.parse_args()
    if args.library is None:
        last = settings.load().get("last_library")
        args.library = next((Path(p) for p in (last, KOBO_BOOKS) if p and Path(p).is_dir()), None)
        if args.library is None:
            ap.error("give the library folder (no earlier library or connected Kobo found)")
    if not args.library.is_dir():
        ap.error(f"{args.library} is not a folder")
    if args.stats and not args.stats.is_file():
        ap.error(f"{args.stats} not found")
    if args.force and args.covers:
        ap.error("--covers and --force can't be combined")
    settings.save(last_library=str(args.library.resolve()))
    if args.force:
        asyncio.run(run_headless(args.library, args.dry_run, not args.no_auto, args.stats))
        return
    BookMetaApp(args.library, args.dry_run, not args.no_auto, args.stats, args.recheck, args.include_verified, args.covers).run()


if __name__ == "__main__":
    main()
