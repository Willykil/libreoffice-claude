import os
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import build  # noqa: E402


def contents(path):
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


class BuildTest(unittest.TestCase):
    def test_committed_oxt_matches_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh = contents(build.build(os.path.join(tmp, "fresh.oxt"), os.path.join(tmp, "update.xml")))
        committed = contents(build.OUT)
        self.assertEqual(sorted(committed), sorted(fresh), "files differ; run python3 build.py")
        stale = [n for n in fresh if committed[n] != fresh[n]]
        self.assertEqual(stale, [], "stale in the .oxt; run python3 build.py")

    def test_update_feed_matches_extension(self):
        with open(build.FEED, encoding="utf-8") as f:
            self.assertEqual(f.read(), build.feed(), "update.xml is stale; run python3 build.py")
        with zipfile.ZipFile(build.OUT) as z:
            self.assertIn('<version value="%s"/>' % build.version(), z.read("description.xml").decode())


if __name__ == "__main__":
    unittest.main()
