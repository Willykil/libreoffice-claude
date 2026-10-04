"""The embedded Edge window follows sidebar resizes without falling behind (claude_embed.py).

The real thing needs Windows; these drive the resize logic with a stand-in for user32."""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extension", "pythonpath"))
import claude_embed  # noqa: E402


class FakeUser32:
    def __init__(self, slow=0):
        self.width = self.height = 0
        self.moves = []
        self.slow = slow

    def RECT(self):
        return mock.Mock(right=0, bottom=0)

    def GetClientRect(self, hwnd, ref):
        rect = ref._obj
        rect.right, rect.bottom = self.width, self.height

    def SetWindowPos(self, hwnd, after, x, y, w, h, flags):
        time.sleep(self.slow)              # Edge taking its time to lay out and paint
        self.moves.append((w, h))


class Host:
    def setPosSize(self, *args):
        pass


class Parent:
    def getPosSize(self):
        return mock.Mock(Width=0, Height=0)


def embed(u):
    with mock.patch.object(claude_embed, "_user32", lambda: u), \
         mock.patch.object(claude_embed.ctypes, "byref", lambda o: mock.Mock(_obj=o)):
        e = claude_embed.EdgeEmbed(None, Parent(), None, None, None)
    e.host, e.edge, e.ready, e.top = Host(), 1, True, 30
    return e


class ResizeTest(unittest.TestCase):
    def run_resizes(self, u, sizes):
        e = embed(u)
        thread = threading.Thread(target=e._resizer, daemon=True)
        thread.start()
        with mock.patch.object(claude_embed.ctypes, "byref", lambda o: mock.Mock(_obj=o)):
            for w in sizes:
                u.width, u.height = w, 700
                e.resize()
        time.sleep(0.3)
        e.closed = True
        e._size_event.set()
        thread.join(1)
        return u.moves

    def test_a_drag_ends_at_the_final_size_without_a_backlog(self):
        u = FakeUser32(slow=0.02)
        moves = self.run_resizes(u, range(300, 700))        # 400 resize events, faster than Edge keeps up
        self.assertEqual(moves[-1], (699, 730))             # Edge ends at the last size, title bar cut off
        self.assertLess(len(moves), 50)                     # in-between sizes are skipped, not queued

    def test_an_unchanged_size_is_not_sent_again(self):
        u = FakeUser32()
        self.assertEqual(self.run_resizes(u, [400, 400, 400]), [(400, 730)])


if __name__ == "__main__":
    unittest.main()
