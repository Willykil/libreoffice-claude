"""The Claude tab in LibreOffice's sidebar.

LibreOffice builds the panel through PanelFactory (registered in Factories.xcu, placed by
Sidebar.xcu). The panel always starts with a version made of LibreOffice's own controls
(NativePanel), so it works everywhere and right away. On Windows it then tries to show the full
web panel inside the same space: an Edge "app" window, reparented into a native child window of
the sidebar (claude_embed.py). If that works, it replaces the native controls; if it doesn't,
the native version simply stays.
"""

import sys
import threading
import time
import traceback

import uno
import unohelper
from com.sun.star.awt import XActionListener, XItemListener, XWindowListener
from com.sun.star.awt.MessageBoxButtons import BUTTONS_YES_NO
from com.sun.star.awt.MessageBoxType import QUERYBOX
from com.sun.star.datatransfer import XTransferable
from com.sun.star.lang import XEventListener
from com.sun.star.ui import XSidebarPanel, XToolPanel, XUIElement, XUIElementFactory
from com.sun.star.view import XSelectionChangeListener

import claude_actions
import claude_api
import claude_office
import claude_panel

TOOLPANEL = 7                       # com.sun.star.ui.UIElementType.TOOLPANEL
POSSIZE = 15                        # com.sun.star.awt.PosSize.POSSIZE
DECK_COMMAND = ".uno:SidebarDeck.ClaudeDeck"

_panels = []                        # open SidebarPanels, so menu actions reach the right one
_panels_lock = threading.Lock()


# ---------------------------------------------------------------- factory / element

class PanelFactory(unohelper.Base, XUIElementFactory):
    def __init__(self, ctx):
        self.ctx = ctx

    def createUIElement(self, url, args):
        props = {a.Name: a.Value for a in args}
        try:
            return ClaudeElement(self.ctx, url, props.get("Frame"), props.get("ParentWindow"))
        except Exception:
            traceback.print_exc()
            raise


class ClaudeElement(unohelper.Base, XUIElement):
    def __init__(self, ctx, url, frame, parent):
        self.ResourceURL = url
        self.Frame = frame
        self.Type = TOOLPANEL
        self._panel = SidebarPanel(ctx, frame, parent)

    def getRealInterface(self):
        return self._panel


# ---------------------------------------------------------------- the panel

class SidebarPanel(unohelper.Base, XToolPanel, XSidebarPanel, XWindowListener, XEventListener):
    def __init__(self, ctx, frame, parent):
        self.ctx = ctx
        self.frame = frame
        self.parent = parent
        self.server = claude_panel.get_server(ctx)
        self.native = NativePanel(ctx, self, parent)
        self.embed = None
        parent.addWindowListener(self)
        parent.addEventListener(self)
        with _panels_lock:
            _panels.append(self)
        if sys.platform == "win32" and not claude_api.load_settings(self.server.settings_path).get("native_sidebar"):
            self._start_embed()
        self.layout()

    @property
    def doc(self):
        try:
            return self.frame.getController().getModel()
        except Exception:
            return None

    @property
    def embedded(self):
        return self.embed is not None and self.embed.ready

    def _start_embed(self):
        try:
            import claude_embed
            self.embed = claude_embed.EdgeEmbed(self.ctx, self.parent, self.server, on_ready=self._embed_ready,
                                                on_fail=self._embed_failed)
            self.embed.start()
        except Exception:
            traceback.print_exc()
            self.embed = None

    def _embed_ready(self):
        self.native.set_visible(False)
        self.layout()

    def _embed_failed(self, reason):
        self.embed = None
        self.native.set_visible(True)
        self.native.note("The full panel couldn't open inside the sidebar (%s). This version has the "
                         "essentials; Open full panel shows the rest in its own window." % reason)

    # XToolPanel
    @property
    def Window(self):
        return self.parent

    def createAccessible(self, parent_accessible):
        return self.parent.getAccessibleContext() if hasattr(self.parent, "getAccessibleContext") else None

    # XSidebarPanel
    def getHeightForWidth(self, width):
        return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 420, -1, 700)

    def getMinimalWidth(self):
        return 280

    # XWindowListener
    def windowResized(self, event):
        self.layout()

    def windowMoved(self, event):
        pass

    def windowShown(self, event):
        self.layout()

    def windowHidden(self, event):
        pass

    def layout(self):
        size = self.parent.getPosSize()
        if self.embedded:
            self.embed.resize(size.Width, size.Height)
        else:
            self.native.layout(size.Width, size.Height)

    # XEventListener: the sidebar is closing this panel
    def disposing(self, source):
        with _panels_lock:
            if self in _panels:
                _panels.remove(self)
        if self.embed is not None:
            self.embed.close()
        self.native.dispose()

    # menu / toolbar actions
    def run_action(self, action):
        if self.embedded:
            self.server.pending = action or "focus"
        else:
            self.native.run_action(action)


