"""Write Word and Excel files. Excel data goes in real Excel tables, because Copilot in Excel works on tables or clean ranges:
one header row, unique headers, no merged cells, no blank rows."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from docx import Document
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from ..corpus.documents import Doc

FIXED = datetime(2026, 1, 1)
NUM = re.compile(r"^-?\d+(\.\d+)?$")


def _new_docx(title: str) -> Document:
    d = Document()
    d.styles["Normal"].font.name = "Calibri"
    d.styles["Normal"].font.size = Pt(11)
    d.core_properties.author = "Maple Ridge Credit Union (invented)"
    d.core_properties.title = title
    d.core_properties.created = d.core_properties.modified = FIXED
    return d


def _table(d: Document, lines: Sequence[str]) -> None:
    head = [c.strip() for c in lines[0].split(" | ")]
    t = d.add_table(rows=1, cols=len(head))
    t.style = "Light Grid Accent 1"
    for i, h in enumerate(head):
        t.rows[0].cells[i].text = h
    for ln in lines[1:]:
        cells = ln.split(" | ")
        row = t.add_row().cells
        for i in range(len(head)):
            row[i].text = cells[i].strip() if i < len(cells) else ""


def write_docx(doc: Doc, path: Path) -> Path:
    """A document with numbered headings. Lines that start with 'Date | ID' become a real table."""
    d = _new_docx(doc.title)
    d.add_heading(doc.title, 0)
    for s in doc.sections:
        d.add_heading(f"{s.id}. {s.heading}" if doc.kind != "case" else s.heading, 1)
        buf: List[str] = []
        for p in s.paragraphs:
            if " | " in p:
                buf.append(p)
                continue
            if buf:
                _table(d, buf)
                buf = []
            d.add_paragraph(p)
        if buf:
            _table(d, buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(path)
    return path


def write_plain_docx(title: str, blocks: Sequence[Dict[str, Any]], path: Path) -> Path:
    """blocks: {h1|h2|p|bullets|numbered|table|quote: value}. Used for the guide, the answer key and the regulation excerpts."""
    d = _new_docx(title)
    d.add_heading(title, 0)
    for b in blocks:
        (kind, val), = b.items()
        if kind == "h1":
            d.add_heading(val, 1)
        elif kind == "h2":
            d.add_heading(val, 2)
        elif kind == "p":
            d.add_paragraph(val)
        elif kind == "quote":
            d.add_paragraph(val, style="Intense Quote")
        elif kind == "bullets":
            for v in val:
                d.add_paragraph(v, style="List Bullet")
        elif kind == "numbered":
            for v in val:
                d.add_paragraph(v, style="List Number")
        elif kind == "table":
            _table(d, val)
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(path)
    return path


def _cell(v: Any) -> Any:
    if isinstance(v, str):
        if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", v):
            return datetime.fromisoformat(v)
        if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            return datetime.fromisoformat(v)
        if NUM.match(v) and not (len(v) > 1 and v.startswith("0") and "." not in v):
            return float(v) if "." in v else int(v)
    return v


def add_table_sheet(wb: Workbook, title: str, columns: List[str], rows: List[List[Any]], table_name: str, first: bool = False) -> None:
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append(columns)
    for r in rows:
        ws.append([_cell(v) for v in r])
    if not rows:
        ws.append([""] * len(columns))          # a table needs at least one data row
    ref = f"A1:{get_column_letter(len(columns))}{max(len(rows), 1) + 1}"
    t = Table(displayName=re.sub(r"[^A-Za-z0-9_]", "_", table_name), ref=ref)
    t.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    ws.add_table(t)
    for i, c in enumerate(columns, 1):
        width = max([len(str(c))] + [len(str(r[i - 1])) for r in rows[:200]]) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(max(width, 10), 60)
        for cell in ws[get_column_letter(i)][1:]:
            if isinstance(cell.value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm" if c.lower() in ("date and time", "timestamp") else "yyyy-mm-dd"
            elif isinstance(cell.value, float):
                cell.number_format = "#,##0.00"
    ws.freeze_panes = "A2"


def write_xlsx(path: Path, sheets: Sequence[Dict[str, Any]]) -> Path:
    """sheets: [{title, columns, rows, name}] -> one Excel table per sheet."""
    wb = Workbook()
    wb.properties.creator = "Maple Ridge Credit Union (invented)"
    wb.properties.created = wb.properties.modified = FIXED
    for i, s in enumerate(sheets):
        add_table_sheet(wb, s["title"], s["columns"], s["rows"], s["name"], first=i == 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
