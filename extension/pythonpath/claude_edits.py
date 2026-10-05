"""Claude acting on the document: formatting, find and replace, comments, cells.

Claude can't call LibreOffice itself (the connection is a plain question and
answer), so it describes its edits in an <edits> block of JSON at the end of
its reply. This module reads that block and applies it through UNO, in one
undo step, so Ctrl+Z (or the panel's Undo) takes all of it back. In Writer,
"Edits as tracked changes" records them as changes to accept or reject.

parse() needs no UNO, so it's unit-testable with a plain Python.
"""

import json
import re

UNDO_TITLE = "Claude edits"
MAX_OPS = 1000

WRITER_HELP = """

You can also change the document directly, the way you would in Word. When the user asks you to change, format, mark up, highlight, underline, comment on, correct, fill in or reorganize the document, do it yourself: never say you can only insert plain text, and never hand the user a macro or a list of steps to do it by hand. End your reply with an <edits> block holding a JSON array of operations; they are applied as soon as you answer, in one step the user can undo. Before the block, say in a sentence or two, in the user's language, what you changed (that note may use [P12] markers). When the user only wants a rewrite of the selected text, you may still reply with just the new text (no block) so they can review it.

Each operation picks its target with one of:
- "para": 12 (the paragraph marked [P12]) or "para": [12, 15] (paragraphs 12 to 15)
- "find": "exact text" or a list of them, copied exactly from the document, each within one paragraph; add "para" to search only there (recommended), and "occurrence": 2 to take only the 2nd match (default: every match)
- "selection": true (the user's selection)
- "para": 12, "cell": "B2" (a cell of the table marked [P12]; tables are shown cell by cell, like "B2: (empty)")
Operations:
- {"op": "format", <target>, "bold": true, "italic": true, "underline": true | "double" | "wave" | false, "strikethrough": true, "highlight": "yellow" | "green" | "cyan" | "pink" | "orange" | "#RRGGBB" | "none", "color": "red" | "#RRGGBB" | "auto", "font": "Arial", "size": 12, "align": "left" | "center" | "right" | "justify", "style": "Heading 1"} (any of these)
- {"op": "replace", <target>, "with": "new text"} (corrections, find and replace)
- {"op": "insert", <target>, "text": "...", "position": "after" | "before"} (after or before whole paragraphs adds new paragraphs; after found text inserts inline). "at": "end" or "start" instead of a target adds paragraphs at the end or start of the document. Separate paragraphs with \\n; "style" sets their paragraph style.
- {"op": "delete", <target>} (a whole paragraph target removes the paragraph)
- {"op": "comment", <target>, "text": "the comment"}
- {"op": "fill", "para": 12, "cells": {"A2": "text", "B2": "line 1\\nline 2"}} (sets the text of table cells, empty or not: use it to answer in a table's blank cells, never put the answer under the table instead)
Inserting "after" or "before" a table's "para" adds paragraphs right after or before the table.
Example:
<edits>
[{"op": "format", "para": 4, "find": ["la mémoire de travail", "surcharge"], "highlight": "yellow"},
 {"op": "format", "para": 7, "find": "En conclusion", "underline": true, "bold": true},
 {"op": "comment", "para": 9, "find": "cette étude", "text": "Which study? Add the reference."}]
</edits>
Keep "find" texts short (a few words up to a sentence) but long enough to be unique in their paragraph. Never put [P12] markers inside "find", "with" or "text"."""

CALC_HELP = """

You can also change the workbook directly, the way you would in Excel. When the user asks you to change, fill in, fix, format, color, comment on or reorganize cells, do it yourself: never say you can't, and never hand the user a macro or steps to do it by hand. End your reply with an <edits> block holding a JSON array of operations; they are applied as soon as you answer, in one step the user can undo. Before the block, say in a sentence or two, in the user's language, what you changed (citing cells like [B3]).

Ranges are written like "B3", "B2:D9" or "Data!A1:C4" ('My sheet'!A1 when the name has spaces); "range": "selection" is the user's selection. Operations:
- {"op": "set", "range": "B2", "values": [["Total", "=SUM(B2:B9)"], [1, 2]]} (rows of values from the range's top-left cell; formulas start with "=", use English function names; a single "value" also works)
- {"op": "format", "range": "A1:D1", "bold": true, "italic": true, "underline": true, "color": "red" | "#RRGGBB", "background": "yellow" | "#RRGGBB" | "none", "font": "Arial", "size": 11, "number_format": "#,##0.00" | "0%" | "YYYY-MM-DD", "align": "left" | "center" | "right", "wrap": true, "border": true}
- {"op": "clear", "range": "C2:C9", "formatting": false} (contents only, unless "formatting": true)
- {"op": "comment", "range": "B7", "text": "the comment"}
- {"op": "insert_rows", "sheet": "Data", "at": 5, "count": 2} / {"op": "delete_rows", "at": 5, "count": 2} (row numbers)
- {"op": "insert_columns", "at": "C", "count": 1} / {"op": "delete_columns", "at": "C", "count": 1}
Example:
<edits>
[{"op": "set", "range": "A10", "values": [["Total", "=SUM(B2:B9)"]]},
 {"op": "format", "range": "A10:B10", "bold": true, "background": "#FFF2CC"}]
</edits>"""

