"""Dialogs, built in code so the extension needs no .xdl files.

Controls keep the system colours so the dialogs follow LibreOffice's light or
dark theme; only the header band uses the accent colour.
"""

import time

import uno
import unohelper
from com.sun.star.awt import XActionListener
from com.sun.star.awt.MessageBoxButtons import BUTTONS_OK
from com.sun.star.awt.MessageBoxType import ERRORBOX, INFOBOX

import claude_api

OK, CANCEL, REPLACE, INSERT, REFINE = 1, 0, 2, 3, 4
_QUICK = 100   # quick-action buttons end the Ask dialog with _QUICK + index

ACCENT = 0xB85C38          # header band; white text on it passes WCAG AA for large text
MUTED = 0x8A8A8A           # hint text, readable on light and dark backgrounds
EXTENSION_ID = "org.willykil.claude"


class _EndDialog(unohelper.Base, XActionListener):
    def __init__(self, dialog, result):
        self.dialog, self.result = dialog, result

    def actionPerformed(self, event):
        self.dialog.endDialog(self.result)

    def disposing(self, source):
        pass


class _Call(unohelper.Base, XActionListener):
    def __init__(self, fn):
        self.fn = fn

    def actionPerformed(self, event):
        self.fn()

    def disposing(self, source):
        pass


def _icon_url(ctx, size=26):
    try:
        pip = ctx.getValueByName("/singletons/com.sun.star.deployment.PackageInformationProvider")
        base = pip.getPackageLocation(EXTENSION_ID)
        return "%s/icons/sparkle_white_%d.png" % (base, size) if base else ""
    except Exception:
        return ""


class _Builder:
    def __init__(self, ctx, title, width, height):
        self.ctx = ctx
        self.smgr = ctx.ServiceManager
        self.width = width
        self.model = self.smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
        self.model.Title = title
        self.model.Width, self.model.Height = width, height
        self._n = 0

    def add(self, kind, x, y, w, h, name=None, **props):
        self._n += 1
        name = name or "c%d" % self._n
        m = self.model.createInstance("com.sun.star.awt.UnoControl%sModel" % kind)
        m.PositionX, m.PositionY, m.Width, m.Height = x, y, w, h
        for k, v in props.items():
            setattr(m, k, v)
        self.model.insertByName(name, m)
        return name

    def header(self, title, subtitle=""):
        """Accent band across the top with the sparkle icon. Returns the y below it.

        Built from side-by-side pieces: overlapping controls don't paint in a reliable order.
        """
        middle = uno.Enum("com.sun.star.style.VerticalAlignment", "MIDDLE")
        bottom = uno.Enum("com.sun.star.style.VerticalAlignment", "BOTTOM")
        top = uno.Enum("com.sun.star.style.VerticalAlignment", "TOP")
        url = _icon_url(self.ctx)
        if url:
            self.add("ImageControl", 0, 0, 30, 30, "header_icon", ImageURL=url, Border=0,
                     BackgroundColor=ACCENT, ScaleMode=0)
        else:
            self.add("FixedText", 0, 0, 30, 30, "header_icon", Label="", BackgroundColor=ACCENT)
        white = dict(TextColor=0xFFFFFF, BackgroundColor=ACCENT)
        if subtitle:
            self.add("FixedText", 30, 0, self.width - 30, 16, "header_title", Label=title, FontHeight=12,
                     FontWeight=150, VerticalAlign=bottom, **white)
            self.add("FixedText", 30, 16, self.width - 30, 14, "header_sub", Label=subtitle,
                     VerticalAlign=top, TextColor=0xFBE3D8, BackgroundColor=ACCENT)
        else:
            self.add("FixedText", 30, 0, self.width - 30, 30, "header_title", Label=title, FontHeight=12,
                     FontWeight=150, VerticalAlign=middle, **white)
        return 36

    def label(self, x, y, w, text, name=None, muted=False, bold=False, h=10, **props):
        if muted:
            props["TextColor"] = MUTED
        if bold:
            props["FontWeight"] = 150
        return self.add("FixedText", x, y, w, h, name, Label=text, **props)

    def button(self, x, y, w, label, push_type=0, name=None, default=False, h=15, **props):
        # push_type: 0 standard, 1 OK, 2 Cancel
        return self.add("Button", x, y, w, h, name, Label=label, PushButtonType=push_type,
                        DefaultButton=default, **props)

    def create(self):
        dlg = self.smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", self.ctx)
        dlg.setModel(self.model)
        toolkit = self.smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
        dlg.createPeer(toolkit, _parent_window(self.ctx))
        return dlg


