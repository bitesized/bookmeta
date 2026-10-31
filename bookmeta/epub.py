"""Read and rewrite EPUB (and kepub.epub) metadata in place."""

from __future__ import annotations

import html
import io
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from lxml import etree
from PIL import Image

from .describe import clean_html

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NS = {"opf": OPF_NS, "dc": DC_NS, "c": "urn:oasis:names:tc:opendocument:xmlns:container"}
OPF = f"{{{OPF_NS}}}"
DC = f"{{{DC_NS}}}"

FLAG_NAME = "bookmeta:status"
FLAG_DATE = "bookmeta:date"
COVER_MAX_EDGE = 2000

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, remove_comments=False)


@dataclass
class BookMeta:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    series: str = ""
    series_index: str = ""
    description: str = ""
    publisher: str = ""
    pubdate: str = ""
    language: str = ""
    isbn: str = ""
    subjects: list[str] = field(default_factory=list)

    FIELDS = ("title", "authors", "series", "series_index", "description", "publisher", "pubdate", "language", "isbn", "subjects")


@dataclass
class Epub:
    path: Path
    meta: BookMeta
    version: str
    opf_path: str
    cover_path: str | None
    cover_bytes: bytes | None
    flag: str | None
    front_text: str
    text_isbns: list[str]

    @property
    def cover_size(self) -> tuple[int, int] | None:
        return image_size(self.cover_bytes)


# --------------------------------------------------------------------------- helpers


def image_size(data: bytes | None) -> tuple[int, int] | None:
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def isbn_valid(isbn: str) -> bool:
    if len(isbn) == 13 and isbn.isdigit():
        total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(isbn[:12]))
        return (10 - total % 10) % 10 == int(isbn[12])
    if len(isbn) == 10 and isbn[:9].isdigit() and (isbn[9].isdigit() or isbn[9] in "Xx"):
        total = sum((10 - i) * int(d) for i, d in enumerate(isbn[:9]))
        check = (11 - total % 11) % 11
        return isbn[9].upper() == ("X" if check == 10 else str(check))
    return False


def to_isbn13(isbn: str) -> str:
    isbn = re.sub(r"[^0-9Xx]", "", isbn)
    if len(isbn) == 13:
        return isbn
    core = "978" + isbn[:9]
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core))
    return core + str((10 - total % 10) % 10)


_ISBN_RE = re.compile(r"ISBN[^0-9]{0,20}((?:\d[\s‐-―-]?){9,12}[\dXx])|\b(97[89]\d{10})\b", re.I)


def find_isbns(text: str) -> list[str]:
    found: list[str] = []
    for m in _ISBN_RE.finditer(text):
        raw = re.sub(r"[^0-9Xx]", "", m.group(1) or m.group(2))
        if isbn_valid(raw):
            isbn = to_isbn13(raw)
            if isbn not in found:
                found.append(isbn)
    return found


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _strip_html(markup: bytes) -> str:
    try:
        root = etree.fromstring(markup, etree.HTMLParser(recover=True))
        for bad in root.iter("script", "style"):
            bad.getparent().remove(bad)
        return re.sub(r"\s+", " ", " ".join(root.itertext())).strip()
    except Exception:
        return re.sub(r"\s+", " ", html.unescape(re.sub(rb"<[^>]+>", b" ", markup).decode("utf-8", "replace"))).strip()


def _metadata_el(opf_root):
    md = opf_root.find(f"{OPF}metadata")
    if md is None:
        md = opf_root.find(".//{*}metadata")
    return md


def _refines(md, el_id: str | None) -> list:
    if not el_id:
        return []
    return [m for m in md.findall(f"{OPF}meta") if m.get("refines") == f"#{el_id}"]


def _refine_value(md, el_id: str | None, prop: str) -> str:
    for m in _refines(md, el_id):
        if m.get("property") == prop:
            return (m.text or "").strip()
    return ""


def _named_meta(md, name: str):
    for m in md.findall(f"{OPF}meta"):
        if m.get("name") == name:
            return m
    return None


def _is_author(md, creator) -> bool:
    role = creator.get(f"{OPF}role") or creator.get("role") or _refine_value(md, creator.get("id"), "role")
    return not role or role.lower() == "aut"


