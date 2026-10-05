import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "extension", "pythonpath"))
import claude_api  # noqa: E402
import claude_usage  # noqa: E402
from mock_server import MockClaude  # noqa: E402
from test_cli import make_fake_claude  # noqa: E402

PLAN = {"session": {"total_cost_usd": 0}, "subscription_type": "max", "rate_limits_available": True,
        "rate_limits": {"five_hour": {"utilization": 34.0, "resets_at": "2026-10-05T03:00:00Z"},
                        "seven_day": {"utilization": 12.5, "resets_at": "2026-10-09T16:00:00Z"},
                        "seven_day_opus": None,
                        "model_scoped": [{"display_name": "Fable", "utilization": 50, "resets_at": None}],
                        "extra_usage": {"is_enabled": True, "monthly_limit": 5000, "used_credits": 1230,
                                        "utilization": 24.6, "currency": "USD"}}}


class UsageTest(unittest.TestCase):
    def setUp(self):
        claude_usage.reset()

    def test_cost_uses_list_prices(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 1_000_000,
                 "cache_creation_input_tokens": 1_000_000}
        self.assertAlmostEqual(claude_usage.cost("claude-opus-5-5", usage), 4 + 2 + 0.2 + 5)
        self.assertIsNone(claude_usage.cost("some-new-model", usage))

    def test_rate_limit_headers(self):
        h = {"anthropic-ratelimit-requests-limit": "50", "anthropic-ratelimit-requests-remaining": "49",
             "anthropic-ratelimit-requests-reset": "2026-10-05T00:01:00Z",
             "anthropic-ratelimit-input-tokens-limit": "30000", "anthropic-ratelimit-input-tokens-remaining": "x"}
        self.assertEqual(claude_usage.rate_limits_from_headers(h), {
            "requests": {"limit": 50, "remaining": 49, "reset": "2026-10-05T00:01:00Z"},
            "input-tokens": {"limit": 30000}})
        self.assertIsNone(claude_usage.rate_limits_from_headers({}))

    def test_api_replies_are_recorded(self):
        mock = MockClaude()
        try:
            mock.text("hi")
            mock.reply["usage"] = {"input_tokens": 1000, "output_tokens": 200}
            mock.reply["model"] = "claude-sonnet-5-5"
            mock.headers = {"anthropic-ratelimit-requests-limit": "50",
                            "anthropic-ratelimit-requests-remaining": "48"}
            settings = dict(claude_api.DEFAULT_SETTINGS, backend=claude_api.API, api_key="k", base_url=mock.url)
            claude_api.ask(settings, "S", "q")
            claude_api.ask(settings, "S", "q")
        finally:
            mock.close()
        snap = claude_usage.api_snapshot()
        self.assertEqual(snap["rate_limits"]["requests"], {"limit": 50, "remaining": 48})
        s = snap["session"]
        self.assertEqual((s["requests"], s["input_tokens"], s["output_tokens"]), (2, 2000, 400))
        self.assertAlmostEqual(s["cost_usd"], 2 * (1000 * 2 + 200 * 10) / 1e6)

    def test_rate_limited_reply_keeps_headers(self):
        mock = MockClaude()
        try:
            mock.status = 429
            mock.reply = {"error": {"message": "slow down"}}
            mock.headers = {"anthropic-ratelimit-requests-limit": "50",
                            "anthropic-ratelimit-requests-remaining": "0", "retry-after": "12"}
            settings = dict(claude_api.DEFAULT_SETTINGS, backend=claude_api.API, api_key="k", base_url=mock.url)
            with self.assertRaises(claude_api.ClaudeError):
                claude_api.ask(settings, "S", "q")
        finally:
            mock.close()
        rl = claude_usage.api_snapshot()["rate_limits"]
        self.assertEqual((rl["requests"]["remaining"], rl["retry_after"]), (0, "12"))
        self.assertEqual(claude_usage.session()["requests"], 0)

    def test_plan_rows_from_windows(self):
        rows = claude_usage.plan_rows(PLAN["rate_limits"])
        self.assertEqual([(r["label"], r["group"], r["percent"]) for r in rows],
                         [("Current session", "session", 34.0), ("All models", "weekly", 12.5),
                          ("Fable", "weekly", 50)])
        self.assertEqual(claude_usage.extra_usage(PLAN["rate_limits"]),
                         {"used": 1230, "limit": 5000, "percent": 24.6, "currency": "USD"})

    def test_plan_rows_prefer_server_rows(self):
        rl = dict(PLAN["rate_limits"], limits=[
            {"kind": "session", "group": "session", "percent": 40, "resets_at": None, "severity": "normal",
             "is_active": True},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 90, "resets_at": None, "severity": "warning",
             "scope": {"model": {"display_name": "Opus"}}, "is_active": False}])
        rows = claude_usage.plan_rows(rl)
        self.assertEqual([(r["label"], r["percent"], r["severity"]) for r in rows],
                         [("Current session", 40, "normal"), ("Opus", 90, "warning")])

    def test_parse_probe(self):
        ok = json.dumps({"type": "control_response", "response": {"subtype": "success", "request_id": "usage",
                                                                   "response": {"a": 1}}})
        self.assertEqual(claude_usage.parse_probe("noise\n" + ok), ({"a": 1}, None))
        bad = json.dumps({"type": "control_response", "response": {
            "subtype": "error", "request_id": "usage", "error": "Unsupported control request subtype: get_usage"}})
        self.assertEqual(claude_usage.parse_probe(bad), (None, claude_usage.UPDATE_HELP))
        self.assertEqual(claude_usage.parse_probe(""), (None, None))