def panel_for(frame):
    # Python wrappers of the same frame differ, but == compares the UNO objects themselves.
    with _panels_lock:
        return next((p for p in _panels if p.frame == frame), None)


def show(ctx, action=None):
    """Open the Claude tab in the current window's sidebar, then run `action` (improve, summarize,
    explain, settings) in it. Falls back to the separate window if the sidebar isn't available."""
    smgr = ctx.ServiceManager
    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    frame = desktop.getCurrentFrame()
    if frame is None:
        return claude_panel.open_panel(ctx, action)
    helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    helper.executeDispatch(frame, DECK_COMMAND, "", 0, ())
    panel = panel_for(frame)
    if panel is not None:
        if action:
            panel.run_action(action)
        return panel

    def wait_for_panel():
        # The sidebar builds the panel a moment after the deck command.
        for _ in range(40):
            time.sleep(0.1)
            p = panel_for(frame)
            if p is not None:
                if action:
                    p.run_action(action)
                return
        claude_panel.open_panel(ctx, action)       # no sidebar (e.g. switched off): its own window
    threading.Thread(target=wait_for_panel, daemon=True).start()
    return None


# ---------------------------------------------------------------- the version made of LibreOffice controls

class _Listener(unohelper.Base, XActionListener, XItemListener):
    def __init__(self, fn):
        self.fn = fn

    def actionPerformed(self, event):
        self._run()

    def itemStateChanged(self, event):
        self._run()

    def _run(self):
        try:
            self.fn()
        except Exception:
            traceback.print_exc()

    def disposing(self, source):
        pass


class _SelectionListener(unohelper.Base, XSelectionChangeListener):
    def __init__(self, fn):
        self.fn = fn

    def selectionChanged(self, event):
        try:
            self.fn()
        except Exception:
            pass

    def disposing(self, source):
        pass


class _Text(unohelper.Base, XTransferable):
    """Plain text for the clipboard."""

    def __init__(self, text):
        self.text = text
        flavor = uno.createUnoStruct("com.sun.star.datatransfer.DataFlavor")
        flavor.MimeType = "text/plain;charset=utf-16"
        flavor.HumanPresentableName = "Unicode-Text"
        flavor.DataType = uno.getTypeByName("string")
        self.flavor = flavor

    def getTransferData(self, flavor):
        return self.text

    def getTransferDataFlavors(self):
        return (self.flavor,)

    def isDataFlavorSupported(self, flavor):
        return flavor.MimeType.startswith("text/plain")


MODEL_CHOICES = [("", "Default model")] + [(m, claude_api.MODEL_LABELS.get(m, m)) for m in claude_api.MODELS]
EFFORT_CHOICES = [(e, claude_api.EFFORT_LABELS[e] + " effort") for e in claude_api.EFFORTS]


