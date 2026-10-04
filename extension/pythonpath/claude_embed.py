"""Windows only: the full web panel inside LibreOffice's sidebar.

LibreOffice has no web view an extension can use, so this opens the panel in a chromeless
Microsoft Edge (or Chrome) "app" window, finds that window by a unique title, and makes it a
child of a native window placed in the sidebar (Win32 SetParent). If any step fails, on_fail is
called and the sidebar keeps the version made of LibreOffice's own controls.

Two things a reparented browser doesn't do by itself:
- hide its own title bar: Edge draws it inside its window, so the window is placed that much
  higher than the sidebar and the title bar is cut off;
- take the keyboard: a child window never gets activated, so on a click inside it the keyboard
  focus is moved there explicitly (otherwise typing goes to the document).
"""

import ctypes
import re
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
SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_SHOWWINDOW = 0x4, 0x10, 0x20, 0x40
FRAME = 1 / 60                                  # seconds between resizes passed on to Edge
PANEL_LIGHT, PANEL_DARK = 0xFAF9F5, 0x1F1E1D    # the panel's background (style.css --bg)
WM_CLOSE = 0x0010
WM_ACTIVATE, WA_CLICKACTIVE = 0x0006, 2
VK_LBUTTON, VK_RBUTTON = 0x01, 0x02


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
    u.EnumChildWindows.argtypes = [wintypes.HWND, u.WNDENUMPROC, wintypes.LPARAM]
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetAsyncKeyState.argtypes = [ctypes.c_int]
    u.GetAsyncKeyState.restype = ctypes.c_short
    u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u.WindowFromPoint.argtypes = [wintypes.POINT]
    u.WindowFromPoint.restype = wintypes.HWND
    u.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.SetFocus.argtypes = [wintypes.HWND]
    u.SetFocus.restype = wintypes.HWND
    u.GetFocus.restype = wintypes.HWND
    u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                      wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
    u.RECT, u.POINT = wintypes.RECT, wintypes.POINT
    u.thread_id = ctypes.WinDLL("kernel32").GetCurrentThreadId
    return u


