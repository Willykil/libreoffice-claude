"""Windows only: the full web panel inside LibreOffice's sidebar.

LibreOffice has no web view an extension can use, so this opens the panel in a chromeless
Microsoft Edge (or Chrome) "app" window, finds that window by a unique title, and makes it a
child of a native window placed in the sidebar (Win32 SetParent). If any step fails, on_fail is
called and the sidebar keeps the version made of LibreOffice's own controls.
"""

import ctypes
import secrets
import subprocess
import threading
import time
import traceback

import uno

import claude_panel

FIND_TIMEOUT = 20                   # seconds to wait for the browser window
CONNECT_TIMEOUT = 15                # seconds for the page inside it to reach LibreOffice
SYSTEM_WIN32 = 1                    # com.sun.star.lang.SystemDependent.SYSTEM_WIN32

GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CHILD, WS_VISIBLE, WS_POPUP = 0x40000000, 0x10000000, 0x80000000
WS_FRAME = 0x00C00000 | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000    # caption, sizing border, buttons
WS_EX_FRAME = 0x00040000 | 0x00000100 | 0x00000200 | 0x00000001             # taskbar button, edges
SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW, SWP_ASYNC = 0x4, 0x10, 0x20, 0x40, 0x4000
WM_CLOSE = 0x0010


def _user32():
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u.EnumWindows.argtypes = [u.WNDENUMPROC, wintypes.LPARAM]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.IsWindow.argtypes = [wintypes.HWND]
    u.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    u.SetParent.restype = wintypes.HWND
    u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [wintypes.UINT]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    # 32-bit Windows has no ...LongPtr functions; the plain ones are the same there.
    u.get_long = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
    u.set_long = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
    u.get_long.argtypes = [wintypes.HWND, ctypes.c_int]
    u.get_long.restype = ctypes.c_ssize_t
    u.set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    u.set_long.restype = ctypes.c_ssize_t
    u.RECT = wintypes.RECT
    return u


def find_window(u, title_part):
    found = []

    def check(hwnd, _):
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            if title_part in buf.value:
                found.append(hwnd)
                return False
        return True

    u.EnumWindows(u.WNDENUMPROC(check), 0)
    return found[0] if found else None


class EdgeEmbed:
    def __init__(self, ctx, parent, server, on_ready, on_fail):
        self.ctx = ctx
        self.parent = parent
        self.server = server
        self.on_ready = on_ready
        self.on_fail = on_fail
        self.ready = False
        self.closed = False
        self.host = None
        self.host_hwnd = None
        self.edge = None
        self.proc = None
        self.id = secrets.token_hex(8)
        self.u = _user32()

    def start(self):
        browser = claude_panel.find_browser()
        if not browser:
            raise RuntimeError("Microsoft Edge wasn't found")
        size = self.parent.getPosSize()
        smgr = self.ctx.ServiceManager
        toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", self.ctx)
        desc = uno.createUnoStruct("com.sun.star.awt.WindowDescriptor")
        desc.Type = uno.Enum("com.sun.star.awt.WindowClass", "SIMPLE")
        desc.WindowServiceName = "systemchildwindow"
        desc.Parent = self.parent
        desc.Bounds = uno.createUnoStruct("com.sun.star.awt.Rectangle", 0, 0, size.Width, size.Height)
        desc.WindowAttributes = 0                        # hidden until the browser is inside it
        self.host = toolkit.createWindow(desc)
        handle = self.host.getWindowHandle(uno.ByteSequence(bytes(16)), SYSTEM_WIN32)
        self.host_hwnd = int(getattr(handle, "value", handle) or 0)
        if not self.host_hwnd:
            raise RuntimeError("no native window for the sidebar")
        url = self.server.url() + "&w=" + self.id
        args = claude_panel.browser_args(self.ctx, browser, url, max(size.Width, 300), max(size.Height, 400),
                                         extra=["--window-position=-32000,-32000"])
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, close_fds=True)
        threading.Thread(target=self._attach, daemon=True).start()

    def _attach(self):
        try:
            deadline = time.time() + FIND_TIMEOUT
            while not self.closed and time.time() < deadline:
                hwnd = find_window(self.u, "Claude-panel-" + self.id)
                if hwnd:
                    self._reparent(hwnd)
                    return self._watch()
                time.sleep(0.1)
            if not self.closed:
                self._fail("Edge didn't open in time")
        except Exception as e:
            traceback.print_exc()
            self._fail(str(e) or e.__class__.__name__)

    def _reparent(self, hwnd):
        u = self.u
        self.edge = hwnd
        style = u.get_long(hwnd, GWL_STYLE)
        u.set_long(hwnd, GWL_STYLE, (style & ~(WS_POPUP | WS_FRAME)) | WS_CHILD | WS_VISIBLE)
        u.set_long(hwnd, GWL_EXSTYLE, u.get_long(hwnd, GWL_EXSTYLE) & ~WS_EX_FRAME)
        if not u.SetParent(hwnd, self.host_hwnd):
            raise RuntimeError("Windows refused to move Edge into the sidebar (error %d)" % ctypes.get_last_error())
        self.host.setVisible(True)
        self.ready = True
        self.resize()
        u.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW |
                       0x1 | 0x2)                                    # SWP_NOSIZE | SWP_NOMOVE
        self.on_ready()

    def _watch(self):
        """If the page never connects (a blank window) or the browser goes away (crash, update),
        fall back to the native version."""
        attached = time.time()
        while not self.closed and self.server.last_seen < attached:
            if time.time() - attached > CONNECT_TIMEOUT:
                return self._fail("the panel didn't load")
            time.sleep(0.2)
        while not self.closed:
            time.sleep(2)
            if not self.closed and not self.u.IsWindow(self.edge):
                self.ready = False
                self._fail("Edge closed")
                return

    def _fail(self, reason):
        self.ready = False
        if self.edge and self.u.IsWindow(self.edge):
            self.u.PostMessageW(self.edge, WM_CLOSE, 0, 0)
        self._dispose_host()
        if not self.closed:
            self.on_fail(reason)

    def resize(self, width=None, height=None):
        if not self.ready or self.closed:
            return
        size = self.parent.getPosSize()
        self.host.setPosSize(0, 0, size.Width, size.Height, 15)
        rect = self.u.RECT()
        self.u.GetClientRect(self.host_hwnd, ctypes.byref(rect))      # real pixels, whatever the scaling
        self.u.SetWindowPos(self.edge, None, 0, 0, rect.right, rect.bottom, SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNC)

    def close(self):
        self.closed = True
        self.ready = False
        if self.edge and self.u.IsWindow(self.edge):
            self.u.PostMessageW(self.edge, WM_CLOSE, 0, 0)
        elif self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        self._dispose_host()

    def _dispose_host(self):
        host, self.host = self.host, None
        if host is not None:
            try:
                host.dispose()
            except Exception:
                pass
