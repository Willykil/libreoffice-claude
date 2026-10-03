"""Reading context from, and writing Claude's reply into, Writer and Calc documents.
Impress presentations and Draw drawings are read only, for now.

Everything here takes plain UNO objects, so it also works over a remote
UNO bridge (that's how tests/test_uno.py drives it against headless soffice).
"""

import re

from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

WRITER = "writer"
CALC = "calc"
IMPRESS = "impress"
DRAW = "draw"
# Kinds Claude can write its reply into; in the others the reply is only shown (and copied).
WRITABLE = (WRITER, CALC)

# Calc cells sent as context in one request; beyond this we ask for a smaller selection
# instead of silently truncating.
MAX_CELLS = 50000
MAX_DOC_CHARS = 2000000

SYSTEM_BASE = ("You are Claude, an AI assistant built into LibreOffice {app}. "
               "Your reply may be inserted straight into the user's {doc}, so reply with only "
               "the content asked for: no preamble (\"Here is...\"), no closing remarks.")

SYSTEM_WRITER = SYSTEM_BASE.format(app="Writer", doc="document") + (
    " Use plain text without Markdown (no **, #, or code fences); separate paragraphs with a "
    "blank line and write list items as \"- item\"."
    " The document is shown with each paragraph numbered like [P12]. When you answer a question "
    "about the document, cite the paragraphs you rely on with those markers, e.g. [P12] or "
    "[P12-P14], so the user can click through to them. Never put markers in text that is meant "
    "to go into the document.")

SYSTEM_CALC = SYSTEM_BASE.format(app="Calc", doc="spreadsheet") + (
    " Spreadsheet content is shown as a tab-separated grid with column letters across the top "
    "and row numbers down the left. When the user wants values, a table or formulas put into "
    "the sheet, reply with ONLY tab-separated rows (no row numbers, no column letters, no "
    "Markdown, no code fences); the user writes them into the sheet at or just below the selection. Formulas start "
    "with \"=\", use English function names, and use \";\" as the argument separator, e.g. "
    "=IF(B2>0;\"yes\";\"no\"). Otherwise answer in concise plain text, and cite the cells you "
    "rely on in square brackets, e.g. [B3], [B2:B9] or [Data!C4], so the user can click through "
    "to them (never in content meant for the sheet).")

SYSTEM_SLIDES = (
    "You are Claude, an AI assistant built into LibreOffice {app}. Your reply is shown in a side "
    "panel next to the {doc}; it is not inserted into it. Reply with only what was asked: no "
    "preamble (\"Here is...\"), no closing remarks. Use plain text without Markdown (no **, #, or "
    "code fences); write list items as \"- item\". The {doc} is shown {page} by {page}, each "
    "marked like [S3] for {page} 3, with its text boxes, tables, pictures (by their alternative "
    "text){notes} and comments. Cite the {page}s you rely on with those markers, e.g. [S3] or "
    "[S3-S5], so the user can click through to them.")

SYSTEM_IMPRESS = SYSTEM_SLIDES.format(app="Impress", doc="presentation", page="slide", notes=", speaker notes")
SYSTEM_DRAW = SYSTEM_SLIDES.format(app="Draw", doc="drawing", page="page", notes="")


class OfficeError(Exception):
    pass


def doc_kind(doc):
    if doc is None:
        return None
    if doc.supportsService("com.sun.star.text.TextDocument"):
        return WRITER
    if doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
        return CALC
    if doc.supportsService("com.sun.star.presentation.PresentationDocument"):
        return IMPRESS     # before Draw: both are drawing documents underneath
    if doc.supportsService("com.sun.star.drawing.DrawingDocument"):
        return DRAW
    return None


def system_prompt(kind, extra=""):
    base = {WRITER: SYSTEM_WRITER, CALC: SYSTEM_CALC, IMPRESS: SYSTEM_IMPRESS, DRAW: SYSTEM_DRAW}[kind]
    extra = (extra or "").strip()
    return base + ("\n\nAdditional instructions from the user:\n" + extra if extra else "")


# ---------------------------------------------------------------- context

