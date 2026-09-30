"""Integration test: install the .oxt into a throwaway profile, run headless
LibreOffice, and drive the side panel's API against real Writer/Calc documents.

Needs LibreOffice (soffice) and python3-uno. Talks to a local mock API or a
fake `claude`, never the real service.
"""

import http.client
import json
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
import claude_panel  # noqa: E402
from mock_server import MockClaude  # noqa: E402


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


def call(port, token, path, body=None, host=None):
    """Talk to a panel server the way the page does. Returns (status, headers, json-or-bytes)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Host": host or "127.0.0.1:%d" % port, "Content-Type": "application/json"}
    if token:
        headers["X-Claude-Token"] = token
    data = None if body is None else json.dumps(body)
    conn.request("GET" if body is None else "POST", path, data, headers)
    res = conn.getresponse()
    raw = res.read()
    conn.close()
    try:
        return res.status, dict(res.getheaders()), json.loads(raw)
    except ValueError:
        return res.status, dict(res.getheaders()), raw


class UnoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        profile = uno.systemPathToFileUrl(os.path.join(cls.tmp, "profile"))
        oxt = build.build(os.path.join(cls.tmp, "claude.oxt"))
        env_arg = "-env:UserInstallation=" + profile
        port = _free_port()
        cls.url_file = os.path.join(cls.tmp, "panel-url")
        env = dict(os.environ, CLAUDE_LO_BROWSER="none", CLAUDE_LO_PANEL_URL_FILE=cls.url_file)
        cls.proc = subprocess.Popen(["soffice", "--headless", "--invisible", "--norestore", env_arg,
                                     "--accept=socket,host=127.0.0.1,port=%d;urp;" % port],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
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
        # A panel server in this process, driving the office over the UNO bridge.
        cls.panel = claude_panel.PanelServer(cls.ctx, cls.settings_path)

    @classmethod
    def tearDownClass(cls):
        cls.panel.shutdown()
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
        self.mock.text("ok")
        claude_api.save_settings(self.settings_path, dict(claude_api.DEFAULT_SETTINGS, backend=claude_api.API,
                                                          api_key="sk-test", base_url=self.mock.url))

    def open(self, kind):
        doc = self.desktop.loadComponentFromURL("private:factory/" + kind, "_blank", 0, (_prop("Hidden", True),))
        self.addCleanup(doc.close, True)
        self.panel.document = lambda: doc      # hidden documents never become "current"
        return doc

    def api(self, path, body=None):
        status, _, data = call(self.panel.port, self.panel.token, path, body)
        self.assertEqual(status, 200, data)
        return data

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
        image = node.getByName("Images").getByName("org.willykil.claude.image.ask")
        self.assertTrue(image.getByName("UserDefinedImages").getPropertyValue("ImageSmallURL")
                        .endswith("/icons/sparkle_16.png"))
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

    def test_menu_opens_panel_server_inside_office(self):
        # The real path: the menu's Job runs inside soffice, starts the server there, and
        # (with the browser disabled for the test) reports the panel URL.
        self.open("swriter")
        job = self.ctx.ServiceManager.createInstanceWithContext("org.willykil.claude.Job", self.ctx)
        job.trigger("ask")
        with open(self.url_file) as f:
            url = f.read()
        port = int(url.split(":")[2].split("/")[0])
        token = url.split("t=")[1].split("&")[0]
        status, _, state = call(port, token, "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["connection"], "subscription")    # the office profile's default settings
        # Panel now counts as open: another menu action is queued for it, not a new window.
        job.trigger("summarize")
        self.assertEqual(call(port, token, "/api/state")[2]["pending"], "summarize")
        self.assertIsNone(call(port, token, "/api/state")[2]["pending"])

    # ---- the server's guard rails

    def test_requires_token_and_local_host(self):
        port = self.panel.port
        self.assertEqual(call(port, None, "/api/state")[0], 403)
        self.assertEqual(call(port, "wrong", "/api/state")[0], 403)
        self.assertEqual(call(port, self.panel.token, "/api/state", host="evil.example:%d" % port)[0], 403)
        self.assertEqual(call(port, None, "/api/ask", {"instruction": "x"})[0], 403)
        self.assertEqual(self.mock.requests, [])

    def test_serves_panel_page(self):
        status, headers, body = call(self.panel.port, None, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"How can I help?", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        for path in ("/app.js", "/style.css"):
            self.assertEqual(call(self.panel.port, None, path)[0], 200)
        self.assertEqual(call(self.panel.port, None, "/../pythonpath/claude_api.py")[0], 403)

    def test_settings_api_never_returns_key(self):
        s = self.api("/api/settings")
        self.assertNotIn("api_key", s)
        self.assertTrue(s["has_api_key"])
        s = self.api("/api/settings", {"backend": "claude_code", "effort": "high", "model": "",
                                       "instructions_writer": "Write in Canadian French.",
                                       "track_changes": True})
        self.assertEqual((s["backend"], s["effort"], s["track_changes"]), ("claude_code", "high", True))
        s = self.api("/api/settings", {"clear_api_key": True})
        self.assertFalse(s["has_api_key"])
        stored = claude_api.load_settings(self.settings_path)
        self.assertEqual(stored["instructions_writer"], "Write in Canadian French.")
        self.assertEqual(stored["instructions_calc"], "")

    def test_model_and_effort_pickers(self):
        self.writer_with_selection()
        state = self.api("/api/state")
        self.assertEqual([m["label"] for m in state["models"]][:3], ["Opus 5.5", "Sonnet 5.5", "Haiku 4.5"])
        self.assertIn({"id": "xhigh", "label": "Extra high"}, state["efforts"])
        self.api("/api/settings", {"model": "claude-sonnet-5-5", "effort": "low"})
        state = self.api("/api/state")
        self.assertEqual((state["model"], state["effort"], state["effort_supported"]), ("claude-sonnet-5-5", "low", True))
        self.api("/api/ask", {"instruction": "x"})
        body = self.mock.requests[-1]["body"]
        self.assertEqual((body["model"], body["output_config"]), ("claude-sonnet-5-5", {"effort": "low"}))
        self.api("/api/settings", {"model": "claude-haiku-4-5"})
        self.assertFalse(self.api("/api/state")["effort_supported"])
        self.api("/api/ask", {"instruction": "x"})
        self.assertNotIn("output_config", self.mock.requests[-1]["body"])

    def test_per_app_instructions(self):
        self.api("/api/settings", {"instructions_writer": "WRITER-RULE", "instructions_calc": "CALC-RULE"})
        self.writer_with_selection()
        self.api("/api/ask", {"instruction": "x"})
        self.assertIn("WRITER-RULE", self.last_system())
        self.assertNotIn("CALC-RULE", self.last_system())
        self.calc_with_data()
        self.api("/api/ask", {"instruction": "x"})
        self.assertIn("CALC-RULE", self.last_system())

    def test_old_single_instructions_migrate(self):
        path = os.path.join(self.tmp, "old.json")
        with open(path, "w") as f:
            json.dump({"extra_instructions": "Be brief."}, f)
        s = claude_api.load_settings(path)
        self.assertEqual((s["instructions_writer"], s["instructions_calc"]), ("Be brief.", "Be brief."))

    def test_history_saved_on_this_computer(self):
        self.assertEqual(self.api("/api/history", {"clear": True}), {"conversations": []})
        convo = {"id": "abc", "title": "Fix grammar", "messages": [{"role": "user", "text": "hi"}]}
        self.api("/api/history", {"save": convo})
        self.api("/api/history", {"save": dict(convo, id="def", title="Second")})
        items = self.api("/api/history", {})["conversations"]
        self.assertEqual([c["id"] for c in items], ["def", "abc"])      # newest first
        self.api("/api/history", {"delete": "def"})
        self.assertEqual([c["id"] for c in self.api("/api/history", {})["conversations"]], ["abc"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "claude-panel-history.json")))

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

    def test_state_describes_selection(self):
        self.writer_with_selection()
        state = self.api("/api/state")
        self.assertEqual(state["doc"]["kind"], "writer")
        self.assertEqual(state["doc"]["label"], "Selection: 4 words")
        self.assertTrue(state["doc"]["has_selection"])
        self.assertEqual(state["connection"], "api")
        quick = {q["label"]: q for q in state["quick"]}
        self.assertTrue(quick["Fix grammar"]["needs_selection"])
        self.assertFalse(quick["Summarize"]["needs_selection"])

    def test_ask_then_replace_selection(self):
        doc = self.writer_with_selection()
        self.mock.text("This sentence has errors.\n\nSecond line.")
        res = self.api("/api/ask", {"action": "improve"})
        self.assertEqual(res["text"], "This sentence has errors.\n\nSecond line.")
        self.assertIsNone(res["grid"])
        self.assertIn("<selection>\nthis sentense has erors\n</selection>", self.last_prompt())
        self.assertIn("[P1] First paragraph stays.\n[P2] this sentense has erors", self.last_prompt())
        self.assertIn("[P12]", self.last_system())
        self.assertIn(claude_actions.PROMPTS["improve"], self.last_prompt())
        self.assertIn("LibreOffice Writer", self.last_system())
        self.assertEqual(self.api("/api/apply", {"text": res["text"], "mode": "replace"}), {"ok": True, "tracked": False})
        self.assertEqual(self.paragraphs(doc),
                         ["First paragraph stays.", "This sentence has errors.", "", "Second line."])
        doc.getUndoManager().undo()   # one undo step reverts the whole insertion
        self.assertEqual(self.paragraphs(doc), ["First paragraph stays.", "this sentense has erors"])

    def test_insert_below(self):
        doc = self.writer_with_selection()
        self.api("/api/apply", {"text": "Added.", "mode": "after"})
        self.assertEqual(self.paragraphs(doc), ["First paragraph stays.", "this sentense has erors", "Added."])

    def test_follow_up_carries_the_conversation(self):
        self.writer_with_selection()
        self.api("/api/ask", {"instruction": "Fix it", "history": [
            {"role": "user", "text": "Fix it"}, {"role": "assistant", "text": "First try."}]})
        prompt = self.last_prompt()
        self.assertIn("<conversation>\nUser: Fix it\n\nClaude: First try.\n</conversation>", prompt)
        self.assertTrue(prompt.endswith("New request: Fix it"))

    def test_no_selection_uses_document_incl_tables(self):
        doc = self.open("swriter")
        doc.getText().setString("Intro text")
        table = doc.createInstance("com.sun.star.text.TextTable")
        table.initialize(2, 2)
        text = doc.getText()
        text.insertTextContent(text.getEnd(), table, False)
        table.getCellByName("A1").setString("Qty")
        table.getCellByName("B2").setString("42")
        self.api("/api/ask", {"action": "summarize"})
        self.assertIn("<document>\n[P1] Intro text\n[P2] (table)\nQty\t\n\t42", self.last_prompt())
        self.assertIn("Nothing is selected.", self.last_prompt())

    def test_comments_and_tracked_changes_are_read(self):
        doc = self.open("swriter")
        text = doc.getText()
        text.setString("The fee is 100 dollars.")
        note = doc.createInstance("com.sun.star.text.textfield.Annotation")
        note.Author, note.Content = "Bob", "Too high?"
        text.insertTextContent(text.getEnd(), note, False)
        doc.RecordChanges = True
        text.insertString(text.getEnd(), " Payable monthly.", False)
        doc.RecordChanges = False
        self.api("/api/ask", {"instruction": "Summarize the redlines"})
        prompt = self.last_prompt()
        self.assertIn("Comments in the document:\n- Bob: Too high?", prompt)
        self.assertIn('Insertion by', prompt)
        self.assertIn('" Payable monthly."', prompt)

    def test_citations_jump_to_the_text(self):
        doc = self.writer_with_selection()
        self.api("/api/goto", {"ref": "P2"})
        self.assertEqual(doc.getCurrentController().getSelection().getByIndex(0).getString(),
                         "this sentense has erors")
        self.api("/api/goto", {"ref": "P1-P2"})
        self.assertEqual(doc.getCurrentController().getSelection().getByIndex(0).getString(),
                         "First paragraph stays.\nthis sentense has erors")
        self.assertIn("isn't in the document", self.api("/api/goto", {"ref": "P9"})["error"])

    def test_tracked_changes_mode(self):
        doc = self.writer_with_selection()
        self.api("/api/settings", {"track_changes": True})
        res = self.api("/api/apply", {"text": "This sentence has errors [P2].", "mode": "replace"})
        self.assertEqual(res, {"ok": True, "tracked": True})
        types = []
        e = doc.getRedlines().createEnumeration()
        while e.hasMoreElements():
            types.append(e.nextElement().getPropertyValue("RedlineType"))
        self.assertIn("Insert", types)
        self.assertIn("Delete", types)
        self.assertFalse(doc.RecordChanges)          # left as it was
        self.assertIn("This sentence has errors.", doc.getText().getString())
        self.assertNotIn("[P2]", doc.getText().getString())

    def test_errors_come_back_as_messages(self):
        self.writer_with_selection()
        self.assertIn("Type what", self.api("/api/ask", {"instruction": "  "})["error"])
        self.mock.status = 500
        self.mock.reply = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
        self.assertEqual(self.api("/api/ask", {"instruction": "x"})["error"], "Claude API error 500: boom")
        claude_api.save_settings(self.settings_path, dict(claude_api.DEFAULT_SETTINGS, backend=claude_api.API))
        os.environ.pop("ANTHROPIC_API_KEY", None)
        res = self.api("/api/ask", {"instruction": "x"})
        self.assertTrue(res["open_settings"])

    def test_claude_code_connection_and_stop(self):
        from test_cli import make_fake_claude
        self.writer_with_selection()
        exe = make_fake_claude(self.tmp)
        claude_api.save_settings(self.settings_path, dict(claude_api.DEFAULT_SETTINGS, claude_path=exe))
        os.environ["FAKE_CLAUDE_REPLY"] = "Cette phrase n'a pas d'erreurs."
        try:
            self.assertEqual(self.api("/api/ask", {"instruction": "Corrige"})["text"],
                             "Cette phrase n'a pas d'erreurs.")
            os.environ["FAKE_CLAUDE_MODE"] = "slow"
            import threading
            out = {}
            t = threading.Thread(target=lambda: out.update(self.api("/api/ask", {"instruction": "slow"})))
            t.start()
            time.sleep(1.0)
            self.assertTrue(self.api("/api/state")["busy"])
            start = time.time()
            self.api("/api/cancel", {})
            t.join(10)
            self.assertEqual(out, {"cancelled": True})
            self.assertLess(time.time() - start, 5)
            self.assertFalse(self.api("/api/state")["busy"])
        finally:
            for k in ("FAKE_CLAUDE_REPLY", "FAKE_CLAUDE_MODE"):
                os.environ.pop(k, None)
        self.assertEqual(self.mock.requests, [])

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

    def test_calc_grid_reply_written_below(self):
        doc, sheet = self.calc_with_data()
        doc.getCurrentController().select(sheet.getCellRangeByName("A1:B3"))
        self.mock.text("```tsv\nTotal\t=SUM(B2:B3)\nTaxed\t=IF(B4>5,B4*1.2,\"n/a\")\n```")
        res = self.api("/api/ask", {"instruction": "add a total"})
        self.assertIn('Selected range Sheet1!A1:B3:\n\tA\tB\n1\tItem\tPrice\n2\tTea\t3\n3\tCake\t4.5',
                      self.last_prompt())
        self.assertIn("LibreOffice Calc", self.last_system())
        self.assertEqual(res["grid"], [["Total", "=SUM(B2:B3)"], ["Taxed", '=IF(B4>5,B4*1.2,"n/a")']])
        self.api("/api/apply", {"text": res["text"], "mode": "after"})
        self.assertEqual(sheet.getCellRangeByName("A4").getString(), "Total")
        self.assertEqual(sheet.getCellRangeByName("B4").getValue(), 7.5)
        self.assertEqual(sheet.getCellRangeByName("B5").getFormula(), '=IF(B4>5;B4*1.2;"n/a")')
        self.assertAlmostEqual(sheet.getCellRangeByName("B5").getValue(), 9.0)

    def test_calc_single_cell_uses_used_area_and_writes_at_cursor(self):
        doc, sheet = self.calc_with_data()
        doc.getCurrentController().select(sheet.getCellRangeByName("D2"))
        self.mock.text("| Name | Code |\n|---|---|\n| Bond | 007 |")
        res = self.api("/api/ask", {"instruction": "table"})
        self.assertIn("the cursor is on cell Sheet1!D2", self.last_prompt())
        self.assertIn("3\tCake\t4.5", self.last_prompt())
        self.api("/api/apply", {"text": res["text"], "mode": "replace"})
        written = doc.getCurrentController().getSelection().getRangeAddress()
        self.assertEqual((written.StartColumn, written.StartRow, written.EndColumn, written.EndRow), (3, 1, 4, 2))
        self.assertEqual(sheet.getCellRangeByName("D2").getString(), "Name")
        self.assertEqual(sheet.getCellRangeByName("E3").getString(), "007")   # stays text
        self.assertEqual(sheet.getCellRangeByName("E3").getType().value, "TEXT")
        state = self.api("/api/state")
        self.assertIn("Find errors", [q["label"] for q in state["quick"]])

    def test_calc_reads_every_sheet_and_jumps_to_cells(self):
        doc, sheet = self.calc_with_data()
        doc.getSheets().insertNewByName("Costs", 1)
        costs = doc.getSheets().getByName("Costs")
        costs.getCellRangeByName("A1").setString("Rent")
        costs.getCellRangeByName("B1").setValue(900)
        doc.getSheets().insertNewByName("Empty", 2)
        doc.getCurrentController().select(sheet.getCellRangeByName("A1"))
        self.api("/api/ask", {"instruction": "what's the rent?"})
        prompt = self.last_prompt()
        self.assertIn("Workbook sheets: Sheet1, Costs, Empty.", prompt)
        self.assertIn('Sheet "Costs" (A1:B1):\n\tA\tB\n1\tRent\t900', prompt)
        self.assertIn('Sheet "Empty" is empty.', prompt)
        self.assertIn("[Data!C4]", self.last_system())
        self.api("/api/goto", {"ref": "Costs!B1"})
        ctl = doc.getCurrentController()
        self.assertEqual(ctl.getActiveSheet().getName(), "Costs")
        self.assertEqual(ctl.getSelection().getRangeAddress().StartColumn, 1)
        self.api("/api/goto", {"ref": "Sheet1!$A$2:B3"})
        addr = ctl.getSelection().getRangeAddress()
        self.assertEqual((ctl.getActiveSheet().getName(), addr.StartRow, addr.EndRow), ("Sheet1", 1, 2))
        self.assertIn("no sheet", self.api("/api/goto", {"ref": "Nope!A1"})["error"])

    def test_calc_asks_before_overwriting(self):
        doc, sheet = self.calc_with_data()
        doc.getCurrentController().select(sheet.getCellRangeByName("A2"))
        res = self.api("/api/apply", {"text": "X\tY", "mode": "replace"})
        self.assertEqual(res, {"confirm": "This replaces 2 cells that already have content (A2:B2)."})
        self.assertEqual(sheet.getCellRangeByName("A2").getString(), "Tea")     # untouched
        self.assertTrue(self.api("/api/apply", {"text": "X\tY", "mode": "replace", "confirm": True})["ok"])
        self.assertEqual(sheet.getCellRangeByName("A2").getString(), "X")
        # Writing into empty cells needs no confirmation.
        doc.getCurrentController().select(sheet.getCellRangeByName("A1:B3"))
        self.assertTrue(self.api("/api/apply", {"text": "Total\t7.5", "mode": "after"})["ok"])


if __name__ == "__main__":
    unittest.main()
