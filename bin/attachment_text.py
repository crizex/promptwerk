#!/usr/bin/env python3
"""Plain text from office formats, stdlib only.

    attachment_text.py sheet.xlsx        text to stdout
    text(path) -> str | None             as a module

Why: the planner reads attachments with the Read tool. Read handles text, images and PDF; an
.xlsx is a ZIP of XML and arrives as noise. Instead of giving the planner a shell, the server
puts a .txt version next to each such attachment.

Covered: xlsx, docx, pptx and the OpenDocument family. The old binary formats (.xls, .doc,
.ppt) cannot be opened without a third-party library; for those a sentence says exactly that.
"""
import re
import sys
import zipfile
from xml.etree import ElementTree as ET

ROWS_PER_SHEET = 200
MAX_CHARS = 200_000
OLD = {".xls": "Excel 97-2003", ".doc": "Word 97-2003", ".ppt": "PowerPoint 97-2003"}


def _tag(e):
    return e.tag.rsplit("}", 1)[-1]


def _col(ref):
    """'B7' -> 1. Without this, columns shift as soon as a cell is empty."""
    m = re.match(r"([A-Z]+)", ref or "")
    if not m:
        return None
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx(z):
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
            shared.append("".join(t.text or "" for t in si.iter() if _tag(t) == "t"))
    names = {}
    if "xl/workbook.xml" in z.namelist():
        for i, s in enumerate(e for e in ET.fromstring(z.read("xl/workbook.xml")).iter()
                              if _tag(e) == "sheet"):
            names[i] = s.get("name") or f"Sheet {i + 1}"
    out = []
    sheets = sorted(n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    for i, sheet in enumerate(sheets):
        out.append(f"## Sheet: {names.get(i, sheet)}")
        rows = 0
        for row in ET.fromstring(z.read(sheet)).iter():
            if _tag(row) != "row":
                continue
            rows += 1
            if rows > ROWS_PER_SHEET:
                out.append(f"[more rows omitted, over {ROWS_PER_SHEET}]")
                break
            cells = []
            for c in row:
                if _tag(c) != "c":
                    continue
                value, formula = "", ""
                for k in c:
                    if _tag(k) == "v":
                        value = k.text or ""
                    elif _tag(k) == "f":
                        formula = k.text or ""
                    elif _tag(k) == "is":
                        value = "".join(t.text or "" for t in k.iter() if _tag(t) == "t")
                if c.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                if formula:  # the formula is the real content of a computed cell
                    value = f"={formula}" + (f" [{value}]" if value else "")
                col = _col(c.get("r"))
                if col is not None:
                    while len(cells) < col:
                        cells.append("")
                cells.append(value)
            out.append("\t".join(cells))
        out.append("")
    return "\n".join(out)


def _paragraphs(z, pattern, breaks):
    out = []
    for name in sorted(n for n in z.namelist() if re.fullmatch(pattern, n)):
        if len(out) > 1:
            out.append("")
        for e in ET.fromstring(z.read(name)).iter():
            if _tag(e) in breaks:
                t = "".join(x.text or "" for x in e.iter()
                            if _tag(x) in ("t", "p", "span") and x.text)
                if t.strip():
                    out.append(t)
    return "\n".join(out)


def text(path):
    """Text version of an office file, or None if the format is not handled here."""
    ext = path[path.rfind("."):].lower() if "." in path else ""
    if ext in OLD:
        return (f"[{OLD[ext]} file: the old binary format cannot be opened here. "
                f"Please send it as .xlsx/.docx/.pptx or as PDF.]")
    if ext not in (".xlsx", ".xlsm", ".docx", ".pptx", ".odt", ".ods", ".odp"):
        return None
    try:
        with zipfile.ZipFile(path) as z:
            if ext in (".xlsx", ".xlsm"):
                raw = _xlsx(z)
            elif ext == ".docx":
                raw = _paragraphs(z, r"word/document\.xml", {"p"})
            elif ext == ".pptx":
                raw = _paragraphs(z, r"ppt/slides/slide\d+\.xml", {"p"})
            else:
                raw = _paragraphs(z, r"content\.xml", {"p", "h", "table-row"})
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError) as e:
        return f"[file could not be read: {type(e).__name__}]"
    if len(raw) > MAX_CHARS:
        raw = raw[:MAX_CHARS] + "\n[truncated]"
    return raw


if __name__ == "__main__":
    for p in sys.argv[1:]:
        t = text(p)
        print(t if t is not None else f"[{p}: not an office format]")