class Context:
    """What gets sent to Claude, plus where to put the answer."""

    def __init__(self, kind, text, has_selection, label, selection="", paragraph=None, before="", after=""):
        self.kind = kind
        self.text = text
        self.has_selection = has_selection
        self.label = label          # one line for the panel, e.g. "Selection: 42 words"
        # Writer only, for the panel's before/after view and page preview:
        self.selection = selection  # the selected text as it is now
        self.paragraph = paragraph  # [P<n>] number of the paragraph the selection starts in
        self.before = before        # the end of the paragraph before it
        self.after = after          # the start of the paragraph after it


def get_context(doc):
    kind = doc_kind(doc)
    if kind == WRITER:
        return _writer_context(doc)
    if kind == CALC:
        return _calc_context(doc)
    if kind in (IMPRESS, DRAW):
        return _slides_context(doc, kind)
    raise OfficeError("Claude works in Writer, Calc, Impress and Draw.")


def _writer_ranges(doc):
    sel = doc.getCurrentController().getSelection()
    if sel is None or not sel.supportsService("com.sun.star.text.TextRanges"):
        return []
    return [sel.getByIndex(i) for i in range(sel.getCount())]


def _writer_context(doc):
    selected = "\n".join(r.getString() for r in _writer_ranges(doc) if r.getString())
    paras = writer_paragraphs(doc)
    body = "\n".join("[P%d] %s" % (n, text) for n, text, _ in paras)
    if len(body) > MAX_DOC_CHARS:
        raise OfficeError("This document is too long to send in one go. Select the part you want "
                          "Claude to work on.")
    parts = ["The document, each paragraph numbered:\n<document>\n%s\n</document>" % body]
    extras = writer_review_notes(doc)
    if extras:
        parts.append(extras)
    words = sum(len(text.split()) for _, text, _ in paras)
    if selected.strip():
        parts.append("The user has selected this text:\n<selection>\n%s\n</selection>" % selected)
        number, before, after = _selection_neighbours(doc, paras)
        where = " in \u00b6%d" % number if number else ""
        return Context(WRITER, "\n\n".join(parts), True, "Selection: %d words%s" % (len(selected.split()), where),
                       selection=selected, paragraph=number, before=before, after=after)
    parts.append("Nothing is selected.")
    return Context(WRITER, "\n\n".join(parts), False,
                   "No selection - Claude reads the whole document (%d words)" % words)


def _selection_neighbours(doc, paras):
    """(paragraph number, end of the paragraph before, start of the one after) for the selection."""
    ranges = _writer_ranges(doc)
    if not ranges:
        return None, "", ""
    text = doc.getText()
    point = ranges[0].getStart()
    for i, (n, _, el) in enumerate(paras):
        if el.supportsService("com.sun.star.text.TextTable"):
            continue
        try:
            inside = text.compareRegionStarts(el, point) >= 0 and text.compareRegionEnds(point, el) >= 0
        except Exception:          # selection in a table, frame or footnote: another text
            return None, "", ""
        if inside:
            prev = next((t for _, t, e in reversed(paras[:i])
                         if not e.supportsService("com.sun.star.text.TextTable")), "")
            nxt = next((t for _, t, e in paras[i + 1:]
                        if not e.supportsService("com.sun.star.text.TextTable")), "")
            return n, prev[-160:], nxt[:160]
    return None, "", ""


def selection_summary(doc):
    """(label, has_selection): a cheap one-liner for the sidebar, without reading the whole document."""
    kind = doc_kind(doc)
    if kind == WRITER:
        words = len(selected_text(doc).split())
        if words:
            return "Selection: %d word%s" % (words, "" if words == 1 else "s"), True
        return "No selection - Claude reads the whole document", False
    if kind == CALC:
        try:
            a = _calc_selection_address(doc)
        except OfficeError as e:
            return str(e), False
        ref = _ref(a.StartColumn, a.StartRow, a.EndColumn, a.EndRow)
        if a.StartColumn == a.EndColumn and a.StartRow == a.EndRow:
            return "Cell %s - Claude reads the whole workbook" % ref, False
        return "Selection: %s" % ref, True
    if kind in (IMPRESS, DRAW):
        return _slides_label(doc, kind)
    return "Open a document, spreadsheet or presentation.", False


def document_text(doc):
    """Plain text of a Writer document's main text, for learning a writing voice from it."""
    return "\n".join(t.split(") ", 1)[1] if t.startswith("(Heading") or t.startswith("(Title)") else t
                     for _, t, el in writer_paragraphs(doc)
                     if not el.supportsService("com.sun.star.text.TextTable"))