class NativePanel:
    PAD = 10
    ROW = 26

    def __init__(self, ctx, owner, parent):
        self.ctx = ctx
        self.owner = owner
        self.smgr = ctx.ServiceManager
        self.server = owner.server
        self.busy = False
        self.reply = None             # last reply from Claude
        self.history = []
        self.visible = True
        doc = owner.doc
        self.kind = claude_office.doc_kind(doc) if doc is not None else None
        toolkit = self.smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        self.container = self.smgr.createInstanceWithContext("com.sun.star.awt.UnoControlContainer", ctx)
        self.container.setModel(self.smgr.createInstanceWithContext("com.sun.star.awt.UnoControlContainerModel", ctx))
        self.container.createPeer(toolkit, parent)
        self.container.setVisible(True)
        self.c = {}
        self._build()
        self._selection_listener = _SelectionListener(self.refresh_context)
        try:
            owner.frame.getController().addSelectionChangeListener(self._selection_listener)
        except Exception:
            self._selection_listener = None
        self.refresh_context()

    # -------------------------------------------------------- building

    def _add(self, kind, name, listener=None, **props):
        model = self.smgr.createInstanceWithContext("com.sun.star.awt.UnoControl%sModel" % kind, self.ctx)
        for k, v in props.items():
            setattr(model, k, v)
        ctrl = self.smgr.createInstanceWithContext("com.sun.star.awt.UnoControl%s" % kind, self.ctx)
        ctrl.setModel(model)
        self.container.addControl(name, ctrl)
        if listener is not None:
            if kind in ("Button",):
                ctrl.addActionListener(_Listener(listener))
            else:
                ctrl.addItemListener(_Listener(listener))
        self.c[name] = ctrl
        return ctrl

    def _build(self):
        writer = self.kind == claude_office.WRITER
        self._add("FixedText", "context", Label="", MultiLine=True)
        if writer:
            self._add("FixedText", "toneLabel", Label="Rewrite as")
            self._add("Button", "formal", lambda: self.rewrite("formal"), Label="Formal")
            self._add("Button", "voice", lambda: self.rewrite("voice"), Label="My voice")
        self.quick = []
        for i, (label, prompt, needs) in enumerate(claude_actions.QUICK_ACTIONS.get(self.kind, [])):
            self._add("Button", "quick%d" % i, (lambda p=prompt, l=label: self.ask({"instruction": p}, l)), Label=label)
            self.quick.append((i, needs))
        self._add("Edit", "prompt", MultiLine=True, VScroll=True, AutoVScroll=True)
        settings = claude_api.load_settings(self.server.settings_path)
        models = [label for _, label in MODEL_CHOICES]
        m_index = next((i for i, (mid, _) in enumerate(MODEL_CHOICES) if mid == (settings.get("model") or "")), 0)
        self._add("ListBox", "model", self._model_changed, Dropdown=True, StringItemList=tuple(models),
                  SelectedItems=(m_index,))
        e_index = next((i for i, (eid, _) in enumerate(EFFORT_CHOICES) if eid == settings.get("effort")), 1)
        self._add("ListBox", "effort", self._effort_changed, Dropdown=True,
                  StringItemList=tuple(l for _, l in EFFORT_CHOICES), SelectedItems=(e_index,))
        if writer:
            self._add("CheckBox", "track", self._track_changed, Label="Tracked",
                      State=1 if settings.get("track_changes") else 0)
        self._add("Button", "ask", self._ask_or_stop, Label="Ask", DefaultButton=True)
        self._add("FixedText", "status", Label="", MultiLine=True)
        self._add("Edit", "reply", MultiLine=True, VScroll=True, ReadOnly=True, Text="")
        first, second = ("Write at selection", "Write below") if self.kind == claude_office.CALC else \
                        ("Replace selection", "Insert below")
        if self.kind in claude_office.WRITABLE:     # Impress/Draw replies are copied, not written in
            self._add("Button", "replace", lambda: self.apply("replace"), Label=first, Enabled=False)
            self._add("Button", "insert", lambda: self.apply("after"), Label=second, Enabled=False)
        self._add("Button", "copy", self.copy, Label="Copy", Enabled=False)
        self._add("Button", "full", self.open_full, Label="Open full panel")
        self._effort_enabled()

    # -------------------------------------------------------- layout

    def layout(self, width, height):
        if not self.visible:
            return
        P, R = self.PAD, self.ROW
        w = max(width - 2 * P, 120)
        y = P

        def place(name, x, yy, ww, hh):
            if name in self.c:
                self.c[name].setPosSize(x, yy, max(ww, 10), hh, POSSIZE)

        def flow(names, y0):
            """Buttons left to right, wrapping; returns the y below them."""
            x, yy = P, y0
            for name in names:
                label = self.c[name].getModel().Label
                bw = min(w, 22 + 7 * len(label))
                if x > P and x + bw > P + w:
                    x, yy = P, yy + R + 4
                place(name, x, yy, bw, R)
                x += bw + 4
            return yy + R + 8

        place("context", P, y, w, 34)
        y += 38
        if "formal" in self.c:
            place("toneLabel", P, y + 5, 70, 18)
            place("formal", P + 72, y, 70, R)
            place("voice", P + 146, y, 84, R)
            y += R + 6
        y = flow(["quick%d" % i for i, _ in self.quick], y)
        place("prompt", P, y, w, 64)
        y += 70
        place("model", P, y, min(130, w // 2), R)
        place("effort", P + min(134, w // 2 + 4), y, min(120, w // 3), R)
        y += R + 6
        if "track" in self.c:
            place("track", P, y + 3, 90, 20)
        place("ask", P + w - 80, y, 80, R)
        y += R + 8
        place("status", P, y, w, 34)
        y += 38
        writes = "replace" in self.c
        bottom = height - P - R - (6 + R if writes else 0)
        reply_h = max(80, bottom - y - 6)
        place("reply", P, y, w, reply_h)
        y += reply_h + 6
        half = (w - 4) // 2
        place("replace", P, y, half, R)
        place("insert", P + half + 4, y, w - half - 4, R)
        if writes:
            y += R + 6
        place("copy", P, y, 64, R)
        place("full", P + 68, y, w - 68, R)

    def set_visible(self, visible):
        self.visible = visible
        self.container.setVisible(visible)

    def note(self, text):
        self.c["status"].getModel().Label = text

    def dispose(self):
        try:
            if self._selection_listener is not None:
                self.owner.frame.getController().removeSelectionChangeListener(self._selection_listener)
        except Exception:
            pass
        try:
            self.container.dispose()
        except Exception:
            pass

    # -------------------------------------------------------- state

    def refresh_context(self):
        doc = self.owner.doc
        if doc is None:
            return
        label, has_selection = claude_office.selection_summary(doc)
        self.c["context"].getModel().Label = label
        self.has_selection = has_selection
        for name in ("formal", "voice"):
            if name in self.c:
                self.c[name].getModel().Enabled = has_selection and not self.busy
        for i, needs in self.quick:
            self.c["quick%d" % i].getModel().Enabled = (has_selection or not needs) and not self.busy

    def _selected(self, name, choices):
        pos = self.c[name].getSelectedItemPos()
        return choices[pos][0] if 0 <= pos < len(choices) else None

    def _model_changed(self):
        model = self._selected("model", MODEL_CHOICES)
        if model is not None:
            self.server.set_settings({"model": model})
        self._effort_enabled()

    def _effort_changed(self):
        effort = self._selected("effort", EFFORT_CHOICES)
        if effort:
            self.server.set_settings({"effort": effort})

    def _effort_enabled(self):
        model = self._selected("model", MODEL_CHOICES) or ""
        self.c["effort"].getModel().Enabled = claude_api.supports_effort(model)

    def _track_changed(self):
        self.server.set_settings({"track_changes": bool(self.c["track"].getModel().State)})

    # -------------------------------------------------------- asking

    def run_action(self, action):
        if action in claude_actions.PROMPTS:
            labels = {"improve": "Improve writing", "summarize": "Summarize", "explain": "Explain"}
            self.ask({"action": action}, labels[action])
        elif action == "settings":
            self.open_full("settings")

    def rewrite(self, tone):
        voice = claude_panel.claude_voice.summary(
            claude_panel.claude_voice.load(self.server._voice_path()))
        if tone == "voice" and not voice["ready"]:
            self.open_full("voice")
            self.note("Teach Claude your writing voice in the full panel first.")
            return
        self.ask({"tone": tone}, "Rewrite · " + ("My voice" if tone == "voice" else "Formal"))

    def _ask_or_stop(self):
        if self.busy:
            self.server.cancel()
            return
        text = self.c["prompt"].getText().strip()
        if not text:
            self.note("Type what you'd like Claude to do.")
            return
        self.c["prompt"].setText("")
        voice = claude_panel.claude_voice.summary(claude_panel.claude_voice.load(self.server._voice_path()))
        body = {"instruction": text}
        if self.kind == claude_office.WRITER and voice["default"] and voice["ready"]:
            body["tone"] = "voice"
        self.ask(body, text)

    def ask(self, body, shown):
        if self.busy:
            return
        doc = self.owner.doc
        self.busy = True
        self._set_busy(True)
        start = time.time()
        body = dict(body, history=list(self.history))

        def tick():
            while self.busy:
                self.note("%s - thinking… %d s" % (shown, time.time() - start))
                time.sleep(0.5)

        def work():
            try:
                res = self.server.ask(body, doc=doc)
            except Exception as e:
                res = {"error": "Unexpected error: %s" % e}
            self.busy = False
            self._done(body, shown, res)

        threading.Thread(target=tick, daemon=True).start()
        threading.Thread(target=work, daemon=True).start()

    def _set_busy(self, busy):
        self.c["ask"].getModel().Label = "Stop" if busy else "Ask"
        for name in ("replace", "insert", "copy"):
            if name in self.c:
                self.c[name].getModel().Enabled = not busy and self.reply is not None
        self.refresh_context()

    def _done(self, body, shown, res):
        self._set_busy(False)
        if res.get("cancelled"):
            self.note("Stopped.")
            return
        if res.get("error"):
            self.note(res["error"])
            if res.get("open_voice"):
                self.open_full("voice")
            elif res.get("open_settings"):
                self.open_full("settings")
            return
        self.reply = res
        self.history += [{"role": "user", "text": body.get("instruction") or shown},
                         {"role": "assistant", "text": res["text"]}]
        self.c["reply"].setText(res["text"])
        for name in ("replace", "insert", "copy"):
            if name in self.c:
                self.c[name].getModel().Enabled = True
        voice = " · in your voice" if res.get("voice") else ""
        where = "choose where it goes" if "replace" in self.c else "copy it"
        self.note("%s%s - %s, or ask for changes." % (shown, voice, where))

    # -------------------------------------------------------- putting the reply in

    def apply(self, mode):
        if self.reply is None:
            return
        text = self.c["reply"].getText()
        doc = self.owner.doc
        res = self.server.apply({"text": text, "mode": mode}, doc=doc)
        if res.get("confirm"):
            if not self._confirm(res["confirm"] + " Replace them?"):
                return
            res = self.server.apply({"text": text, "mode": mode, "confirm": True}, doc=doc)
        if res.get("error"):
            self.note(res["error"])
        elif res.get("tracked"):
            self.note("Added as tracked changes - accept or reject them in Writer.")
        else:
            self.note("Done - Ctrl+Z undoes it.")

    def _confirm(self, text):
        toolkit = self.smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
        box = toolkit.createMessageBox(self.owner.frame.getContainerWindow(), QUERYBOX, BUTTONS_YES_NO,
                                       "Claude", text)
        try:
            return box.execute() == 2          # MessageBoxResults.YES
        finally:
            box.dispose()

    def copy(self):
        if self.reply is None:
            return
        clip = self.smgr.createInstanceWithContext("com.sun.star.datatransfer.clipboard.SystemClipboard", self.ctx)
        clip.setContents(_Text(self.c["reply"].getText()), None)
        self.note("Copied.")

    def open_full(self, action=None):
        """The full web panel in its own window (it never moves LibreOffice)."""
        server = self.server
        if server.is_open():
            server.pending = action or "focus"
        else:
            claude_panel.launch_window(self.ctx, server.url(action))
