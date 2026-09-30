"""Integration test: install the .oxt into a throwaway profile, run headless
LibreOffice, and drive the real actions against real Writer/Calc documents.

Needs LibreOffice (soffice) and python3-uno. Talks to a local mock
API, never the real one.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "extension", "pythonpath"))
sys.path.insert(0, os.path.join(HERE, ".."))

import uno  # noqa: E402
import unohelper  # noqa: E402
from com.sun.star.beans import PropertyValue  # noqa: E402
from com.sun.star.task import XInteractionHandler  # noqa: E402
from com.sun.star.ucb import XCommandEnvironment  # noqa: E402

import build  # noqa: E402
import claude_actions  # noqa: E402
import claude_api  # noqa: E402
from mock_server import MockClaude  # noqa: E402

REPLACE, INSERT, CANCEL = 2, 3, 0


class FakeUI:
    REPLACE, INSERT = REPLACE, INSERT

    def __init__(self, prompt=None, choice=CANCEL):
        self.prompt, self.choice = prompt, choice
        self.messages, self.shown = [], []

    def message(self, text, error=False):
        self.messages.append((text, error))

    def ask_prompt(self, label):
        self.label = label
        return self.prompt

    def show_result(self, text, kind, truncated):
        self.shown.append((text, kind, truncated))
        return self.choice, text

    def edit_settings(self, settings):
        return None

    def wait(self, fn):
        return fn()


class _Approve(unohelper.Base, XInteractionHandler):
    def handle(self, request):
        for c in request.getContinuations():
            if c.queryInterface(uno.getTypeByName("com.sun.star.task.XInteractionApprove")):
                c.select()
                return


class _CommandEnv(unohelper.Base, XCommandEnvironment):
    def getInteractionHandler(self):
        return _Approve()

    def getProgressHandler(self):
        return None


def _prop(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UnoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        profile = uno.systemPathToFileUrl(os.path.join(cls.tmp, "profile"))
        oxt = build.build(os.path.join(cls.tmp, "claude.oxt"))
        env_arg = "-env:UserInstallation=" + profile
        port = _free_port()
        cls.proc = subprocess.Popen(["soffice", "--headless", "--invisible", "--norestore", env_arg,
                                     "--accept=socket,host=127.0.0.1,port=%d;urp;" % port],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        for _ in range(60):
            try:
                cls.ctx = resolver.resolve(
                    "uno:socket,host=127.0.0.1,port=%d;urp;StarOffice.ComponentContext" % port)
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("soffice did not start")
        # Install through the running office's ExtensionManager, as Tools > Extensions does.
        # (unopkg refuses to run as root, which CI containers often are.)
        manager = cls.ctx.getValueByName("/singletons/com.sun.star.deployment.ExtensionManager")
        manager.addExtension(uno.systemPathToFileUrl(oxt), (), "user", None, _CommandEnv())
        cls.desktop = cls.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", cls.ctx)
        cls.mock = MockClaude()
        cls.settings_path = os.path.join(cls.tmp, "settings.json")
        claude_api.save_settings(cls.settings_path, dict(claude_api.DEFAULT_SETTINGS,
                                                         api_key="sk-test", base_url=cls.mock.url))

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()
        try:
            cls.desktop.terminate()
        except Exception:
            pass
        try:
            cls.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.mock.requests.clear()

    def open(self, kind):
        doc = self.desktop.loadComponentFromURL("private:factory/" + kind, "_blank", 0, (_prop("Hidden", True),))
        self.addCleanup(doc.close, True)
        return doc

    def run_action(self, doc, action, ui):
        return claude_actions.run(self.ctx, doc, action, ui, path=self.settings_path)

    def last_prompt(self):
        return self.mock.requests[-1]["body"]["messages"][0]["content"]

    def last_system(self):
        return self.mock.requests[-1]["body"]["system"]

    # ---- installation

    def test_component_registered(self):
        job = self.ctx.ServiceManager.createInstanceWithContext("org.willykil.claude.Job", self.ctx)
        self.assertIsNotNone(job, "extension's Python component failed to load")

    def test_menu_registered(self):
        cp = self.ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.configuration.ConfigurationProvider", self.ctx)
        node = cp.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess",
                                              (_prop("nodepath", "/org.openoffice.Office.Addons/AddonUI"),))
        menu = node.getByName("OfficeMenuBar").getByName("org.willykil.claude.menu")
        urls = [menu.getByName("Submenu").getByName(n).getPropertyValue("URL")
                for n in menu.getByName("Submenu").getElementNames()]
        for action in ("ask", "improve", "summarize", "explain", "settings"):
            self.assertIn("service:org.willykil.claude.Job?" + action, urls)
        tab = node.getByName("OfficeNotebookBar").getByName("org.willykil.claude.notebookbar")
        self.assertEqual(tab.getByName("n1").getPropertyValue("URL"), "service:org.willykil.claude.Job?ask")
        merging = node.getByName("OfficeToolbarMerging").getByName("org.willykil.claude")
        for name in ("writer", "calc"):
            self.assertEqual(merging.getByName(name).getPropertyValue("MergeToolBar"), "standardbar")

    def test_settings_path_in_profile(self):
        path = claude_actions.settings_path(self.ctx)
        self.assertTrue(path.startswith(os.path.join(self.tmp, "profile")), path)

    # ---- Writer

    def writer_with_selection(self):
        doc = self.open("swriter")
        text = doc.getText()
        text.setString("First paragraph stays.")
        cur = text.createTextCursorByRange(text.getEnd())
        text.insertControlCharacter(cur, 0, False)  # PARAGRAPH_BREAK
        text.insertString(cur, "this sentense has erors", False)
        cur.gotoStartOfParagraph(True)
        doc.getCurrentController().select(cur)
        return doc

    def paragraphs(self, doc):
        out, e = [], doc.getText().createEnumeration()
        while e.hasMoreElements():
            out.append(e.nextElement().getString())
        return out

    def test_writer_improve_replaces_selection(self):
        doc = self.writer_with_selection()
        self.mock.text("This sentence has errors.\n\nSecond line.")
        ui = FakeUI(choice=REPLACE)
        self.run_action(doc, "improve", ui)
        self.assertIn("<selection>\nthis sentense has erors\n</selection>", self.last_prompt())
        self.assertIn("LibreOffice Writer", self.last_system())
        self.assertEqual(ui.shown[0][1], "writer")
        self.assertEqual(self.paragraphs(doc),
                         ["First paragraph stays.", "This sentence has errors.", "", "Second line."])
        doc.getUndoManager().undo()   # one undo step reverts the whole insertion
        self.assertEqual(self.paragraphs(doc), ["First paragraph stays.", "this sentense has erors"])

    def test_writer_insert_after(self):
        doc = self.writer_with_selection()
        self.mock.text("Added.")
        self.run_action(doc, "ask", FakeUI(prompt="continue", choice=INSERT))
        self.assertTrue(self.last_prompt().endswith("\n\ncontinue"))
        self.assertEqual(self.paragraphs(doc),
                         ["First paragraph stays.", "this sentense has erors", "Added."])

    def test_writer_no_selection_uses_document_incl_tables(self):
        doc = self.open("swriter")
        doc.getText().setString("Intro text")
        table = doc.createInstance("com.sun.star.text.TextTable")
        table.initialize(2, 2)
        text = doc.getText()
        text.insertTextContent(text.getEnd(), table, False)
        table.getCellByName("A1").setString("Qty")
        table.getCellByName("B2").setString("42")
        self.mock.text("Summary.")
        ui = FakeUI()
        self.run_action(doc, "summarize", ui)
        prompt = self.last_prompt()
        self.assertIn("<document>\nIntro text\nQty\t\n\t42", prompt)
        self.assertEqual(ui.shown[0][0], "Summary.")

    def test_writer_improve_needs_selection(self):
        doc = self.open("swriter")
        doc.getText().setString("x")
        ui = FakeUI()
        self.run_action(doc, "improve", ui)
        self.assertEqual(self.mock.requests, [])
        self.assertIn("Select the text", ui.messages[0][0])

    def test_api_error_is_shown(self):
        doc = self.writer_with_selection()
        self.mock.status = 500
        self.mock.reply = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
        ui = FakeUI(choice=REPLACE)
        self.run_action(doc, "improve", ui)
        self.assertEqual(ui.messages, [("Claude API error 500: boom", True)])
        self.assertEqual(ui.shown, [])

    # ---- Calc

    def calc_with_data(self):
        doc = self.open("scalc")
        sheet = doc.getSheets().getByIndex(0)
        rows = [("Item", "Price"), ("Tea", 3), ("Cake", 4.5)]
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                cell = sheet.getCellByPosition(c, r)
                cell.setValue(v) if isinstance(v, (int, float)) else cell.setString(v)
        return doc, sheet

    def test_calc_write_below_with_formulas(self):
        doc, sheet = self.calc_with_data()
        doc.getCurrentController().select(sheet.getCellRangeByName("A1:B3"))
        self.mock.text("```tsv\nTotal\t=SUM(B2:B3)\nTaxed\t=IF(B4>5,B4*1.2,\"n/a\")\n```")
        ui = FakeUI(prompt="add a total", choice=INSERT)
        self.run_action(doc, "ask", ui)
        prompt = self.last_prompt()
        self.assertIn('Sheet "Sheet1", selected range A1:B3:\n\tA\tB\n1\tItem\tPrice\n2\tTea\t3\n3\tCake\t4.5',
                      prompt)
        self.assertIn("LibreOffice Calc", self.last_system())
        self.assertEqual(sheet.getCellRangeByName("A4").getString(), "Total")
        self.assertEqual(sheet.getCellRangeByName("B4").getValue(), 7.5)
        self.assertEqual(sheet.getCellRangeByName("B5").getFormula(), '=IF(B4>5;B4*1.2;"n/a")')
        self.assertAlmostEqual(sheet.getCellRangeByName("B5").getValue(), 9.0)

    def test_calc_single_cell_uses_used_area_and_writes_at_cursor(self):
        doc, sheet = self.calc_with_data()
        doc.getCurrentController().select(sheet.getCellRangeByName("D2"))
        self.mock.text("| Name | Code |\n|---|---|\n| Bond | 007 |")
        self.run_action(doc, "ask", FakeUI(prompt="table", choice=REPLACE))
        self.assertIn("cursor is on cell D2", self.last_prompt())
        self.assertIn("3\tCake\t4.5", self.last_prompt())
        self.assertEqual(sheet.getCellRangeByName("D2").getString(), "Name")
        self.assertEqual(sheet.getCellRangeByName("E3").getString(), "007")   # stays text
        self.assertEqual(sheet.getCellRangeByName("E3").getType().value, "TEXT")

    def test_dialogs_build(self):
        # Headless LibreOffice cancels modal dialogs immediately, but building them still
        # validates every control model and property name against the real toolkit.
        import claude_dialogs
        self.assertIsNone(claude_dialogs.ask_prompt(self.ctx, "Selection: 3 words", "hi"))
        self.assertEqual(claude_dialogs.show_result(self.ctx, "reply", "calc", True), (0, "reply"))
        self.assertEqual(claude_dialogs.show_result(self.ctx, "reply", "writer", False)[0], 0)
        self.assertIsNone(claude_dialogs.edit_settings(self.ctx, dict(claude_api.DEFAULT_SETTINGS)))
        claude_dialogs.message(self.ctx, None, "hello")

    def test_job_trigger_runs_in_office(self):
        job = self.ctx.ServiceManager.createInstanceWithContext("org.willykil.claude.Job", self.ctx)
        self.open("swriter")
        job.trigger("settings")
        job.trigger("ask")      # prompt dialog is auto-cancelled: no request goes out
        self.assertEqual(self.mock.requests, [])

    def test_wait_responsive_over_real_toolkit(self):
        doc = self.open("swriter")
        frame = doc.getCurrentController().getFrame()
        self.assertEqual(claude_actions.wait_responsive(self.ctx, frame, lambda: (time.sleep(0.3), 5)[1]), 5)
        with self.assertRaises(ValueError):
            claude_actions.wait_responsive(self.ctx, frame, lambda: int("x"))


if __name__ == "__main__":
    unittest.main()