def selected_text(doc):
    return "\n".join(r.getString() for r in _writer_ranges(doc) if r.getString())


def writer_paragraphs(doc):
    """[(number, text, element)] for the non-empty paragraphs and tables of the main text.

    The numbering is what Claude cites as [P12]; goto() recomputes it the same way.
    """
    out, n = [], 0
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        el = enum.nextElement()
        if el.supportsService("com.sun.star.text.TextTable"):
            text = "(table)\n" + _table_text(el)
        else:
            text = el.getString()
            if not text.strip():
                continue
            try:
                style = el.getPropertyValue("ParaStyleName")
                if style.startswith("Heading") or style == "Title":
                    text = "(%s) %s" % (style, text)
            except Exception:
                pass
        n += 1
        out.append((n, text, el))
    return out


def _table_text(table):
    rows = table.getRows().getCount()
    cols = table.getColumns().getCount()
    lines = []
    for r in range(rows):
        cells = []
        for c in range(cols):
            try:
                cells.append(table.getCellByName(_col_letters(c) + str(r + 1)).getString())
            except Exception:
                pass  # merged cells have no name
        lines.append("\t".join(cells))
    return "\n".join(lines)


def writer_review_notes(doc):
    """Comments and tracked changes, so Claude can summarize redlines or answer about comments."""
    comments = []
    try:
        fields = doc.getTextFields().createEnumeration()
        while fields.hasMoreElements():
            f = fields.nextElement()
            if f.supportsService("com.sun.star.text.textfield.Annotation"):
                anchor = f.getAnchor().getString().strip()
                on = ' (on "%s")' % anchor[:200] if anchor else ""
                comments.append("- %s%s: %s" % (f.Author or "Someone", on, f.Content))
    except Exception:
        pass
    changes = []
    try:
        redlines = doc.getRedlines().createEnumeration()
        while redlines.hasMoreElements():
            r = redlines.nextElement()
            kind = r.getPropertyValue("RedlineType")
            start, end = r.getPropertyValue("RedlineStart"), r.getPropertyValue("RedlineEnd")
            try:
                cur = start.getText().createTextCursorByRange(start)
                cur.gotoRange(end, True)
                text = cur.getString()
            except Exception:
                text = ""
            label = {"Insert": "Insertion", "Delete": "Deletion", "Format": "Formatting change"}.get(kind, kind)
            changes.append('- %s by %s: "%s"' % (label, r.getPropertyValue("RedlineAuthor") or "someone",
                                                 text[:500]))
    except Exception:
        pass
    parts = []
    if comments:
        parts.append("Comments in the document:\n" + "\n".join(comments))
    if changes:
        parts.append("Tracked changes in the document (the text above still shows them):\n"
                     + "\n".join(changes))
    return "\n\n".join(parts)


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


def _used_area(sheet):
    cursor = sheet.createCursor()
    cursor.gotoEndOfUsedArea(False)
    end = cursor.getRangeAddress()
    return 0, 0, end.EndColumn, end.EndRow


def _ref(c0, r0, c1, r1):
    a = "%s%d" % (_col_letters(c0), r0 + 1)
    return a if (c0, r0) == (c1, r1) else "%s:%s%d" % (a, _col_letters(c1), r1 + 1)


