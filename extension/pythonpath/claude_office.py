"""Reading context from, and writing Claude's reply into, Writer and Calc documents.

Everything here takes plain UNO objects, so it also works over a remote
UNO bridge (that's how tests/test_uno.py drives it against headless soffice).
"""

import re

from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

WRITER = "writer"
CALC = "calc"

# Calc cells sent as context in one request; beyond this we ask for a smaller selection
# instead of silently truncating.
MAX_CELLS = 50000

SYSTEM_BASE = ("You are Claude, an AI assistant built into LibreOffice {app}. "
               "Your reply may be inserted straight into the user's {doc}, so reply with only "
               "the content asked for: no preamble (\"Here is...\"), no closing remarks.")

SYSTEM_WRITER = SYSTEM_BASE.format(app="Writer", doc="document") + (
    " Use plain text without Markdown (no **, #, or code fences); separate paragraphs with a "
    "blank line and write list items as \"- item\".")

SYSTEM_CALC = SYSTEM_BASE.format(app="Calc", doc="spreadsheet") + (
    " Spreadsheet content is shown as a tab-separated grid with column letters across the top "
    "and row numbers down the left. When the user wants values, a table or formulas put into "
    "the sheet, reply with ONLY tab-separated rows (no row numbers, no column letters, no "
    "Markdown, no code fences); the user writes them into the sheet at or just below the selection. Formulas start "
    "with \"=\", use English function names, and use \";\" as the argument separator, e.g. "
    "=IF(B2>0;\"yes\";\"no\"). Otherwise answer in concise plain text.")


class OfficeError(Exception):
    pass


def doc_kind(doc):
    if doc is None:
        return None
    if doc.supportsService("com.sun.star.text.TextDocument"):
        return WRITER
    if doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
        return CALC
    return None


def system_prompt(kind, extra=""):
    base = SYSTEM_WRITER if kind == WRITER else SYSTEM_CALC
    extra = (extra or "").strip()
    return base + ("\n\nAdditional instructions from the user:\n" + extra if extra else "")


# ---------------------------------------------------------------- context

class Context:
    """What gets sent to Claude, plus where to put the answer."""

    def __init__(self, kind, text, has_selection, label):
        self.kind = kind
        self.text = text
        self.has_selection = has_selection
        self.label = label          # one line for the dialog, e.g. "Selection: 42 words"


def get_context(doc):
    kind = doc_kind(doc)
    if kind == WRITER:
        return _writer_context(doc)
    if kind == CALC:
        return _calc_context(doc)
    raise OfficeError("Claude works in Writer documents and Calc spreadsheets.")


def _writer_ranges(doc):
    sel = doc.getCurrentController().getSelection()
    if sel is None or not sel.supportsService("com.sun.star.text.TextRanges"):
        return []
    return [sel.getByIndex(i) for i in range(sel.getCount())]


def _writer_context(doc):
    selected = "\n".join(r.getString() for r in _writer_ranges(doc) if r.getString())
    if selected.strip():
        return Context(WRITER, "Selected text:\n<selection>\n%s\n</selection>" % selected, True,
                       "Selection: %d words" % len(selected.split()))
    body = writer_document_text(doc)
    return Context(WRITER, "The whole document (nothing is selected):\n<document>\n%s\n</document>" % body,
                   False, "No selection - using the whole document (%d words)" % len(body.split()))


def writer_document_text(doc):
    """Main text including table contents, which XText.getString() leaves out."""
    parts = []
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        if el.supportsService("com.sun.star.text.TextTable"):
            rows = el.getRows().getCount()
            cols = el.getColumns().getCount()
            for r in range(rows):
                cells = []
                for c in range(cols):
                    try:
                        cells.append(el.getCellByName(_col_letters(c) + str(r + 1)).getString())
                    except Exception:
                        pass  # merged cells have no name
                parts.append("\t".join(cells))
        else:
            parts.append(el.getString())
    return "\n".join(parts)


def _calc_selection_address(doc):
    sel = doc.getCurrentController().getSelection()
    if hasattr(sel, "getRangeAddresses") and not hasattr(sel, "getRangeAddress"):
        addrs = sel.getRangeAddresses()   # multi-range selection: use the first
        if not addrs:
            raise OfficeError("Select some cells first.")
        return addrs[0]
    if hasattr(sel, "getRangeAddress"):
        return sel.getRangeAddress()
    raise OfficeError("Select some cells first (a chart or shape is selected).")