def _parent_window(ctx):
    """The current document window, so dialogs open centered over it."""
    try:
        desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        return desktop.getCurrentFrame().getContainerWindow()
    except Exception:
        return None


def connection_label(settings):
    if settings.get("backend") == claude_api.API:
        return "Using your Anthropic API key (billed per use)"
    return "Using your Claude subscription (via Claude Code)"


def message(ctx, frame, text, title="Claude", error=False):
    toolkit = ctx.ServiceManager.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    parent = frame.getContainerWindow() if frame else None
    box = toolkit.createMessageBox(parent, ERRORBOX if error else INFOBOX, BUTTONS_OK, title, text)
    box.execute()
    box.dispose()


# ---------------------------------------------------------------- Ask

def ask_prompt(ctx, context, quick_actions, settings):
    """Returns the instruction to send (typed, or a quick action's prompt), or None if cancelled.

    quick_actions: [(label, prompt, needs_selection)]
    """
    W = 320
    per_row = 4
    rows = (len(quick_actions) + per_row - 1) // per_row
    b = _Builder(ctx, "Ask Claude", W, 0)
    y = b.header("Ask Claude", context.label)
    b.label(10, y, W - 20, "Quick actions", bold=True)
    y += 13
    bw = (W - 20 - (per_row - 1) * 4) / per_row
    for i, (label, _prompt, needs_sel) in enumerate(quick_actions):
        r, c = divmod(i, per_row)
        enabled = context.has_selection or not needs_sel
        b.button(int(10 + c * (bw + 4)), y + r * 19, int(bw), label, name="quick%d" % i,
                 Enabled=enabled, HelpText="" if enabled else "Select some text first")
    y += rows * 19 + 6
    b.label(10, y, W - 20, "Or tell Claude what you want", bold=True)
    y += 13
    b.add("Edit", 10, y, W - 20, 62, "prompt", MultiLine=True, VScroll=True, AutoVScroll=True, FontHeight=11)
    y += 66
    b.label(10, y, W - 20, connection_label(settings), muted=True)
    y += 14
    b.button(W - 122, y, 54, "Ask", push_type=1, default=True, name="ask")
    b.button(W - 64, y, 54, "Cancel", push_type=2)
    b.model.Height = y + 22
    dlg = b.create()
    for i in range(len(quick_actions)):
        dlg.getControl("quick%d" % i).addActionListener(_EndDialog(dlg, _QUICK + i))
    dlg.getControl("prompt").setFocus()
    try:
        result = dlg.execute()
        if result >= _QUICK:
            return quick_actions[result - _QUICK][1]
        if result != OK:
            return None
        return dlg.getControl("prompt").getText().strip() or None
    finally:
        dlg.dispose()


def ask_refine(ctx):
    """Asks how to change the last reply. Returns the instruction or None."""
    W = 300
    b = _Builder(ctx, "Refine reply", W, 0)
    y = b.header("Refine the reply", "Claude will rewrite its answer")
    b.label(10, y, W - 20, "What should change?", bold=True)
    y += 13
    b.add("Edit", 10, y, W - 20, 50, "prompt", MultiLine=True, VScroll=True, FontHeight=11)
    y += 56
    b.label(10, y, W - 20, "e.g. \"shorter\", \"more formal\", \"in English\", \"as a bulleted list\"", muted=True)
    y += 14
    b.button(W - 122, y, 54, "Refine", push_type=1, default=True)
    b.button(W - 64, y, 54, "Cancel", push_type=2)
    b.model.Height = y + 22
    dlg = b.create()
    dlg.getControl("prompt").setFocus()
    try:
        if dlg.execute() != OK:
            return None
        return dlg.getControl("prompt").getText().strip() or None
    finally:
        dlg.dispose()


