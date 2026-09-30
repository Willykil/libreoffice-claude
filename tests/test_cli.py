import json
import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "extension", "pythonpath"))
import claude_api  # noqa: E402
import claude_cli  # noqa: E402


def make_fake_claude(directory):
    """An executable `claude` wrapping fake_claude.py (a .cmd on Windows)."""
    script = os.path.join(HERE, "fake_claude.py")
    if sys.platform == "win32":
        path = os.path.join(directory, "claude.cmd")
        with open(path, "w") as f:
            f.write('@"%s" "%s" %%*\n' % (sys.executable, script))
    else:
        path = os.path.join(directory, "claude")
        with open(path, "w") as f:
            f.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exe = make_fake_claude(self.tmp.name)
        self.log = os.path.join(self.tmp.name, "log.json")
        os.environ["FAKE_CLAUDE_LOG"] = self.log
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        self.settings = dict(claude_api.DEFAULT_SETTINGS, claude_path=self.exe)

    def tearDown(self):
        self.tmp.cleanup()
        for k in ("FAKE_CLAUDE_LOG", "FAKE_CLAUDE_MODE", "FAKE_CLAUDE_REPLY"):
            os.environ.pop(k, None)

    def logged(self):
        with open(self.log, encoding="utf-8") as f:
            return json.load(f)

    def test_default_backend_is_claude_code(self):
        self.assertEqual(claude_api.DEFAULT_SETTINGS["backend"], claude_api.CLAUDE_CODE)
        os.environ["FAKE_CLAUDE_REPLY"] = "Bonjour à tous"
        self.assertEqual(claude_api.ask(self.settings, "SYS é", "texte ü"), ("Bonjour à tous", False))
        call = self.logged()
        self.assertEqual(call["system"], "SYS é")
        self.assertEqual(call["stdin"], "texte ü")
        argv = call["argv"]
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertNotIn("--model", argv)            # blank model: Claude Code's default
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(tempfile.gettempdir()))

    def test_model_passed_through(self):
        claude_api.ask(dict(self.settings, model="claude-sonnet-5-5", effort=""), "s", "u")
        argv = self.logged()["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertNotIn("--effort", argv)

    def test_system_prompt_file_removed(self):
        claude_api.ask(self.settings, "s", "u")
        argv = self.logged()["argv"]
        self.assertFalse(os.path.exists(argv[argv.index("--system-prompt-file") + 1]))

    def test_logged_out(self):
        os.environ["FAKE_CLAUDE_MODE"] = "logged_out"
        with self.assertRaisesRegex(claude_api.ClaudeError, "isn't signed in"):
            claude_api.ask(self.settings, "s", "u")

    def test_crash_shows_stderr(self):
        os.environ["FAKE_CLAUDE_MODE"] = "crash"
        with self.assertRaisesRegex(claude_api.ClaudeError, "something broke"):
            claude_api.ask(self.settings, "s", "u")

    def test_not_installed(self):
        with self.assertRaisesRegex(claude_api.ClaudeError, "install.ps1"):
            claude_api.ask(dict(self.settings, claude_path=os.path.join(self.tmp.name, "nope")), "s", "u")

    def test_found_on_path(self):
        old = os.environ["PATH"]
        os.environ["PATH"] = self.tmp.name + os.pathsep + old
        try:
            self.assertEqual(os.path.realpath(claude_cli.find_claude({})), os.path.realpath(self.exe))
        finally:
            os.environ["PATH"] = old


if __name__ == "__main__":
    unittest.main()