def _calc_context(doc):
    addr = _calc_selection_address(doc)
    sheet = doc.getSheets().getByIndex(addr.Sheet)
    single = addr.StartColumn == addr.EndColumn and addr.StartRow == addr.EndRow
    if single:
        cursor = sheet.createCursor()
        cursor.gotoEndOfUsedArea(False)
        end = cursor.getRangeAddress()
        grid_range = (0, 0, end.EndColumn, end.EndRow)
    else:
        grid_range = (addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)
    c0, r0, c1, r1 = grid_range
    cells = (c1 - c0 + 1) * (r1 - r0 + 1)
    if cells > MAX_CELLS:
        raise OfficeError("That's %d cells - too many to send. Select a smaller range (up to %d cells)."
                          % (cells, MAX_CELLS))
    data = sheet.getCellRangeByPosition(c0, r0, c1, r1).getFormulaArray()
    grid = format_grid(data, c0, r0)
    active = "%s%d" % (_col_letters(addr.StartColumn), addr.StartRow + 1)
    if single:
        empty = not any(v for row in data for v in row)
        text = ('Sheet "%s". The cursor is on cell %s. ' % (sheet.getName(), active)
                + ("The sheet is empty." if empty else "Used area of the sheet:\n" + grid))
        label = "Active cell %s - using the sheet's used area as context" % active
        return Context(CALC, text, False, label)
    ref = "%s:%s%d" % (active, _col_letters(addr.EndColumn), addr.EndRow + 1)
    text = 'Sheet "%s", selected range %s:\n%s' % (sheet.getName(), ref, grid)
    return Context(CALC, text, True, "Selection: %s (%d cells)" % (ref, cells))


def format_grid(data, col0, row0):
    ncols = max((len(r) for r in data), default=0)
    lines = ["\t" + "\t".join(_col_letters(col0 + c) for c in range(ncols))]
    for i, row in enumerate(data):
        lines.append("%d\t%s" % (row0 + i + 1, "\t".join(str(v) for v in row)))
    return "\n".join(lines)


def _col_letters(index):
    s = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        s = chr(65 + rem) + s
    return s


# ---------------------------------------------------------------- applying the reply

REPLACE = "replace"
INSERT_AFTER = "after"


def apply_result(doc, text, mode):
    kind = doc_kind(doc)
    undo = doc.getUndoManager()
    undo.enterUndoContext("Claude")
    try:
        if kind == WRITER:
            _writer_apply(doc, text, mode)
        elif kind == CALC:
            _calc_apply(doc, text, mode)
    finally:
        undo.leaveUndoContext()


def _writer_apply(doc, text, mode):
    ranges = _writer_ranges(doc)
    if ranges:
        target = ranges[0] if mode == REPLACE else ranges[-1]
    else:
        target = doc.getCurrentController().getViewCursor()
    body = target.getText()
    cursor = body.createTextCursorByRange(target if mode == REPLACE else target.getEnd())
    lines = text.split("\n")
    if mode == REPLACE:
        cursor.setString("")
    else:
        cursor.collapseToEnd()
        body.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
    for i, line in enumerate(lines):
        if i:
            body.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
        body.insertString(cursor, line, False)


def _calc_apply(doc, text, mode):
    addr = _calc_selection_address(doc)
    sheet = doc.getSheets().getByIndex(addr.Sheet)
    col = addr.StartColumn
    row = addr.StartRow if mode == REPLACE else addr.EndRow + 1
    for r, values in enumerate(parse_grid(text)):
        for c, value in enumerate(values):
            set_cell(sheet.getCellByPosition(col + c, row + r), value)


_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def set_cell(cell, value):
    value = value.strip()
    if value.startswith("=") and len(value) > 1:
        cell.setFormula(api_formula(value))
    elif _NUMBER.match(value) and not (len(value) > 1 and value[0] == "0" and value[1].isdigit()):
        cell.setValue(float(value))   # leading-zero codes like 007 stay text
    else:
        cell.setString(value)


def api_formula(formula):
    """The UNO API parses formulas with ';' separators; Claude often writes ','."""
    out, in_quotes = [], False
    for ch in formula:
        if ch == '"':
            in_quotes = not in_quotes
        out.append(";" if ch == "," and not in_quotes else ch)
    return "".join(out)


def strip_fences(text):
    m = re.match(r"^\s*```[^\n]*\n(.*?)\n?```\s*$", text, re.S)
    return m.group(1) if m else text


def parse_grid(text):
    lines = [l for l in strip_fences(text).split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    table = [l.strip() for l in lines if l.strip()]
    if table and all(l.startswith("|") for l in table):   # Markdown table
        rows = []
        for l in table:
            cells = [c.strip() for c in l.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            rows.append(cells)
        return rows
    return [l.split("\t") for l in lines]