_BLOCK = re.compile(r"<edits>\s*(.*?)\s*</edits>", re.S)
_FENCE = re.compile(r"^```[a-z]*\s*(.*?)\s*```$", re.S)


def parse(text):
    """(text without the block, operations, problem or None)."""
    blocks = list(_BLOCK.finditer(text or ""))
    if not blocks:
        # A reply cut off at the length limit loses its closing tag.
        start = (text or "").find("<edits>")
        if start < 0:
            return text, [], None
        return text[:start].rstrip(), [], "Claude's edits were cut off before the end, so none were made."
    clean = _BLOCK.sub("", text)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    ops = []
    for m in blocks:
        raw = m.group(1).strip()
        fence = _FENCE.match(raw)
        if fence:
            raw = fence.group(1)
        try:
            data = json.loads(raw)
        except ValueError:
            return clean, [], "Claude's edits couldn't be read, so none were made."
        if isinstance(data, dict):
            data = data.get("edits", [data])
        if not isinstance(data, list):
            return clean, [], "Claude's edits couldn't be read, so none were made."
        ops.extend(op for op in data if isinstance(op, dict))
    if len(ops) > MAX_OPS:
        return clean, [], "Claude asked for %d edits at once; that's too many (up to %d)." % (len(ops), MAX_OPS)
    return clean, ops, None