def _dark_mode():
    """Whether Windows apps use the dark theme, which Edge, and so the panel, follows."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def find_child(u, parent, class_name):
    found = []

    def check(hwnd, _):
        buf = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, buf, 64)
        if buf.value == class_name:
            found.append(hwnd)
            return False
        return True

    u.EnumChildWindows(parent, u.WNDENUMPROC(check), 0)
    return found[0] if found else None


def window_title(u, hwnd):
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


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
        self.top = 0                    # height of Edge's own title bar, cut off above the sidebar
        self.proc = None
        self.id = secrets.token_hex(8)
        self.u = _user32()
        self._size_wanted = None        # (width, height) Edge should be, set by resize()
        self._size_done = None
        self._size_event = threading.Event()

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
        try:
            # What shows for a moment where the sidebar grows, before Edge repaints: the panel's own
            # background rather than a contrasting flash.
            self.host.setBackground(PANEL_DARK if _dark_mode() else PANEL_LIGHT)
        except Exception:
            pass
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
        # The page puts its title bar height in its title (claude-panel-<id>-<pixels>), measured
        # while it was still a normal window.
        m = re.search(r"Claude-panel-%s-(\d+)" % self.id, window_title(u, hwnd))
        self.top = min(int(m.group(1)), 120) if m else 0
        style = u.get_long(hwnd, GWL_STYLE)
        u.set_long(hwnd, GWL_STYLE, (style & ~(WS_POPUP | WS_FRAME)) | WS_CHILD | WS_VISIBLE)
        u.set_long(hwnd, GWL_EXSTYLE, u.get_long(hwnd, GWL_EXSTYLE) & ~WS_EX_FRAME)
        if not u.SetParent(hwnd, self.host_hwnd):
            raise RuntimeError("Windows refused to move Edge into the sidebar (error %d)" % ctypes.get_last_error())
        self.host.setVisible(True)
        self.ready = True
        u.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW |
                       0x1 | 0x2)                                    # SWP_NOSIZE | SWP_NOMOVE
        self._measure_title_bar()
        threading.Thread(target=self._resizer, daemon=True).start()
        self.resize()
        self.on_ready()

    def _measure_title_bar(self):
        """Where the page starts inside Edge's window: the top of its page area (a child window
        Chromium keeps for accessibility). Falls back to what the page measured."""
        u = self.u
        page = find_child(u, self.edge, "Chrome_RenderWidgetHostHWND")
        if not page:
            return
        outer, inner = u.RECT(), u.RECT()
        u.GetWindowRect(self.edge, ctypes.byref(outer))
        u.GetWindowRect(page, ctypes.byref(inner))
        if 0 <= inner.top - outer.top <= 120:
            self.top = inner.top - outer.top

    def _watch(self):
        """If the page never connects (a blank window) or the browser goes away (crash, update),
        fall back to the native version."""
        attached = time.time()
        while not self.closed and self.server.last_seen < attached:
            if time.time() - attached > CONNECT_TIMEOUT:
                return self._fail("the panel didn't load")
            time.sleep(0.2)
        was_down, ticks, active = False, 0, False
        while not self.closed:
            time.sleep(0.03)
            ticks += 1
            down = bool((self.u.GetAsyncKeyState(VK_LBUTTON) | self.u.GetAsyncKeyState(VK_RBUTTON)) & 0x8000)
            if down and not was_down:
                if self._under_mouse():
                    self._take_focus()
                    active = True
                elif active:                                 # clicked back into the document
                    self.u.SendMessageTimeoutW(self.edge, WM_ACTIVATE, 0, 0, 0x2, 200, None)
                    active = False
            was_down = down
            if ticks % 60 == 0 and not self.closed and not self.u.IsWindow(self.edge):
                self.ready = False
                self._fail("Edge closed")
                return

    def _under_mouse(self):
        pt = self.u.POINT()
        self.u.GetCursorPos(ctypes.byref(pt))
        hit = self.u.WindowFromPoint(pt)
        return bool(hit) and (hit == self.edge or bool(self.u.IsChild(self.edge, hit)))

    def _take_focus(self):
        """Give Edge the keyboard. LibreOffice may take it back while handling the same click, so
        check again for a moment."""
        u = self.u
        me, edge_thread = u.thread_id(), u.GetWindowThreadProcessId(self.edge, None)
        if not edge_thread:
            return
        u.AttachThreadInput(me, edge_thread, True)       # SetFocus only works within one input queue
        try:
            # Chromium routes keys only while it believes its window is active.
            u.SendMessageTimeoutW(self.edge, WM_ACTIVATE, WA_CLICKACTIVE, 0, 0x2, 200, None)   # SMTO_ABORTIFHUNG
            for _ in range(10):
                focus = u.GetFocus()
                if not (focus and (focus == self.edge or u.IsChild(self.edge, focus))):
                    u.SetFocus(self.edge)
                time.sleep(0.03)
        finally:
            u.AttachThreadInput(me, edge_thread, False)

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
        self._size_wanted = (rect.right, rect.bottom)
        self._size_event.set()

    def _resizer(self):
        """Pass sizes on to Edge at most once a frame, newest only. Dragging the sidebar edge sends a
        flood of resizes; queueing each one made Edge lay out and paint sizes long gone, so it lagged."""
        while not self.closed:
            self._size_event.wait()
            self._size_event.clear()
            want = self._size_wanted
            if self.closed or not self.ready or want is None or want == self._size_done:
                continue
            self._size_done = want
            # Not async: this thread waits until Edge has taken the size, so it is never handed a
            # backlog. LibreOffice's own thread doesn't wait; it only records the latest size.
            self.u.SetWindowPos(self.edge, None, 0, -self.top, want[0], want[1] + self.top,
                                SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(FRAME)

    def close(self):
        self.closed = True
        self.ready = False
        self._size_event.set()                       # let the resizer thread finish
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