# ---------------------------------------------------------------- waiting

class Busy:
    """A small modeless 'Claude is thinking' window with a Cancel button.

    The document window is disabled meanwhile, so the selection the reply
    will go into can't change underneath it.
    """

    def __init__(self, ctx, frame, settings):
        self.cancelled = False
        self._frame_window = frame.getContainerWindow() if frame else None
        W = 250
        b = _Builder(ctx, "Claude", W, 0)
        y = b.header("Claude is thinking", connection_label(settings))
        b.label(10, y + 2, W - 80, "Working...", name="status", FontHeight=11, h=14)
        b.button(W - 64, y, 54, "Cancel", name="cancel")
        b.model.Height = y + 22
        self.dlg = b.create()
        self.dlg.getControl("cancel").addActionListener(_Call(self._cancel))
        self._status = self.dlg.getControl("status").getModel()
        self._start = time.time()
        if self._frame_window is not None:
            self._frame_window.setEnable(False)
        self.dlg.setVisible(True)

    def _cancel(self):
        self.cancelled = True
        self._status.Label = "Cancelling..."

    def tick(self):
        if not self.cancelled:
            secs = int(time.time() - self._start)
            self._status.Label = "Working%s  %d s" % (("." * (secs % 3 + 1)).ljust(3), secs)

    def close(self):
        if self._frame_window is not None:
            self._frame_window.setEnable(True)
        self.dlg.setVisible(False)
        self.dlg.dispose()


# ---------------------------------------------------------------- reply

def show_result(ctx, text, kind, truncated=False):
    """Shows Claude's reply. Returns (REPLACE | INSERT | REFINE | CANCEL, edited text)."""
    W, H = 360, 270
    b = _Builder(ctx, "Claude", W, H)
    y = b.header("Claude's reply", "Edit it if you like, then choose where it goes")
    if truncated:
        b.label(10, y, W - 20, "The reply was cut off at the length limit (raise it in Settings).",
                TextColor=0xC0392B)
        y += 12
    b.add("Edit", 10, y, W - 20, H - y - 30, "reply", MultiLine=True, VScroll=True, HScroll=False,
          Text=text, FontHeight=11)
    if kind == "calc":
        labels = ("Write at selection", "Write below selection")
    else:
        labels = ("Replace selection", "Insert after")
    by = H - 22
    # In Calc, writing below never overwrites cells, so make that the default.
    b.button(10, by, 88, labels[0], name="replace", default=kind != "calc")
    b.button(102, by, 88, labels[1], name="insert", default=kind == "calc")
    b.button(194, by, 60, "Refine...", name="refine")
    b.button(W - 64, by, 54, "Close", push_type=2)
    dlg = b.create()
    dlg.getControl("replace").addActionListener(_EndDialog(dlg, REPLACE))
    dlg.getControl("insert").addActionListener(_EndDialog(dlg, INSERT))
    dlg.getControl("refine").addActionListener(_EndDialog(dlg, REFINE))
    try:
        result = dlg.execute()
        return result, dlg.getControl("reply").getText()
    finally:
        dlg.dispose()


# ---------------------------------------------------------------- settings

BACKENDS = ((claude_api.CLAUDE_CODE, "Claude Code - uses your Claude subscription"),
            (claude_api.API, "Anthropic API key - billed separately, per use"))