def summary_for_history(text, edits):
    """The reply as later turns of the conversation see it, with what was actually applied."""
    if not edits:
        return text
    lines = [text] if text else []
    if edits.get("done"):
        lines.append("(Edits applied: %s.)" % "; ".join(edits["done"]))
    if edits.get("failed"):
        lines.append("(Edits that failed: %s.)" % "; ".join(edits["failed"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- colors and labels

COLORS = {
    "yellow": 0xFFFF00, "green": 0x00FF00, "lime": 0x00FF00, "light green": 0x90EE90,
    "cyan": 0x00FFFF, "turquoise": 0x00FFFF, "blue": 0x0000FF, "light blue": 0xADD8E6,
    "pink": 0xFF66CC, "magenta": 0xFF00FF, "red": 0xFF0000, "orange": 0xFFA500,
    "purple": 0x800080, "violet": 0xEE82EE, "gray": 0x808080, "grey": 0x808080,
    "light gray": 0xD3D3D3, "light grey": 0xD3D3D3, "black": 0x000000, "white": 0xFFFFFF,
    "dark red": 0x8B0000, "dark green": 0x006400, "dark blue": 0x00008B, "brown": 0x8B4513,
}
NO_COLOR = -1        # COL_TRANSPARENT / COL_AUTO


def color(value):
    """A UNO color int, NO_COLOR for none/auto, or raise ValueError."""
    if value is None or value is False:
        return NO_COLOR
    v = str(value).strip().lower()
    if v in ("", "none", "auto", "automatic", "transparent", "no", "off", "false"):
        return NO_COLOR
    if v in COLORS:
        return COLORS[v]
    m = re.fullmatch(r"#?([0-9a-f]{6})", v)
    if m:
        return int(m.group(1), 16)
    m = re.fullmatch(r"#?([0-9a-f]{3})", v)
    if m:
        return int("".join(c * 2 for c in m.group(1)), 16)
    raise ValueError("unknown color %r" % value)


def _short(text, n=40):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _para_label(para):
    if isinstance(para, list) and len(para) == 2 and para[0] != para[1]:
        return "¶%s–%s" % (para[0], para[1])
    if isinstance(para, list):
        para = para[0]
    return "¶%s" % para


def _cell_names(op):
    cells = op.get("cell")
    return [str(c).strip().upper() for c in (cells if isinstance(cells, list) else [cells])] if cells else []


def _target_label(op):
    finds = op.get("find")
    where = " in " + _para_label(op["para"]) if op.get("para") is not None else ""
    cells = _cell_names(op)
    if cells:
        where = " in cell%s %s of %s" % ("s" if len(cells) > 1 else "", ", ".join(cells),
                                         _para_label(op.get("para")))
    if finds:
        finds = finds if isinstance(finds, list) else [finds]
        what = ", ".join("“%s”" % _short(f, 30) for f in finds[:3])
        if len(finds) > 3:
            what += " +%d more" % (len(finds) - 3)
        return what + where
    if cells:
        return where[4:]
    if op.get("para") is not None:
        return _para_label(op["para"])
    if op.get("selection"):
        return "the selection"
    if op.get("at") in ("end", "start"):
        return "the %s of the document" % op["at"]
    return "?"


_UNDERLINES = {"single": 1, "double": 2, "dotted": 3, "dash": 5, "dashed": 5, "wave": 10, "wavy": 10,
               "bold": 12, "thick": 12}


def _underline(value):
    if value is True:
        return 1
    if not value or str(value).lower() in ("none", "false", "off", "no"):
        return 0
    v = str(value).lower()
    if v not in _UNDERLINES:
        raise ValueError("unknown underline %r" % value)
    return _UNDERLINES[v]


def format_label(op):
    parts = []
    for key, on, off in (("bold", "bold", "not bold"), ("italic", "italic", "not italic"),
                         ("strikethrough", "struck through", "not struck through"),
                         ("wrap", "wrapped", "not wrapped"), ("border", "borders", "no borders")):
        if key in op:
            parts.append(on if op[key] else off)
    if "underline" in op:
        parts.append("underlined" if op["underline"] and str(op["underline"]).lower() not in ("none", "false")
                     else "not underlined")
    for key, word in (("highlight", "highlight"), ("background", "fill")):
        if key in op:
            v = op[key]
            parts.append("no " + word if color_or_none(v) == NO_COLOR else "%s %s" % (v, word))
    if "color" in op:
        parts.append("%s text" % op["color"])
    if "font" in op:
        parts.append(str(op["font"]))
    if "size" in op:
        parts.append("%s pt" % op["size"])
    if "align" in op:
        parts.append("aligned %s" % op["align"])
    if "style" in op:
        parts.append("style “%s”" % op["style"])
    if "number_format" in op:
        parts.append("format %s" % op["number_format"])
    return ", ".join(parts) or "formatted"


def _cap(text):
    return text[:1].upper() + text[1:]


def color_or_none(value):
    try:
        return color(value)
    except ValueError:
        return None


class EditError(Exception):
    pass


# ---------------------------------------------------------------- applying

def apply(doc, ops, track_changes=False, paragraphs=None):
    """Apply operations to a Writer or Calc document in one undo step.

    paragraphs: the [(number, text, element)] Claude was shown, so [P12] means the paragraph
    it saw even if the user typed in the meantime. Returns {"done": [...], "failed": [...]}.
    """
    import claude_office
    kind = claude_office.doc_kind(doc)
    if kind not in claude_office.WRITABLE:
        return {"done": [], "failed": ["Claude can't edit %ss yet." % claude_office._page_word(kind)]}
    done, failed = [], []
    undo = doc.getUndoManager()
    undo.enterUndoContext(UNDO_TITLE)
    doc.lockControllers()          # redraw once at the end, not after every edit
    try:
        if kind == claude_office.WRITER:
            previous = doc.getPropertyValue("RecordChanges")
            doc.setPropertyValue("RecordChanges", bool(track_changes) or previous)
            try:
                writer = _Writer(doc, paragraphs)
                for op in ops:
                    _run(writer, op, done, failed)
            finally:
                doc.setPropertyValue("RecordChanges", previous)
        else:
            calc = _Calc(doc)
            for op in ops:
                _run(calc, op, done, failed)
    finally:
        doc.unlockControllers()
        undo.leaveUndoContext()
    if not done:
        # Nothing changed: don't leave an empty "Claude edits" step on the undo stack.
        try:
            if undo.isUndoPossible() and undo.getCurrentUndoActionTitle() == UNDO_TITLE:
                undo.undo()
                undo.clearRedo()
        except Exception:
            pass
    return {"done": done, "failed": failed}


def _run(editor, op, done, failed):
    name = str(op.get("op") or "").lower()
    fn = getattr(editor, "op_" + name, None)
    if fn is None:
        failed.append("Unknown edit “%s”" % name)
        return
    try:
        done.append(fn(op))
    except EditError as e:
        failed.append(str(e))
    except Exception as e:    # a bad value or a UNO error: report it, keep going
        failed.append("%s: %s" % (name.capitalize(), _short(getattr(e, "Message", "") or e, 120)))


def undo_last(doc):
    """Undo the latest Claude edits, if they are still the last thing done."""
    undo = doc.getUndoManager()
    if not undo.isUndoPossible() or undo.getCurrentUndoActionTitle() != UNDO_TITLE:
        raise EditError("Something else was changed since; use Edit > Undo in LibreOffice instead.")
    undo.undo()


# ---------------------------------------------------------------- Writer

def _enum(type_name, value):
    import uno
    return uno.Enum(type_name, value)


_ALIGN = {"left": 0, "right": 1, "justify": 2, "justified": 2, "block": 2, "center": 3, "centre": 3}


def _now():
    import time
    import uno
    t = time.localtime()
    dt = uno.createUnoStruct("com.sun.star.util.DateTime")
    dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes, dt.Seconds = t[0], t[1], t[2], t[3], t[4], t[5]
    return dt


def _quote_pattern(text):
    """An ICU regex for text, forgiving curly vs straight quotes, dashes and spacing."""
    out = []
    for ch in text:
        if ch in "'‘’ʼ":
            out.append("['‘’ʼ]")
        elif ch in "\"“”«»":
            out.append("[\"“”«»]")
        elif ch in "-‐‑‒–—":
            out.append("[-‐‑‒–—]")
        elif ch.isspace() or ch == " " or ch == " ":
            if not out or out[-1] != "\\s+":
                out.append("\\s+")
        elif ch.isalnum():
            out.append(ch)
        else:
            out.append("\\x{%04X}" % ord(ch))
    return "".join(out)


class _Writer:
    def __init__(self, doc, paragraphs):
        import claude_office
        self.doc = doc
        self.office = claude_office
        self.paras = {n: el for n, _, el in (paragraphs or claude_office.writer_paragraphs(doc))}

    # -- targets

    def _para_numbers(self, op):
        p = op.get("para")
        if p is None:
            return None
        if isinstance(p, str) and re.fullmatch(r"\s*P?\d+\s*([-–]\s*P?\d+\s*)?", p):
            p = [int(x) for x in re.findall(r"\d+", p)]
        if isinstance(p, (int, float)) and not isinstance(p, bool):
            p = [int(p)]
        if not isinstance(p, list) or not p or not all(isinstance(x, (int, float)) for x in p):
            raise EditError("Bad paragraph number %r" % (op.get("para"),))
        first, last = int(p[0]), int(p[-1])
        if first > last:
            first, last = last, first
        missing = [n for n in (first, last) if n not in self.paras]
        if missing:
            raise EditError("There's no paragraph ¶%d" % missing[0])
        return [n for n in range(first, last + 1) if n in self.paras]

    def _is_table(self, el):
        return el.supportsService("com.sun.star.text.TextTable")

    def _table_of(self, op):
        numbers = self._para_numbers(op)
        if not numbers or len(numbers) != 1 or not self._is_table(self.paras[numbers[0]]):
            raise EditError("%s: a \"cell\" needs \"para\" set to the table's number"
                            % str(op.get("op")).capitalize())
        return self.paras[numbers[0]]

    def _cell(self, table, name):
        try:
            cell = table.getCellByName(str(name).strip().upper())
        except Exception:
            cell = None
        if cell is None:
            raise EditError("The table has no cell %s" % name)
        return cell

    def _cell_range(self, cell):
        cur = cell.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        return cur

    def _para_range(self, el):
        """A text cursor over a whole paragraph, or one per cell for a table."""
        if self._is_table(el):
            out = []
            for name in el.getCellNames():
                cell = el.getCellByName(name)
                cur = cell.createTextCursor()
                cur.gotoEnd(True)
                out.append(cur)
            return out
        cur = el.getText().createTextCursorByRange(el.getStart())
        cur.gotoRange(el.getEnd(), True)
        return [cur]

    def _inside(self, rng, el):
        if self._is_table(el):
            text = rng.getText()
            return any(el.getCellByName(n) == text for n in el.getCellNames())
        try:
            t = el.getText()
            return t.compareRegionStarts(el, rng) >= 0 and t.compareRegionEnds(rng, el) >= 0
        except Exception:      # another text (a table cell, a frame, a footnote)
            return False

    def _search(self, needle, scope, regex, case):
        desc = self.doc.createSearchDescriptor()
        desc.SearchString = needle
        desc.SearchRegularExpression = regex
        desc.SearchCaseSensitive = case
        if scope is None or any(self._is_table(el) for el in scope):
            found = self.doc.findAll(desc)
            hits = [found.getByIndex(i) for i in range(found.getCount())]
            if scope is not None:
                hits = [h for h in hits if any(self._inside(h, el) for el in scope)]
            return hits
        # Search just the paragraphs asked for, not the whole document each time.
        hits = []
        for el in scope:
            start = el.getStart()
            while True:
                hit = self.doc.findNext(start, desc)
                if hit is None or not self._inside(hit, el):
                    break
                hits.append(hit)
                start = hit.getEnd()
        return hits

    def _find(self, needle, scope):
        needle = str(needle)
        if not needle.strip():
            raise EditError("Empty text to find")
        if "\n" in needle.strip():
            raise EditError("“%s” spans paragraphs; find text one paragraph at a time"
                            % _short(needle))
        needle = needle.strip("\n")
        for regex, case in ((False, True), (True, True), (True, False)):
            hits = self._search(_quote_pattern(needle) if regex else needle, scope, regex, case)
            if hits:
                return hits
        return []

    def targets(self, op):
        """(ranges, label, whole_paragraphs): what an operation acts on."""
        label = _target_label(op)
        numbers = self._para_numbers(op)
        scope = [self.paras[n] for n in numbers] if numbers else None
        cells = None
        if op.get("cell"):
            table = self._table_of(op)
            cells = [self._cell(table, name) for name in _cell_names(op)]
        finds = op.get("find")
        if finds:
            finds = finds if isinstance(finds, list) else [finds]
            ranges, missing = [], []
            for f in finds:
                hits = self._find(f, scope)
                if cells:
                    hits = [h for h in hits if any(h.getText() == c for c in cells)]
                occ = op.get("occurrence")
                if hits and isinstance(occ, int) and not isinstance(occ, bool):
                    hits = hits[occ - 1:occ] if 1 <= occ <= len(hits) else []
                if hits:
                    ranges.extend(hits)
                else:
                    missing.append(f)
            if missing and not ranges:
                where = " in " + label.rsplit(" in ", 1)[1] if numbers else ""
                raise EditError("Couldn't find %s%s" % (", ".join("“%s”" % _short(f) for f in missing),
                                                       where))
            if missing:
                label += " (not found: %s)" % ", ".join("“%s”" % _short(f, 30) for f in missing)
            return ranges, label, False
        if cells:
            return [self._cell_range(c) for c in cells], label, False
        if numbers:
            return [r for n in numbers for r in self._para_range(self.paras[n])], label, True
        if op.get("selection"):
            ranges = [r for r in self.office._writer_ranges(self.doc) if r.getString()]
            if not ranges:
                raise EditError("Nothing is selected")
            return ranges, label, False
        raise EditError("%s: say where (\"para\", \"find\" or \"selection\")" % str(op.get("op")).capitalize())

    # -- operations

    def _set_char(self, rng, op):
        if "bold" in op:
            rng.setPropertyValue("CharWeight", 150.0 if op["bold"] else 100.0)
        if "italic" in op:
            rng.setPropertyValue("CharPosture", _enum("com.sun.star.awt.FontSlant",
                                                      "ITALIC" if op["italic"] else "NONE"))
        if "underline" in op:
            rng.setPropertyValue("CharUnderline", _underline(op["underline"]))
        if "strikethrough" in op:
            rng.setPropertyValue("CharStrikeout", 1 if op["strikethrough"] else 0)
        if "highlight" in op:
            c = color(op["highlight"])
            rng.setPropertyValue("CharBackColor", c)
            if c == NO_COLOR:
                try:      # highlighting that came from a .docx is kept separately
                    rng.setPropertyValue("CharHighlight", NO_COLOR)
                except Exception:
                    pass
        if "color" in op:
            rng.setPropertyValue("CharColor", color(op["color"]))
        if "font" in op:
            rng.setPropertyValue("CharFontName", str(op["font"]))
        if "size" in op:
            rng.setPropertyValue("CharHeight", float(op["size"]))
        if "align" in op:
            a = str(op["align"]).lower()
            if a not in _ALIGN:
                raise EditError("Unknown alignment “%s”" % op["align"])
            rng.setPropertyValue("ParaAdjust", _ALIGN[a])
        if "style" in op:
            rng.setPropertyValue("ParaStyleName", self._style(op["style"]))

    def _style(self, name):
        styles = self.doc.getStyleFamilies().getByName("ParagraphStyles")
        if styles.hasByName(name):
            return name
        low = str(name).strip().lower()
        for n in styles.getElementNames():
            style = styles.getByName(n)
            if n.lower() == low or (getattr(style, "DisplayName", "") or "").lower() == low:
                return n
        raise EditError("There's no paragraph style “%s”" % name)

    def op_format(self, op):
        ranges, label, _ = self.targets(op)
        keys = ("bold", "italic", "underline", "strikethrough", "highlight", "color", "font", "size",
                "align", "style")
        if not any(k in op for k in keys):
            raise EditError("Format %s: no formatting given" % label)
        for k in ("highlight", "color"):
            if k in op:
                color(op[k])          # a bad color fails before anything changes
        if "underline" in op:
            _underline(op["underline"])
        if "style" in op:
            self._style(op["style"])
        for rng in ranges:
            self._set_char(rng, op)
        return "%s: %s%s" % (_cap(format_label(op)), label, _times(ranges))

    def _write(self, cursor, text, style=None):
        """Insert text with \\n as paragraph breaks at the cursor (which ends after it)."""
        body = cursor.getText()
        for i, line in enumerate(str(text).split("\n")):
            if i:
                body.insertControlCharacter(cursor, self.office.PARAGRAPH_BREAK, False)
                if style:
                    cursor.setPropertyValue("ParaStyleName", style)
            body.insertString(cursor, line, False)

    def op_replace(self, op):
        if "with" not in op:
            raise EditError("Replace: no \"with\" text")
        ranges, label, _ = self.targets(op)
        new = str(op["with"])
        for rng in reversed(ranges):
            cur = rng.getText().createTextCursorByRange(rng)
            cur.setString("")
            self._write(cur, new)
        return "Replaced %s%s with “%s”" % (label, _times(ranges), _short(new))

    # Writer splits a paragraph by moving the text before the break into a new paragraph, so
    # a break at the end of a paragraph leaves its object on the new, empty one after it.
    # These keep the [P12] numbers on the text Claude saw.

    def _elements(self):
        out, enum = [], self.doc.getText().createEnumeration()
        while enum.hasMoreElements():
            out.append(enum.nextElement())
        return out

    def _neighbour(self, el, step):
        els = self._elements()
        table = el.getName() if self._is_table(el) else None
        for i, e in enumerate(els):
            if e == el or (table and self._is_table(e) and e.getName() == table):
                j = i + step
                return els[j] if 0 <= j < len(els) else None
        return None

    def _break_at_end(self, el):
        """A new paragraph after el, by a break at its end; returns a cursor in it."""
        body = el.getText()
        cur = body.createTextCursorByRange(el.getEnd())
        body.insertControlCharacter(cur, self.office.PARAGRAPH_BREAK, False)
        prev = self._neighbour(el, -1)
        for n, e in list(self.paras.items()):
            if e == el and prev is not None:
                self.paras[n] = prev
        return cur

    def _new_paragraphs(self, cur, text, style, before_existing):
        """Write lines as paragraphs at cur. before_existing: cur is at the start of a paragraph
        that must stay as it is, so each line ends with a break instead of starting with one."""
        body = cur.getText()
        lines = str(text).split("\n")
        for i, line in enumerate(lines):
            if before_existing:
                body.insertString(cur, line, False)
                body.insertControlCharacter(cur, self.office.PARAGRAPH_BREAK, False)
                if style:
                    cur.goLeft(1, False)
                    cur.setPropertyValue("ParaStyleName", style)
                    cur.goRight(1, False)
            else:
                if i:
                    body.insertControlCharacter(cur, self.office.PARAGRAPH_BREAK, False)
                if style:
                    cur.setPropertyValue("ParaStyleName", style)
                body.insertString(cur, line, False)

    def op_delete(self, op):
        ranges, label, whole = self.targets(op)
        for rng in reversed(ranges):
            body = rng.getText()
            cur = body.createTextCursorByRange(rng)
            if whole and body == self.doc.getText():
                # take a paragraph break too, so no empty paragraph is left behind: the one
                # after it, or for the last paragraph the one before
                cur = body.createTextCursorByRange(rng.getStart())
                cur.gotoRange(rng.getEnd(), True)
                if not cur.goRight(1, True):
                    cur = body.createTextCursorByRange(rng.getStart())
                    if cur.goLeft(1, False):
                        cur.gotoRange(rng.getEnd(), True)
                    else:
                        cur = body.createTextCursorByRange(rng)
            cur.setString("")
        return "Deleted %s%s" % (label, _times(ranges))

    def op_insert(self, op):
        text = op.get("text")
        if text is None:
            raise EditError("Insert: no \"text\"")
        style = self._style(op["style"]) if op.get("style") else None
        before = str(op.get("position") or "after").lower() == "before"
        at = op.get("at")
        if at in ("end", "start") and not (op.get("find") or op.get("para") or op.get("selection")):
            els = self._elements()
            if at == "end":
                last = els[-1]
                if not self._is_table(last) and last.getString() == "":
                    cur = last.getText().createTextCursorByRange(last.getStart())
                elif self._is_table(last):
                    raise EditError("Insert: the document ends with a table")
                else:
                    cur = self._break_at_end(last)
                self._new_paragraphs(cur, text, style, False)
            else:
                if self._is_table(els[0]):
                    raise EditError("Insert: the document starts with a table")
                cur = els[0].getText().createTextCursorByRange(els[0].getStart())
                self._new_paragraphs(cur, text, style, True)
            return "Inserted \u201c%s\u201d at the %s of the document" % (_short(text), at)
        ranges, label, whole = self.targets(op)
        if whole:
            numbers = self._para_numbers(op)
            el = self.paras[numbers[0] if before else numbers[-1]]
            if self._is_table(el):
                self._beside_table(el, text, style, before)
            elif before:
                cur = el.getText().createTextCursorByRange(el.getStart())
                self._new_paragraphs(cur, text, style, True)
            else:
                # As if the user pressed Enter at its end and typed: the new paragraphs keep
                # its look (font, size, indents), or take the style after a heading.
                style = style or self._follow_style(el)
                self._new_paragraphs(self._break_at_end(el), text, style, False)
            return "Inserted \u201c%s\u201d %s %s" % (_short(text), "before" if before else "after", label)
        rng = ranges[0] if before else ranges[-1]
        cur = rng.getText().createTextCursorByRange(rng.getStart() if before else rng.getEnd())
        self._write(cur, text, style)
        return "Inserted \u201c%s\u201d %s %s" % (_short(text), "before" if before else "after", label)

    def _beside_table(self, table, text, style, before):
        """New paragraphs right before or after a table, written in the paragraph next to it."""
        near = self._neighbour(table, -1 if before else 1)
        if near is None or self._is_table(near):
            raise EditError("Insert: there's no paragraph %s this table to write next to"
                            % ("before" if before else "after"))
        body = near.getText()
        if before:
            # as if the user pressed Enter at the end of the paragraph above the table
            self._new_paragraphs(self._break_at_end(near), text, style or self._follow_style(near), False)
            return
        self._new_paragraphs(body.createTextCursorByRange(near.getStart()), text, style, True)
        if near.getString() == "" or style:
            return        # under the table's blank line, which stays below the new paragraphs
        # Text right under the table (often the next question, numbered or bold): the new
        # paragraphs mustn't take its look, so they get the plain look of the text above the table.
        cur = body.createTextCursorByRange(near.getStart())
        cur.goLeft(1, False)
        cur.gotoStartOfParagraph(True)
        for _ in range(str(text).count("\n")):
            cur.goLeft(1, True)
            cur.gotoStartOfParagraph(True)
        above = self._neighbour(table, -1)
        try:
            look = above.getPropertyValue("ParaStyleName") if above is not None and not self._is_table(above) \
                else "Standard"
            cur.setAllPropertiesToDefault()
            cur.setPropertyValue("ParaStyleName", look)
            cur.setPropertyValue("NumberingStyleName", "")
        except Exception:
            pass          # the text is in; only its look is off

    def op_fill(self, op):
        cells = op.get("cells")
        if cells is None and op.get("cell") and "text" in op:
            cells = {op["cell"]: op["text"]}
        if isinstance(cells, list):      # [{"cell": "A2", "text": "..."}]
            cells = {c.get("cell"): c.get("text", "") for c in cells if isinstance(c, dict)}
        if not isinstance(cells, dict) or not cells:
            raise EditError("Fill: no \"cells\" given")
        table = self._table_of(op)
        found = [(str(n).strip().upper(), self._cell(table, n), "" if t is None else str(t))
                 for n, t in cells.items()]      # a bad cell name fails before anything changes
        for _, cell, text in found:
            cur = self._cell_range(cell)
            if cur.getString():
                cur.setString("")
            self._write(cur, text)
        names = [n for n, _, _ in found]
        what = ", ".join(names[:6]) + (" +%d more" % (len(names) - 6) if len(names) > 6 else "")
        return "Filled cell%s %s of %s" % ("s" if len(names) > 1 else "", what, _para_label(op.get("para")))

    def _follow_style(self, el):
        """The style Writer gives the paragraph after a heading (Text Body), or None when that's
        its own style: setting a style resets a paragraph's own formatting, so it's left alone."""
        try:
            name = el.getPropertyValue("ParaStyleName")
            follow = self.doc.getStyleFamilies().getByName("ParagraphStyles").getByName(name).FollowStyle
            return follow if follow and follow != name else None
        except Exception:
            return None

    def op_comment(self, op):
        text = op.get("text")
        if not text:
            raise EditError("Comment: no \"text\"")
        ranges, label, _ = self.targets(op)
        for rng in ranges:
            note = self.doc.createInstance("com.sun.star.text.textfield.Annotation")
            note.Author = "Claude"
            note.Content = str(text)
            try:
                note.DateTimeValue = _now()
            except Exception:
                pass
            # Search results run backwards, which a comment can't span; rebuild it start to end.
            cur = rng.getText().createTextCursorByRange(rng.getStart())
            cur.gotoRange(rng.getEnd(), True)
            rng.getText().insertTextContent(cur, note, True)
        return "Commented on %s%s: “%s”" % (label, _times(ranges), _short(text))

    def op_style(self, op):
        if not op.get("style"):
            raise EditError("Style: no \"style\" name")
        return self.op_format({k: v for k, v in op.items() if k != "op"})


def _times(ranges):
    return " (%d×)" % len(ranges) if len(ranges) > 1 else ""


# ---------------------------------------------------------------- Calc

_HORI = {"left": "LEFT", "center": "CENTER", "centre": "CENTER", "right": "RIGHT", "justify": "BLOCK",
         "standard": "STANDARD", "default": "STANDARD"}


def _col_index(letters):
    n = 0
    for ch in str(letters).strip().upper():
        if not "A" <= ch <= "Z":
            raise EditError("Bad column “%s”" % letters)
        n = n * 26 + ord(ch) - 64
    if not n:
        raise EditError("Bad column “%s”" % letters)
    return n - 1


class _Calc:
    def __init__(self, doc):
        import claude_office
        self.doc = doc
        self.office = claude_office

    def _sheet(self, name):
        sheets = self.doc.getSheets()
        if not name:
            return self.doc.getCurrentController().getActiveSheet()
        if not sheets.hasByName(name):
            raise EditError("There's no sheet called “%s”" % name)
        return sheets.getByName(name)

    def _range(self, op, key="range"):
        """(sheet, cell range, label)."""
        ref = str(op.get(key) or "").strip()
        if not ref:
            raise EditError("%s: no \"range\"" % str(op.get("op")).capitalize())
        if ref.lower() == "selection":
            a = self.office._calc_selection_address(self.doc)
            sheet = self.doc.getSheets().getByIndex(a.Sheet)
            rng = sheet.getCellRangeByPosition(a.StartColumn, a.StartRow, a.EndColumn, a.EndRow)
            return sheet, rng, "the selection"
        m = self.office._CALC_REF.match(ref.replace(" ", "") if "'" not in ref else ref)
        if not m:
            raise EditError("Bad range “%s”" % ref)
        name = m.group("sheet") or op.get("sheet")
        if name and m.group("sheet"):
            name = name[1:-1].replace("''", "'") if name.startswith("'") else name
        sheet = self._sheet(name)
        try:
            rng = sheet.getCellRangeByName(m.group("range").replace("$", ""))
        except Exception:
            raise EditError("Bad range “%s”" % ref) from None
        return sheet, rng, ref.replace("$", "")

    def op_set(self, op):
        sheet, rng, label = self._range(op)
        values = op.get("values", op.get("value"))
        if values is None:
            raise EditError("Set %s: no \"values\"" % label)
        if not isinstance(values, list):
            values = [[values]]
        elif values and not any(isinstance(v, list) for v in values):
            values = [values]
        a = rng.getRangeAddress()
        count = 0
        for r, row in enumerate(values):
            if not isinstance(row, list):
                row = [row]
            for c, v in enumerate(row):
                cell = sheet.getCellByPosition(a.StartColumn + c, a.StartRow + r)
                if v is None:
                    cell.setString("")
                elif isinstance(v, bool):
                    cell.setFormula("=TRUE()" if v else "=FALSE()")
                elif isinstance(v, (int, float)):
                    cell.setValue(float(v))
                else:
                    self.office.set_cell(cell, str(v))
                count += 1
        width = max((len(r) if isinstance(r, list) else 1) for r in values) if values else 1
        end = self.office._ref(a.StartColumn, a.StartRow, a.StartColumn + width - 1, a.StartRow + len(values) - 1)
        where = (label.split("!")[0] + "!" if "!" in label else "") + end
        return "Wrote %d cell%s in %s" % (count, "" if count == 1 else "s", where)

    def op_format(self, op):
        sheet, rng, label = self._range(op)
        keys = ("bold", "italic", "underline", "color", "background", "font", "size", "number_format",
                "align", "wrap", "border", "strikethrough")
        if not any(k in op for k in keys):
            raise EditError("Format %s: no formatting given" % label)
        if "bold" in op:
            rng.setPropertyValue("CharWeight", 150.0 if op["bold"] else 100.0)
        if "italic" in op:
            rng.setPropertyValue("CharPosture", _enum("com.sun.star.awt.FontSlant",
                                                      "ITALIC" if op["italic"] else "NONE"))
        if "underline" in op:
            rng.setPropertyValue("CharUnderline", _underline(op["underline"]))
        if "strikethrough" in op:
            rng.setPropertyValue("CharStrikeout", 1 if op["strikethrough"] else 0)
        if "color" in op:
            rng.setPropertyValue("CharColor", color(op["color"]))
        if "background" in op or "highlight" in op:
            c = color(op.get("background", op.get("highlight")))
            rng.setPropertyValue("CellBackColor", c)
            rng.setPropertyValue("IsCellBackgroundTransparent", c == NO_COLOR)
        if "font" in op:
            rng.setPropertyValue("CharFontName", str(op["font"]))
        if "size" in op:
            rng.setPropertyValue("CharHeight", float(op["size"]))
        if "number_format" in op:
            rng.setPropertyValue("NumberFormat", self._number_format(str(op["number_format"])))
        if "align" in op:
            a = str(op["align"]).lower()
            if a not in _HORI:
                raise EditError("Unknown alignment “%s”" % op["align"])
            rng.setPropertyValue("HoriJustify", _enum("com.sun.star.table.CellHoriJustify", _HORI[a]))
        if "wrap" in op:
            rng.setPropertyValue("IsTextWrapped", bool(op["wrap"]))
        if "border" in op:
            import uno
            line = uno.createUnoStruct("com.sun.star.table.BorderLine2")
            if op["border"]:
                line.OuterLineWidth, line.LineWidth = 26, 26
                line.Color = color(op["border"]) if isinstance(op["border"], str) else 0
            border = rng.getPropertyValue("TableBorder2")
            for side in ("TopLine", "BottomLine", "LeftLine", "RightLine", "HorizontalLine", "VerticalLine"):
                setattr(border, side, line)
                setattr(border, "Is" + side + "Valid", True)
            rng.setPropertyValue("TableBorder2", border)
        return "%s: %s" % (_cap(format_label(op)), label)

    def _number_format(self, code):
        import uno
        formats = self.doc.getNumberFormats()
        locale = uno.createUnoStruct("com.sun.star.lang.Locale")
        locale.Language, locale.Country = "en", "US"
        key = formats.queryKey(code, locale, False)
        if key == -1:
            try:
                key = formats.addNew(code, locale)
            except Exception:
                raise EditError("Bad number format “%s”" % code) from None
        return key

    def op_clear(self, op):
        sheet, rng, label = self._range(op)
        flags = 1 | 2 | 4 | 16      # VALUE, DATETIME, STRING, FORMULA
        if op.get("formatting"):
            flags |= 32 | 64        # HARDATTR, STYLES
        rng.clearContents(flags)
        return "Cleared %s%s" % (label, " and its formatting" if op.get("formatting") else "")

    def op_comment(self, op):
        text = op.get("text")
        if not text:
            raise EditError("Comment: no \"text\"")
        sheet, rng, label = self._range(op)
        a = rng.getRangeAddress()
        import uno
        cell = uno.createUnoStruct("com.sun.star.table.CellAddress")
        cell.Sheet, cell.Column, cell.Row = a.Sheet, a.StartColumn, a.StartRow
        sheet.getAnnotations().insertNew(cell, str(text))
        return "Commented on %s: “%s”" % (self.office._ref(a.StartColumn, a.StartRow,
                                                                   a.StartColumn, a.StartRow), _short(text))

    def _count(self, op):
        try:
            n = int(op.get("count") or 1)
        except (TypeError, ValueError):
            raise EditError("Bad count %r" % op.get("count")) from None
        if not 1 <= n <= 10000:
            raise EditError("Bad count %r" % op.get("count"))
        return n

    def _row(self, op):
        try:
            row = int(op.get("at"))
        except (TypeError, ValueError):
            raise EditError("%s: \"at\" should be a row number" % op.get("op")) from None
        if row < 1:
            raise EditError("Bad row %d" % row)
        return row - 1

    def op_insert_rows(self, op):
        sheet, at, n = self._sheet(op.get("sheet")), self._row(op), self._count(op)
        sheet.getRows().insertByIndex(at, n)
        return "Inserted %d row%s at row %d" % (n, "" if n == 1 else "s", at + 1)

    def op_delete_rows(self, op):
        sheet, at, n = self._sheet(op.get("sheet")), self._row(op), self._count(op)
        sheet.getRows().removeByIndex(at, n)
        return "Deleted row%s %d%s" % ("" if n == 1 else "s", at + 1, "–%d" % (at + n) if n > 1 else "")

    def _column(self, op):
        at = op.get("at")
        if isinstance(at, int) and not isinstance(at, bool):
            return at - 1
        return _col_index(at or "")

    def op_insert_columns(self, op):
        sheet, at, n = self._sheet(op.get("sheet")), self._column(op), self._count(op)
        sheet.getColumns().insertByIndex(at, n)
        return "Inserted %d column%s at %s" % (n, "" if n == 1 else "s", self.office._col_letters(at))

    def op_delete_columns(self, op):
        sheet, at, n = self._sheet(op.get("sheet")), self._column(op), self._count(op)
        sheet.getColumns().removeByIndex(at, n)
        last = self.office._col_letters(at + n - 1)
        return "Deleted column%s %s%s" % ("" if n == 1 else "s", self.office._col_letters(at),
                                          "–" + last if n > 1 else "")