def _calc_context(doc):
    addr = _calc_selection_address(doc)
    sheets = doc.getSheets()
    active = sheets.getByIndex(addr.Sheet)
    single = addr.StartColumn == addr.EndColumn and addr.StartRow == addr.EndRow
    budget = MAX_CELLS
    parts = []
    names = [sheets.getByIndex(i).getName() for i in range(sheets.getCount())]
    parts.append("Workbook sheets: %s. The user is on sheet \"%s\"." % (", ".join(names), active.getName()))

    sel = (addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)
    ref = _ref(*sel)
    if not single:
        cells = (sel[2] - sel[0] + 1) * (sel[3] - sel[1] + 1)
        if cells > MAX_CELLS:
            raise OfficeError("That's %d cells - too many to send. Select a smaller range (up to %d cells)."
                              % (cells, MAX_CELLS))
        budget -= cells
        data = active.getCellRangeByPosition(*sel).getFormulaArray()
        parts.append("Selected range %s!%s:\n%s" % (_quote_sheet(active.getName()), ref,
                                                    format_grid(data, sel[0], sel[1])))
    else:
        parts.append("Nothing is selected; the cursor is on cell %s!%s." % (_quote_sheet(active.getName()), ref))

    # Every sheet's used area, active sheet first, while the cell budget lasts.
    order = [addr.Sheet] + [i for i in range(sheets.getCount()) if i != addr.Sheet]
    for i in order:
        sheet = sheets.getByIndex(i)
        area = _used_area(sheet)
        cells = (area[2] + 1) * (area[3] + 1)
        data = sheet.getCellRangeByPosition(*area).getFormulaArray()
        if not any(v for row in data for v in row):
            parts.append("Sheet \"%s\" is empty." % sheet.getName())
        elif cells > budget:
            parts.append("Sheet \"%s\" (%s, %d cells) is too large to include; select a range in it to "
                         "work on it." % (sheet.getName(), _ref(*area), cells))
        else:
            budget -= cells
            parts.append("Sheet \"%s\" (%s):\n%s" % (sheet.getName(), _ref(*area), format_grid(data, 0, 0)))
    text = "\n\n".join(parts)
    if single:
        return Context(CALC, text, False, "Cell %s - Claude reads the whole workbook" % ref)
    return Context(CALC, text, True, "Selection: %s (%d cells)" % (ref, (sel[2] - sel[0] + 1) * (sel[3] - sel[1] + 1)))


def _quote_sheet(name):
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name) else "'%s'" % name.replace("'", "''")


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


# ---------------------------------------------------------------- Impress and Draw

def _page_word(kind):
    return "slide" if kind == IMPRESS else "page"


def _current_page_number(doc):
    """1-based number of the slide/page on screen, or None."""
    try:
        current = doc.getCurrentController().getCurrentPage()
        pages = doc.getDrawPages()
        for i in range(pages.getCount()):
            if pages.getByIndex(i) == current:
                return i + 1
    except Exception:
        pass
    return None


def _selected_shapes(doc):
    sel = doc.getCurrentController().getSelection()
    if sel is None or not hasattr(sel, "getCount") or not hasattr(sel, "getByIndex"):
        return []
    return [sel.getByIndex(i) for i in range(sel.getCount())]


def _prop(obj, name, default=None):
    try:
        return obj.getPropertyValue(name)
    except Exception:
        return default


def _cell_table_text(model):
    """A drawing table (TableShape.Model) as tab-separated rows."""
    rows, cols = model.getRows().getCount(), model.getColumns().getCount()
    return "\n".join("\t".join(model.getCellByPosition(c, r).getString() for c in range(cols))
                     for r in range(rows))


def _bullets(shape):
    """An outline placeholder's paragraphs as "- item" lines, indented by level."""
    lines = []
    enum = shape.getText().createEnumeration()
    while enum.hasMoreElements():
        para = enum.nextElement()
        text = para.getString()
        if text.strip():
            level = _prop(para, "NumberingLevel") or 0
            lines.append("  " * max(level, 0) + "- " + text)
    return "\n".join(lines)


_LABELS = {"TitleTextShape": "Title", "SubtitleShape": "Subtitle"}


def shape_lines(shape):
    """What one shape says, as lines of text (empty placeholders and decorations say nothing)."""
    kind = shape.getShapeType().rsplit(".", 1)[-1]
    if kind == "GroupShape":
        return [line for i in range(shape.getCount()) for line in shape_lines(shape.getByIndex(i))]
    if kind == "PageShape" or _prop(shape, "IsEmptyPresentationObject"):
        return []
    if kind == "TableShape":
        return ["(table)\n" + _cell_table_text(shape.Model)]
    alt = " ".join(x for x in (_prop(shape, "Title") or "", _prop(shape, "Description") or "") if x.strip())
    if kind in ("GraphicObjectShape", "OLE2Shape", "MediaShape") or kind.endswith("ChartShape"):
        what = "chart" if kind == "OLE2Shape" else "picture"
        return ["(%s%s)" % (what, ": " + alt if alt else "")]
    if kind == "OutlinerShape":
        text = _bullets(shape)
        return [text] if text else []
    try:
        text = shape.getString()
    except Exception:
        text = ""
    if not text.strip():
        return ["(shape: %s)" % alt] if alt else []
    label = _LABELS.get(kind)
    return ["%s: %s" % (label, text) if label else text]