# --------------------------------------------------------------------------- reading


def _opf_path(z: zipfile.ZipFile) -> str:
    container = etree.fromstring(z.read("META-INF/container.xml"), _PARSER)
    rootfile = container.find(".//c:rootfile", NS)
    if rootfile is None:
        rootfile = container.find(".//{*}rootfile")
    return rootfile.get("full-path")


def _resolve(opf_path: str, href: str) -> str:
    base = PurePosixPath(opf_path).parent
    parts: list[str] = []
    for part in (base / unquote(href.split("#")[0])).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    return "/".join(parts)


def _cover_item(opf_root):
    manifest = opf_root.find(f"{OPF}manifest")
    if manifest is None:
        return None
    items = manifest.findall(f"{OPF}item")
    for item in items:
        if "cover-image" in (item.get("properties") or "").split():
            return item
    md = _metadata_el(opf_root)
    cover_meta = _named_meta(md, "cover") if md is not None else None
    if cover_meta is not None:
        for item in items:
            if item.get("id") == cover_meta.get("content") and (item.get("media-type") or "").startswith("image/"):
                return item
    for item in items:
        if "cover" in (item.get("id") or "").lower() and (item.get("media-type") or "").startswith("image/"):
            return item
    return None


def read_epub(path: Path) -> Epub:
    with zipfile.ZipFile(path) as z:
        opf_path = _opf_path(z)
        root = etree.fromstring(z.read(opf_path), _PARSER)
        md = _metadata_el(root)
        meta = BookMeta()

        titles = md.findall(f"{DC}title")
        meta.title = _text(titles[0]) if titles else ""
        meta.authors = [_text(c) for c in md.findall(f"{DC}creator") if _is_author(md, c) and _text(c)]
        desc = md.find(f"{DC}description")
        meta.description = clean_html("".join(desc.itertext())) if desc is not None else ""
        meta.publisher = _text(md.find(f"{DC}publisher"))
        meta.pubdate = _text(md.find(f"{DC}date"))
        meta.language = _text(md.find(f"{DC}language"))
        meta.subjects = [_text(s) for s in md.findall(f"{DC}subject") if _text(s)]
        for ident in md.findall(f"{DC}identifier"):
            raw = re.sub(r"^(urn:)?isbn:", "", _text(ident), flags=re.I)
            digits = re.sub(r"[^0-9Xx]", "", raw)
            if isbn_valid(digits):
                meta.isbn = to_isbn13(digits)
                break

        series = _named_meta(md, "calibre:series")
        if series is not None:
            meta.series = (series.get("content") or "").strip()
            idx = _named_meta(md, "calibre:series_index")
            meta.series_index = (idx.get("content") or "").strip() if idx is not None else ""
        else:
            for m in md.findall(f"{OPF}meta"):
                if m.get("property") == "belongs-to-collection":
                    meta.series = (m.text or "").strip()
                    meta.series_index = _refine_value(md, m.get("id"), "group-position")
                    break

        flag_el = _named_meta(md, FLAG_NAME)
        flag = flag_el.get("content") if flag_el is not None else None

        cover_path = cover_bytes = None
        item = _cover_item(root)
        if item is not None:
            cover_path = _resolve(opf_path, item.get("href"))
            try:
                cover_bytes = z.read(cover_path)
            except KeyError:
                cover_path = None

        # Front matter text gives the verifier ground truth from the book itself, and
        # copyright pages (front or back) are the most reliable source of the real ISBN.
        manifest = {i.get("id"): i for i in root.iter(f"{OPF}item")}
        spine = [manifest[r.get("idref")] for r in root.iter(f"{OPF}itemref") if r.get("idref") in manifest]
        docs = spine[:8] + spine[-3:] if len(spine) > 11 else spine
        texts = []
        for item in docs:
            try:
                texts.append(_strip_html(z.read(_resolve(opf_path, item.get("href")))))
            except KeyError:
                continue
        front = " ".join(texts[:8])
        isbns = find_isbns(" ".join(texts))

    return Epub(
        path=path,
        meta=meta,
        version=root.get("version", "2.0"),
        opf_path=opf_path,
        cover_path=cover_path,
        cover_bytes=cover_bytes,
        flag=flag,
        front_text=front[:2500],
        text_isbns=isbns,
    )