def edit_settings(ctx, settings):
    """Returns updated settings, or None if cancelled."""
    W = 300
    b = _Builder(ctx, "Claude Settings", W, 0)
    y = b.header("Claude Settings", "How the extension talks to Claude")
    keys = [k for k, _ in BACKENDS]
    backend = settings.get("backend", claude_api.CLAUDE_CODE)
    L, F = 10, 90                      # label column, field column
    FW = W - F - 10

    b.label(L, y, W - 20, "Connection", bold=True)
    y += 13
    b.label(L, y + 2, 78, "Connect with")
    b.add("ListBox", F, y, FW, 13, "backend", Dropdown=True, StringItemList=tuple(l for _, l in BACKENDS),
          SelectedItems=(keys.index(backend) if backend in keys else 0,))
    y += 16
    b.label(F, y, FW, "Claude Code needs to be installed and signed in once.", muted=True)
    y += 13
    b.label(L, y + 2, 78, "Claude Code path")
    b.add("Edit", F, y, FW, 13, "claude_path", Text=settings.get("claude_path", ""))
    y += 16
    b.label(F, y, FW, "Optional - leave blank to find it automatically.", muted=True)
    y += 13
    b.label(L, y + 2, 78, "API key")
    b.add("Edit", F, y, FW, 13, "api_key", EchoChar=ord("*"), Text=settings.get("api_key", ""))
    y += 16
    b.label(F, y, FW, "Only for the API key connection (console.anthropic.com).", muted=True)
    y += 18

    b.label(L, y, W - 20, "Replies", bold=True)
    y += 13
    b.label(L, y + 2, 78, "Model")
    b.add("ComboBox", F, y, FW, 13, "model", Dropdown=True, StringItemList=tuple(claude_api.MODELS),
          Text=settings.get("model", ""))
    y += 16
    b.label(F, y, FW, "Leave blank for the default model.", muted=True)
    y += 13
    effort = settings.get("effort", "medium")
    b.label(L, y + 2, 78, "Effort")
    b.add("ListBox", F, y, 70, 13, "effort", Dropdown=True, StringItemList=tuple(claude_api.EFFORTS),
          SelectedItems=(claude_api.EFFORTS.index(effort) if effort in claude_api.EFFORTS else 1,))
    b.label(F + 76, y + 2, FW - 76, "low = faster, high = more careful", muted=True)
    y += 17
    b.label(L, y + 2, 78, "Max reply length")
    b.add("NumericField", F, y, 70, 13, "max_tokens", DecimalAccuracy=0, ValueMin=256, ValueMax=128000,
          Value=float(settings.get("max_tokens", 16000)), StrictFormat=True, Spin=True)
    b.label(F + 76, y + 2, FW - 76, "tokens (API key connection only)", muted=True)
    y += 20
    b.label(L, y, W - 20, "Always tell Claude (optional)", bold=True)
    y += 13
    b.add("Edit", L, y, W - 20, 40, "extra", MultiLine=True, VScroll=True,
          Text=settings.get("extra_instructions", ""))
    y += 44
    b.label(L, y, W - 20, "e.g. \"Write in Canadian French.\" or \"Keep answers short.\"", muted=True)
    y += 14
    b.button(W - 122, y, 54, "Save", push_type=1, default=True)
    b.button(W - 64, y, 54, "Cancel", push_type=2)
    b.model.Height = y + 22
    dlg = b.create()
    try:
        if dlg.execute() != OK:
            return None
        updated = dict(settings)
        pos = dlg.getControl("backend").getSelectedItemPos()
        updated["backend"] = keys[pos] if 0 <= pos < len(keys) else claude_api.CLAUDE_CODE
        updated["claude_path"] = dlg.getControl("claude_path").getText().strip()
        updated["api_key"] = dlg.getControl("api_key").getText().strip()
        updated["model"] = dlg.getControl("model").getText().strip()
        updated["effort"] = dlg.getControl("effort").getSelectedItem() or "medium"
        updated["max_tokens"] = int(dlg.getControl("max_tokens").getValue())
        updated["extra_instructions"] = dlg.getControl("extra").getText()
        return updated
    finally:
        dlg.dispose()