class CliUsageTest(unittest.TestCase):
    def setUp(self):
        claude_usage.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = dict(claude_api.DEFAULT_SETTINGS, claude_path=make_fake_claude(self.tmp.name))
        os.environ["FAKE_CLAUDE_LOG"] = os.path.join(self.tmp.name, "log.json")
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        os.environ["FAKE_CLAUDE_USAGE"] = json.dumps(PLAN)

    def tearDown(self):
        self.tmp.cleanup()
        for k in ("FAKE_CLAUDE_LOG", "FAKE_CLAUDE_MODE", "FAKE_CLAUDE_USAGE"):
            os.environ.pop(k, None)

    def test_plan_usage_through_claude_code(self):
        snap = claude_usage.cli_snapshot(self.settings)
        self.assertIsNone(snap["error"])
        self.assertEqual(snap["subscription"], "max")
        self.assertEqual(snap["plan"][0]["percent"], 34.0)
        self.assertEqual(snap["extra"]["used"], 1230)
        with open(os.environ["FAKE_CLAUDE_LOG"], encoding="utf-8") as f:
            call = json.load(f)
        # Asks without a prompt, so no model call and nothing counted against the plan.
        self.assertEqual(json.loads(call["stdin"])["request"], {"subtype": "get_usage", "skip_behaviors": True})
        self.assertEqual(call["argv"][call["argv"].index("--input-format") + 1], "stream-json")
        self.assertEqual(call["argv"][call["argv"].index("--tools") + 1], "")

    def test_older_claude_code(self):
        os.environ["FAKE_CLAUDE_MODE"] = "usage_unsupported"
        snap = claude_usage.cli_snapshot(self.settings)
        self.assertEqual((snap["plan"], snap["error"]), (None, claude_usage.UPDATE_HELP))

    def test_api_key_login_has_no_plan(self):
        os.environ["FAKE_CLAUDE_USAGE"] = json.dumps({"subscription_type": None, "rate_limits_available": False,
                                                       "rate_limits": None})
        snap = claude_usage.cli_snapshot(self.settings)
        self.assertIsNone(snap["plan"])
        self.assertIn("not a Claude subscription", snap["error"])

    def test_not_installed(self):
        settings = dict(self.settings, claude_path=os.path.join(self.tmp.name, "missing"))
        self.assertIn("isn't installed", claude_usage.cli_snapshot(settings)["error"])

    def test_answers_are_counted(self):
        claude_api.ask(self.settings, "S", "q")
        s = claude_usage.session()
        self.assertEqual((s["requests"], s["input_tokens"], s["output_tokens"]), (1, 120, 30))
        self.assertEqual(claude_usage.cli_snapshot(self.settings)["status"]["status"], "allowed")


if __name__ == "__main__":
    unittest.main()