# --------------------------------------------------------------------------- writing


def _remove(el) -> None:
    parent = el.getparent()
    if parent is not None:
        parent.remove(el)


def _remove_with_refines(md, el) -> None:
    for r in _refines(md, el.get("id")):
        _remove(r)
    _remove(el)


def _insert_after_last(md, tag: str, new_el) -> None:
    existing = md.findall(tag)
    if existing:
        existing[-1].addnext(new_el)
    else:
        md.append(new_el)


def _set_simple(md, tag: str, value: str) -> None:
    els = md.findall(tag)
    if els:
        for extra in els[1:]:
            _remove_with_refines(md, extra)
        for child in list(els[0]):
            els[0].remove(child)
        els[0].text = value
    else:
        el = etree.SubElement(md, tag)
        el.text = value


def _set_named_meta(md, name: str, value: str | None) -> None:
    el = _named_meta(md, name)
    if value is None or value == "":
        if el is not None:
            _remove(el)
        return
    if el is None:
        el = etree.SubElement(md, f"{OPF}meta")
        el.set("name", name)
    el.set("content", value)


def file_as(name: str) -> str:
    parts = name.split()
    if len(parts) < 2:
        return name
    suffixes = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
    if parts[-1].lower() in suffixes and len(parts) > 2:
        return f"{parts[-2]}, {' '.join(parts[:-2])} {parts[-1]}"
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def _apply_meta(root, version: str, changes: dict) -> None:
    md = _metadata_el(root)
    epub3 = version.startswith("3")

    if "title" in changes:
        titles = md.findall(f"{DC}title")
        for extra in titles[1:]:
            _remove_with_refines(md, extra)
        if titles:
            for child in list(titles[0]):
                titles[0].remove(child)
            titles[0].text = changes["title"]
        else:
            el = etree.Element(f"{DC}title")
            el.text = changes["title"]
            md.insert(0, el)

    if "authors" in changes:
        creators = [c for c in md.findall(f"{DC}creator") if _is_author(md, c)]
        anchor = creators[0].getprevious() if creators else md.find(f"{DC}title")
        for c in creators:
            _remove_with_refines(md, c)
        new_els = []
        for i, name in enumerate(changes["authors"], 1):
            el = etree.Element(f"{DC}creator")
            el.text = name
            if epub3:
                el.set("id", f"bookmeta-creator{i}")
                new_els.append(el)
                for prop, val in (("role", "aut"), ("file-as", file_as(name))):
                    m = etree.Element(f"{OPF}meta", refines=f"#bookmeta-creator{i}", property=prop)
                    if prop == "role":
                        m.set("scheme", "marc:relators")
                    m.text = val
                    new_els.append(m)
            else:
                el.set(f"{OPF}role", "aut")
                el.set(f"{OPF}file-as", file_as(name))
                new_els.append(el)
        for el in reversed(new_els):
            if anchor is not None:
                anchor.addnext(el)
            else:
                md.insert(0, el)

    for key, tag in (("description", "description"), ("publisher", "publisher"), ("pubdate", "date"), ("language", "language")):
        if key in changes:
            _set_simple(md, f"{DC}{tag}", changes[key])

    if "subjects" in changes:
        for s in md.findall(f"{DC}subject"):
            _remove(s)
        for subject in changes["subjects"]:
            el = etree.Element(f"{DC}subject")
            el.text = subject
            _insert_after_last(md, f"{DC}subject", el)

    if "isbn" in changes:
        have = False
        unique_id = root.get("unique-identifier")
        for ident in md.findall(f"{DC}identifier"):
            digits = re.sub(r"[^0-9Xx]", "", re.sub(r"^(urn:)?isbn:", "", _text(ident), flags=re.I))
            if isbn_valid(digits):
                if ident.get("id") == unique_id:
                    have = True  # never rewrite the package's unique identifier
                else:
                    _remove(ident)
        if not have:
            el = etree.Element(f"{DC}identifier")
            if epub3:
                el.text = f"urn:isbn:{changes['isbn']}"
            else:
                el.set(f"{OPF}scheme", "ISBN")
                el.text = changes["isbn"]
            _insert_after_last(md, f"{DC}identifier", el)

    if "series" in changes or "series_index" in changes:
        series = changes.get("series")
        index = changes.get("series_index")
        if series is None:
            current = _named_meta(md, "calibre:series")
            series = current.get("content") if current is not None else ""
        _set_named_meta(md, "calibre:series", series)
        _set_named_meta(md, "calibre:series_index", index if series else None)
        if epub3:
            for m in md.findall(f"{OPF}meta"):
                if m.get("property") == "belongs-to-collection":
                    _remove_with_refines(md, m)
            if series:
                coll = etree.SubElement(md, f"{OPF}meta", property="belongs-to-collection", id="bookmeta-series")
                coll.text = series
                ctype = etree.SubElement(md, f"{OPF}meta", refines="#bookmeta-series", property="collection-type")
                ctype.text = "series"
                if index:
                    pos = etree.SubElement(md, f"{OPF}meta", refines="#bookmeta-series", property="group-position")
                    pos.text = index


