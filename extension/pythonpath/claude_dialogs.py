"""Dialogs, built in code so the extension needs no .xdl files."""

import unohelper
from com.sun.star.awt import XActionListener
from com.sun.star.awt.MessageBoxButtons import BUTTONS_OK
from com.sun.star.awt.MessageBoxType import ERRORBOX, INFOBOX

import claude_api

OK, CANCEL, REPLACE, INSERT = 1, 0, 2, 3


class _EndDialog(unohelper.Base, XActionListener):
    def __init__(self, dialog, result):
        self.dialog, self.result = dialog, result

    def actionPerformed(self, event):
        self.dialog.endDialog(self.result)

    def disposing(self, source):
        pass


class _Builder:
    def __init__(self, ctx, title, width, height):
        self.ctx = ctx
        self.smgr = ctx.ServiceManager
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

    def button(self, x, y, w, label, push_type=0, name=None, default=False):
        # push_type: 0 standard, 1 OK, 2 Cancel
        return self.add("Button", x, y, w, 14, name, Label=label, PushButtonType=push_type, DefaultButton=default)

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


def message(ctx, frame, text, title="Claude", error=False):
    toolkit = ctx.ServiceManager.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    parent = frame.getContainerWindow() if frame else None
    box = toolkit.createMessageBox(parent, ERRORBOX if error else INFOBOX, BUTTONS_OK, title, text)
    box.execute()
    box.dispose()


def ask_prompt(ctx, context_label, initial=""):
    """Returns the user's instruction, or None if cancelled."""
    b = _Builder(ctx, "Ask Claude", 280, 150)
    b.add("FixedText", 8, 6, 264, 10, Label=context_label)
    b.add("FixedText", 8, 20, 264, 10, Label="What should Claude do?")
    b.add("Edit", 8, 32, 264, 90, "prompt", MultiLine=True, VScroll=True, AutoVScroll=True, Text=initial)
    b.button(170, 130, 50, "Ask", push_type=1, default=True)
    b.button(222, 130, 50, "Cancel", push_type=2)
    dlg = b.create()
    dlg.getControl("prompt").setFocus()
    try:
        if dlg.execute() != OK:
            return None
        text = dlg.getControl("prompt").getText().strip()
        return text or None
    finally:
        dlg.dispose()


def show_result(ctx, text, kind, truncated=False):
    """Shows Claude's reply. Returns REPLACE, INSERT or CANCEL."""
    b = _Builder(ctx, "Claude", 320, 220)
    note = "Reply was cut off at the max-tokens limit (raise it in Settings). " if truncated else ""
    b.add("FixedText", 8, 6, 304, 10, Label=note + "You can edit the reply before inserting it.")
    b.add("Edit", 8, 18, 304, 176, "reply", MultiLine=True, VScroll=True, HScroll=False, Text=text)
    if kind == "calc":
        labels = ("Write at selection", "Write below selection")
    else:
        labels = ("Replace selection", "Insert after")
    # In Calc, writing below never overwrites cells, so make that the default.
    b.button(8, 200, 90, labels[0], name="replace", default=kind != "calc")
    b.button(102, 200, 90, labels[1], name="insert", default=kind == "calc")
    b.button(262, 200, 50, "Close", push_type=2)
    dlg = b.create()
    dlg.getControl("replace").addActionListener(_EndDialog(dlg, REPLACE))
    dlg.getControl("insert").addActionListener(_EndDialog(dlg, INSERT))
    try:
        result = dlg.execute()
        return result, dlg.getControl("reply").getText()
    finally:
        dlg.dispose()


def edit_settings(ctx, settings):
    """Returns updated settings, or None if cancelled."""
    b = _Builder(ctx, "Claude Settings", 280, 196)
    b.add("FixedText", 8, 8, 70, 10, Label="API key")
    b.add("Edit", 80, 6, 192, 12, "api_key", EchoChar=ord("*"), Text=settings.get("api_key", ""))
    b.add("FixedText", 80, 20, 192, 10, Label="From console.anthropic.com, or blank to use ANTHROPIC_API_KEY")
    b.add("FixedText", 8, 36, 70, 10, Label="Model")
    b.add("ComboBox", 80, 34, 192, 12, "model", Dropdown=True, StringItemList=tuple(claude_api.MODELS),
          Text=settings.get("model", ""))
    b.add("FixedText", 8, 52, 70, 10, Label="Effort")
    effort = settings.get("effort", "medium")
    b.add("ListBox", 80, 50, 80, 12, "effort", Dropdown=True, StringItemList=tuple(claude_api.EFFORTS),
          SelectedItems=(claude_api.EFFORTS.index(effort) if effort in claude_api.EFFORTS else 1,))
    b.add("FixedText", 8, 68, 70, 10, Label="Max reply tokens")
    b.add("NumericField", 80, 66, 80, 12, "max_tokens", DecimalAccuracy=0, ValueMin=256, ValueMax=128000,
          Value=float(settings.get("max_tokens", 16000)), StrictFormat=True, Spin=True)
    b.add("FixedText", 8, 84, 264, 10, Label="Extra instructions for every request (optional)")
    b.add("Edit", 8, 96, 264, 72, "extra", MultiLine=True, VScroll=True, Text=settings.get("extra_instructions", ""))
    b.button(170, 176, 50, "Save", push_type=1, default=True)
    b.button(222, 176, 50, "Cancel", push_type=2)
    dlg = b.create()
    try:
        if dlg.execute() != OK:
            return None
        updated = dict(settings)
        updated["api_key"] = dlg.getControl("api_key").getText().strip()
        updated["model"] = dlg.getControl("model").getText().strip() or claude_api.DEFAULT_SETTINGS["model"]
        updated["effort"] = dlg.getControl("effort").getSelectedItem() or "medium"
        updated["max_tokens"] = int(dlg.getControl("max_tokens").getValue())
        updated["extra_instructions"] = dlg.getControl("extra").getText()
        return updated
    finally:
        dlg.dispose()