def _page_text(page, number, kind):
    word = _page_word(kind)
    head = "[S%d] %s %d" % (number, word.capitalize(), number)
    name = page.getName() if hasattr(page, "getName") else ""
    if name and not re.fullmatch(r"(page|slide)\s*\d+", name, re.I):
        head += ' "%s"' % name
    if kind == IMPRESS and _prop(page, "Visible") is False:
        head += " (hidden)"
    lines = [head]
    for i in range(page.getCount()):
        lines.extend(shape_lines(page.getByIndex(i)))
    if kind == IMPRESS:
        try:
            notes = page.getNotesPage()
            text = "\n".join(notes.getByIndex(i).getString() for i in range(notes.getCount())
                             if notes.getByIndex(i).getShapeType().endswith("NotesShape")
                             and not _prop(notes.getByIndex(i), "IsEmptyPresentationObject")).strip()
            if text:
                lines.append("Speaker notes: " + text)
        except Exception:
            pass
    try:
        comments = page.createAnnotationEnumeration()
        while comments.hasMoreElements():
            c = comments.nextElement()
            lines.append("Comment by %s: %s" % (c.Author or "someone", c.TextRange.getString()))
    except Exception:
        pass
    if len(lines) == 1:
        lines.append("(nothing written on this %s)" % word)
    return "\n".join(lines)


def _slides_label(doc, kind):
    """(label, has_selection) for the panel."""
    word = _page_word(kind)
    number = _current_page_number(doc)
    shapes = _selected_shapes(doc)
    where = " on %s %d" % (word, number) if number else ""
    if shapes:
        return "Selection: %d shape%s%s" % (len(shapes), "" if len(shapes) == 1 else "s", where), True
    count = doc.getDrawPages().getCount()
    whole = "presentation" if kind == IMPRESS else "drawing"
    return ("%s%s - Claude reads the whole %s (%d %s%s)"
            % (word.capitalize(), " %d" % number if number else "", whole, count, word, "" if count == 1 else "s"),
            False)


def _slides_context(doc, kind):
    word = _page_word(kind)
    pages = doc.getDrawPages()
    body = "\n\n".join(_page_text(pages.getByIndex(i), i + 1, kind) for i in range(pages.getCount()))
    if len(body) > MAX_DOC_CHARS:
        raise OfficeError("This file is too long to send in one go.")
    whole = "presentation" if kind == IMPRESS else "drawing"
    parts = ["The %s, %s by %s:\n<%s>\n%s\n</%s>" % (whole, word, word, whole, body, whole)]
    number = _current_page_number(doc)
    if number:
        parts.append("The user is on %s %d." % (word, number))
    label, has_selection = _slides_label(doc, kind)
    selected = "\n".join(line for s in _selected_shapes(doc) for line in shape_lines(s))
    if has_selection and selected.strip():
        parts.append("The user has selected this on %s %s:\n<selection>\n%s\n</selection>"
                     % (word, number or "?", selected))
    elif has_selection:
        parts.append("The user has selected shapes without text.")
    else:
        parts.append("Nothing is selected.")
    return Context(kind, "\n\n".join(parts), has_selection, label, selection=selected if has_selection else "")


# ---------------------------------------------------------------- applying the reply

REPLACE = "replace"
INSERT_AFTER = "after"


def apply_result(doc, text, mode, track_changes=False):
    kind = doc_kind(doc)
    if kind not in WRITABLE:
        # Impress/Draw API edits don't reach the Undo stack, so Ctrl+Z couldn't take them back.
        raise OfficeError("Claude can't write into %ss yet; copy the reply instead." % _page_word(kind))
    undo = doc.getUndoManager()
    undo.enterUndoContext("Claude")
    try:
        if kind == WRITER:
            previous = doc.getPropertyValue("RecordChanges")
            doc.setPropertyValue("RecordChanges", bool(track_changes) or previous)
            try:
                _writer_apply(doc, strip_citations(text), mode)
            finally:
                doc.setPropertyValue("RecordChanges", previous)
        elif kind == CALC:
            _calc_apply(doc, text, mode)
    finally:
        undo.leaveUndoContext()


_CITATION = re.compile(r"\s?\[P\d+(?:\s*[-\u2013]\s*P?\d+)?\]")


