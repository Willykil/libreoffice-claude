"""The <edits> block Claude ends its reply with (no LibreOffice needed)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extension", "pythonpath"))
import claude_edits  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_no_block(self):
        self.assertEqual(claude_edits.parse("Just an answer."), ("Just an answer.", [], None))

    def test_block_is_taken_out_of_the_text(self):
        text, ops, problem = claude_edits.parse(
            'J\'ai surligné [P2].\n\n<edits>\n[{"op": "format", "para": 2, "highlight": "yellow"}]\n</edits>\n')
        self.assertEqual(text, "J'ai surligné [P2].")
        self.assertEqual(ops, [{"op": "format", "para": 2, "highlight": "yellow"}])
        self.assertIsNone(problem)

    def test_fenced_json_and_several_blocks(self):
        text, ops, _ = claude_edits.parse('A <edits>```json\n[{"op": "delete", "para": 1}]\n```</edits> B'
                                          '<edits>{"edits": [{"op": "delete", "para": 2}]}</edits>')
        self.assertEqual(text, "A  B")
        self.assertEqual([o["para"] for o in ops], [1, 2])

    def test_bad_json_and_cut_off_blocks_make_no_edits(self):
        text, ops, problem = claude_edits.parse('Done.\n<edits>[{"op": "delete",]</edits>')
        self.assertEqual((text, ops), ("Done.", []))
        self.assertIn("couldn't be read", problem)
        text, ops, problem = claude_edits.parse('Done.\n<edits>[{"op": "delete", "para": 1}, {"op"')
        self.assertEqual((text, ops), ("Done.", []))
        self.assertIn("cut off", problem)

    def test_colors(self):
        self.assertEqual(claude_edits.color("yellow"), 0xFFFF00)
        self.assertEqual(claude_edits.color("#00ff00"), 0x00FF00)
        self.assertEqual(claude_edits.color("F00"), 0xFF0000)
        self.assertEqual(claude_edits.color("none"), claude_edits.NO_COLOR)
        self.assertEqual(claude_edits.color(None), claude_edits.NO_COLOR)
        with self.assertRaises(ValueError):
            claude_edits.color("sparkly")

    def test_quote_pattern_forgives_quotes_dashes_and_spaces(self):
        self.assertEqual(claude_edits._quote_pattern("l'a  b-c"),
                         "l['‘’ʼ]a\\s+b[-‐‑‒–—]c")
        self.assertEqual(claude_edits._quote_pattern("(x)"), "\\x{0028}x\\x{0029}")

    def test_summary_for_history(self):
        self.assertEqual(claude_edits.summary_for_history("Fait.", {"done": ["Bold: ¶1"], "failed": []}),
                         "Fait.\n(Edits applied: Bold: ¶1.)")


if __name__ == "__main__":
    unittest.main()