def encode_cover(data: bytes, media_type: str) -> bytes:
    """Re-encode a downloaded cover to the media type the EPUB declares, capped in size."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if max(im.size) > COVER_MAX_EDGE:
            im.thumbnail((COVER_MAX_EDGE, COVER_MAX_EDGE), Image.LANCZOS)
        out = io.BytesIO()
        if media_type == "image/png":
            im.save(out, "PNG", optimize=True)
        else:
            im.convert("RGB").save(out, "JPEG", quality=90, optimize=True, progressive=False)
        return out.getvalue()


def write_epub(
    book: Epub,
    changes: dict,
    cover: bytes | None,
    status: str,
    library_root: Path,
    backup_root: Path,
) -> Path:
    """Rewrite the book with new metadata/cover and the verified flag. Returns the backup path."""
    path = book.path
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read(book.opf_path), _PARSER)
        _apply_meta(root, book.version, changes)
        md = _metadata_el(root)
        _set_named_meta(md, FLAG_NAME, status)
        _set_named_meta(md, FLAG_DATE, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

        replace: dict[str, bytes] = {}
        add_cover: tuple[str, bytes] | None = None
        if cover:
            item = _cover_item(root)
            if item is not None and book.cover_path:
                replace[book.cover_path] = encode_cover(cover, item.get("media-type") or "image/jpeg")
            else:
                manifest = root.find(f"{OPF}manifest")
                href = "bookmeta-cover.jpg"
                item = etree.SubElement(manifest, f"{OPF}item", id="bookmeta-cover", href=href)
                item.set("media-type", "image/jpeg")
                if book.version.startswith("3"):
                    item.set("properties", "cover-image")
                _set_named_meta(md, "cover", "bookmeta-cover")
                add_cover = (_resolve(book.opf_path, href), encode_cover(cover, "image/jpeg"))

        etree.indent(md, space="    ", level=1)
        replace[book.opf_path] = etree.tostring(root, xml_declaration=True, encoding="utf-8")

        tmp = path.with_name(f".bookmeta-tmp-{path.name}")
        with zipfile.ZipFile(tmp, "w") as out:
            names = z.namelist()
            if "mimetype" in names:
                out.writestr(zipfile.ZipInfo("mimetype"), z.read("mimetype"), compress_type=zipfile.ZIP_STORED)
            for info in z.infolist():
                if info.filename == "mimetype":
                    continue
                data = replace.get(info.filename)
                if data is None:
                    data = z.read(info.filename)
                out.writestr(info, data, compress_type=info.compress_type)
            if add_cover:
                out.writestr(add_cover[0], add_cover[1], compress_type=zipfile.ZIP_DEFLATED)

    try:
        check = read_epub(tmp)
        if check.flag != status:
            raise RuntimeError("rewritten EPUB failed verification")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    backup = backup_root / path.relative_to(library_root)
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():  # keep the oldest original if a book is rewritten twice
        shutil.copyfile(path, backup)
    os.replace(tmp, path)
    return backup