def strip_citations(text):
    return _CITATION.sub("", text)


def calc_target(doc, text, mode):
    """(sheet, first col, first row, rows) that a write would touch."""
    addr = _calc_selection_address(doc)
    sheet = doc.getSheets().getByIndex(addr.Sheet)
    row = addr.StartRow if mode == REPLACE else addr.EndRow + 1
    return sheet, addr.StartColumn, row, parse_grid(text)


def calc_overwrites(doc, text, mode):
    """Cells that already have content and would be overwritten: (count, 'B4:C6' or None)."""
    sheet, col, row, rows = calc_target(doc, text, mode)
    hits = []
    for r, values in enumerate(rows):
        for c in range(len(values)):
            cell = sheet.getCellByPosition(col + c, row + r)
            if cell.getFormula() != "":
                hits.append((col + c, row + r))
    if not hits:
        return 0, None
    cs, rs = [h[0] for h in hits], [h[1] for h in hits]
    return len(hits), _ref(min(cs), min(rs), max(cs), max(rs))


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
    sheet, col, row, rows = calc_target(doc, text, mode)
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            set_cell(sheet.getCellByPosition(col + c, row + r), value)
    if rows:   # select what was written, so it's easy to see
        width = max(len(v) for v in rows)
        doc.getCurrentController().select(sheet.getCellRangeByPosition(col, row, col + width - 1,
                                                                        row + len(rows) - 1))


# ---------------------------------------------------------------- citations

_WRITER_REF = re.compile(r"^P(\d+)(?:\s*[-\u2013]\s*P?(\d+))?$")
_CELL = r"\$?[A-Z]{1,3}\$?\d{1,7}"
_SLIDE_REF = re.compile(r"^S(\d+)(?:\s*[-\u2013]\s*S?(\d+))?$")
_CALC_REF = re.compile(r"^(?:(?P<sheet>'(?:[^']|'')+'|[^!:'\[\]]+)!)?(?P<range>%s(?::%s)?)$" % (_CELL, _CELL))


def goto(doc, ref):
    """Select what a citation like P12, P3-P5, B7, B2:C9 or 'Data'!C4 points at."""
    ref = (ref or "").strip()
    kind = doc_kind(doc)
    controller = doc.getCurrentController()
    if kind == WRITER:
        m = _WRITER_REF.match(ref)
        if not m:
            raise OfficeError("Unknown reference: %s" % ref)
        first, last = int(m.group(1)), int(m.group(2) or m.group(1))
        paras = {n: el for n, _, el in writer_paragraphs(doc)}
        if first not in paras:
            raise OfficeError("Paragraph %d isn't in the document any more." % first)
        a, b = paras[first], paras.get(last, paras[first])
        if a.supportsService("com.sun.star.text.TextTable"):
            controller.select(a)
            return
        cursor = a.getText().createTextCursorByRange(a.getStart())
        try:
            cursor.gotoRange(b.getEnd() if not b.supportsService("com.sun.star.text.TextTable") else a.getEnd(), True)
        except Exception:
            cursor.gotoRange(a.getEnd(), True)
        controller.select(cursor)
        return
    if kind == CALC:
        m = _CALC_REF.match(ref.replace(" ", ""))
        if not m:
            raise OfficeError("Unknown reference: %s" % ref)
        sheets = doc.getSheets()
        name = m.group("sheet")
        if name:
            name = name[1:-1].replace("''", "'") if name.startswith("'") else name
            if not sheets.hasByName(name):
                raise OfficeError("There's no sheet called %s." % name)
            sheet = sheets.getByName(name)
        else:
            sheet = controller.getActiveSheet()
        controller.setActiveSheet(sheet)
        controller.select(sheet.getCellRangeByName(m.group("range").replace("$", "")))
        return
    if kind in (IMPRESS, DRAW):
        m = _SLIDE_REF.match(ref)
        if not m:
            raise OfficeError("Unknown reference: %s" % ref)
        pages = doc.getDrawPages()
        n = int(m.group(1))
        if not 1 <= n <= pages.getCount():
            raise OfficeError("%s %d isn't there any more." % (_page_word(kind).capitalize(), n))
        controller.setCurrentPage(pages.getByIndex(n - 1))
        return
    raise OfficeError("Open a document, spreadsheet or presentation first.")


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
