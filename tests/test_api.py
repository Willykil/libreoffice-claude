import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extension", "pythonpath"))
import claude_api  # noqa: E402
from mock_server import MockClaude  # noqa: E402


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.mock = MockClaude()
        self.settings = dict(claude_api.DEFAULT_SETTINGS, backend=claude_api.API, api_key="sk-test",
                             base_url=self.mock.url)

    def tearDown(self):
        self.mock.close()

    def test_request_shape(self):
        self.mock.text("Hello there")
        text, truncated = claude_api.ask(self.settings, "SYS", "hi")
        self.assertEqual((text, truncated), ("Hello there", False))
        req = self.mock.requests[0]
        self.assertEqual(req["path"], "/v1/messages")
        self.assertEqual(req["headers"]["x-api-key"], "sk-test")
        self.assertEqual(req["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(req["headers"]["anthropic-beta"], "server-side-fallback-2026-07-01")
        body = req["body"]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["system"], "SYS")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(body["output_config"], {"effort": "medium"})
        self.assertEqual(body["fallbacks"], "default")
        self.assertNotIn("thinking", body)

    def test_haiku_gets_no_effort_or_fallbacks(self):
        body, headers = claude_api.build_request(dict(self.settings, model="claude-haiku-4-5"), "s", "u")
        self.assertNotIn("output_config", body)
        self.assertNotIn("fallbacks", body)
        self.assertNotIn("anthropic-beta", headers)

    def test_truncated(self):
        self.mock.text("partial", stop_reason="max_tokens")
        self.assertEqual(claude_api.ask(self.settings, "s", "u"), ("partial", True))

    def test_refusal(self):
        self.mock.reply = {"content": [], "stop_reason": "refusal",
                           "stop_details": {"type": "refusal", "category": "cyber", "explanation": None}}
        with self.assertRaisesRegex(claude_api.ClaudeError, "declined.*cyber"):
            claude_api.ask(self.settings, "s", "u")

    def test_http_error(self):
        self.mock.status = 401
        self.mock.reply = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        with self.assertRaisesRegex(claude_api.ClaudeError, "401: invalid x-api-key(.|\n)*Settings"):
            claude_api.ask(self.settings, "s", "u")

    def test_unreachable(self):
        with self.assertRaisesRegex(claude_api.ClaudeError, "Could not reach"):
            claude_api.ask(dict(self.settings, base_url="http://127.0.0.1:9"), "s", "u")

    def test_missing_key(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with self.assertRaisesRegex(claude_api.ClaudeError, "No API key"):
            claude_api.ask(dict(self.settings, api_key=""), "s", "u")
        self.assertEqual(self.mock.requests, [])

    def test_env_key_fallback(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-env"
        try:
            self.assertEqual(claude_api.resolve_api_key(dict(self.settings, api_key="")), "sk-env")
            self.assertEqual(claude_api.resolve_api_key(self.settings), "sk-test")
        finally:
            del os.environ["ANTHROPIC_API_KEY"]

    def test_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "s.json")
            self.assertEqual(claude_api.load_settings(path), claude_api.DEFAULT_SETTINGS)
            claude_api.save_settings(path, dict(self.settings, model="claude-sonnet-5-5", junk=1))
            loaded = claude_api.load_settings(path)
            self.assertEqual(loaded["model"], "claude-sonnet-5-5")
            self.assertNotIn("junk", loaded)
            with open(path, "w") as f:
                f.write("{corrupt")
            self.assertEqual(claude_api.load_settings(path), claude_api.DEFAULT_SETTINGS)


if __name__ == "__main__":
    unittest.main()
