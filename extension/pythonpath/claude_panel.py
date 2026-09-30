"""The Claude side panel: a small web page served from inside LibreOffice.

LibreOffice dialogs can only use its classic controls, so the panel is HTML
(extension/panel/) shown in a chromeless Edge/Chrome "app" window beside the
document. This module is the local server the page talks to; it reads the
selection and writes replies through the same UNO code as before.

Security: the server listens on 127.0.0.1 only, on a random port, and every
API call must carry a random per-session token (so other web pages can't drive
it) and a 127.0.0.1/localhost Host header (so DNS rebinding can't either).
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

import claude_actions
import claude_api
import claude_office

PANEL_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "panel"))
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
PANEL_WIDTH, PANEL_HEIGHT = 440, 860
SEEN_WINDOW = 4.0      # seconds: a panel that polled this recently counts as open


class PanelServer:
    def __init__(self, ctx, settings_path):
        self.ctx = ctx
        self.settings_path = settings_path
        self.token = secrets.token_urlsafe(24)
        self.last_seen = 0.0
        self.pending = None
        self._last_doc = None
        self._request = None            # (cancel Event) while a request runs
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="claude-panel", daemon=True).start()

    def url(self, action=None):
        url = "http://127.0.0.1:%d/?t=%s" % (self.port, self.token)
        return url + ("&run=" + quote(action) if action else "")

    def shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def is_open(self):
        return time.time() - self.last_seen < SEEN_WINDOW

    # ------------------------------------------------------------ documents

    def document(self):
        """The active Writer/Calc document, or the last one used while the panel has focus."""
        try:
            desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
            doc = desktop.getCurrentComponent()
            if claude_office.doc_kind(doc):
                self._last_doc = doc
                return doc
        except Exception:
            pass
        try:
            if self._last_doc is not None and claude_office.doc_kind(self._last_doc):
                return self._last_doc
        except Exception:          # closed since
            self._last_doc = None
        return None

    @staticmethod
    def _title(doc):
        try:
            title = doc.getCurrentController().getFrame().getTitle()
            return title.split(" - LibreOffice")[0].split(" — LibreOffice")[0]
        except Exception:
            return ""

    # ------------------------------------------------------------ API

    def state(self):
        self.last_seen = time.time()
        pending, self.pending = self.pending, None
        settings = claude_api.load_settings(self.settings_path)
        out = {"pending": pending, "busy": self._request is not None,
               "connection": "api" if settings.get("backend") == claude_api.API else "subscription",
               "doc": None, "quick": []}
        doc = self.document()
        if doc is not None:
            try:
                ctx = claude_office.get_context(doc)
                out["doc"] = {"kind": ctx.kind, "title": self._title(doc), "label": ctx.label,
                              "has_selection": ctx.has_selection}
            except claude_office.OfficeError as e:
                out["doc"] = {"kind": claude_office.doc_kind(doc), "title": self._title(doc),
                              "label": str(e), "has_selection": False}
            out["quick"] = [{"label": l, "prompt": p, "needs_selection": n}
                            for l, p, n in claude_actions.QUICK_ACTIONS[out["doc"]["kind"]]]
        return out

    def ask(self, body):
        instruction = (body.get("instruction") or "").strip()
        if body.get("action") in claude_actions.PROMPTS:
            instruction = claude_actions.PROMPTS[body["action"]]
        if not instruction:
            return {"error": "Type what you'd like Claude to do."}
        doc = self.document()
        if doc is None:
            return {"error": "Open a Writer document or Calc spreadsheet first."}
        try:
            context = claude_office.get_context(doc)
        except claude_office.OfficeError as e:
            return {"error": str(e)}
        settings = claude_api.load_settings(self.settings_path)
        if settings.get("backend") == claude_api.API and not claude_api.resolve_api_key(settings):
            return {"error": "Add your Anthropic API key in Settings, or switch the connection to "
                             "Claude Code to use your Claude subscription.", "open_settings": True}
        system = claude_office.system_prompt(context.kind, settings.get("extra_instructions"))
        user_text = conversation_prompt(context.text, body.get("history") or [], instruction)

        cancel = threading.Event()
        with self._lock:
            if self._request is not None:
                return {"error": "Claude is still working on the previous request."}
            self._request = cancel
        try:
            result = _run_cancellable(lambda: claude_api.ask(settings, system, user_text, cancel), cancel)
        except claude_api.Cancelled:
            return {"cancelled": True}
        except claude_api.ClaudeError as e:
            return {"error": str(e)}
        finally:
            with self._lock:
                self._request = None
        text, truncated = result
        if not text:
            return {"error": "Claude returned an empty reply."}
        grid = None
        if context.kind == claude_office.CALC:
            rows = claude_office.parse_grid(text)
            if any(len(r) > 1 for r in rows):
                grid = rows
        return {"text": text, "truncated": truncated, "kind": context.kind, "grid": grid}

    def cancel(self):
        req = self._request
        if req is not None:
            req.set()
        return {"ok": True}

    def apply(self, body):
        text = body.get("text") or ""
        mode = claude_office.REPLACE if body.get("mode") == "replace" else claude_office.INSERT_AFTER
        doc = self.document()
        if doc is None:
            return {"error": "Open a Writer document or Calc spreadsheet first."}
        try:
            claude_office.apply_result(doc, text, mode)
        except claude_office.OfficeError as e:
            return {"error": str(e)}
        return {"ok": True}

    def get_settings(self):
        s = claude_api.load_settings(self.settings_path)
        return {"backend": s["backend"], "model": s["model"], "effort": s["effort"],
                "max_tokens": s["max_tokens"], "extra_instructions": s["extra_instructions"],
                "claude_path": s["claude_path"], "has_api_key": bool(s["api_key"]),
                "models": claude_api.MODELS, "efforts": claude_api.EFFORTS}

    def set_settings(self, body):
        s = claude_api.load_settings(self.settings_path)
        for key in ("backend", "model", "effort", "extra_instructions", "claude_path"):
            if key in body:
                s[key] = str(body[key]).strip() if key != "extra_instructions" else str(body[key])
        if s["backend"] not in (claude_api.CLAUDE_CODE, claude_api.API):
            s["backend"] = claude_api.CLAUDE_CODE
        if s["effort"] not in claude_api.EFFORTS:
            s["effort"] = "medium"
        if "max_tokens" in body:
            try:
                s["max_tokens"] = max(256, min(128000, int(body["max_tokens"])))
            except (TypeError, ValueError):
                pass
        if body.get("api_key"):
            s["api_key"] = str(body["api_key"]).strip()
        if body.get("clear_api_key"):
            s["api_key"] = ""
        claude_api.save_settings(self.settings_path, s)
        return self.get_settings()


def conversation_prompt(context_text, history, instruction):
    """One prompt carrying the document context, earlier turns, and the new request."""
    if not history:
        return "%s\n\n%s" % (context_text, instruction)
    turns = []
    for turn in history[-20:]:
        who = "User" if turn.get("role") == "user" else "Claude"
        turns.append("%s: %s" % (who, turn.get("text", "")))
    return ("%s\n\nOur conversation so far:\n<conversation>\n%s\n</conversation>\n\nNew request: %s"
            % (context_text, "\n\n".join(turns), instruction))


def _run_cancellable(fn, cancel):
    """Run fn on a worker thread; raise Cancelled as soon as `cancel` is set."""
    result = {}

    def work():
        try:
            result["value"] = fn()
        except BaseException as e:
            result["error"] = e

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    while worker.is_alive():
        if cancel.wait(0.1):
            raise claude_api.Cancelled()
    if "error" in result:
        raise result["error"]
    return result["value"]


def _handler(panel):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _allowed_host(self):
            host = (self.headers.get("Host") or "").lower()
            return host in ("127.0.0.1:%d" % panel.port, "localhost:%d" % panel.port)

        def _send(self, status, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._allowed_host():
                return self._send(403, {"error": "forbidden"})
            path = urlparse(self.path).path
            if path in STATIC:
                name, ctype = STATIC[path]
                with open(os.path.join(PANEL_DIR, name), "rb") as f:
                    return self._send(200, f.read(), ctype)
            if not self._authorized():
                return self._send(403, {"error": "forbidden"})
            if path == "/api/state":
                return self._send(200, panel.state())
            if path == "/api/settings":
                return self._send(200, panel.get_settings())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._allowed_host() or not self._authorized():
                return self._send(403, {"error": "forbidden"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "bad request"})
            routes = {"/api/ask": panel.ask, "/api/apply": panel.apply, "/api/settings": panel.set_settings,
                      "/api/cancel": lambda b: panel.cancel()}
            fn = routes.get(urlparse(self.path).path)
            if fn is None:
                return self._send(404, {"error": "not found"})
            try:
                self._send(200, fn(body))
            except Exception as e:     # never leave the panel hanging
                self._send(500, {"error": "Unexpected error: %s" % e})

        def _authorized(self):
            return secrets.compare_digest(self.headers.get("X-Claude-Token") or "", panel.token)

    return Handler


# ---------------------------------------------------------------- the window

_server = None
_server_lock = threading.Lock()


def get_server(ctx):
    global _server
    with _server_lock:
        if _server is None:
            _server = PanelServer(ctx, claude_actions.settings_path(ctx))
        return _server


def open_panel(ctx, action=None):
    """Show the panel (launching it if needed) and optionally run a quick action in it."""
    server = get_server(ctx)
    if server.is_open():
        server.pending = action or "focus"
        return server
    url = server.url(action)
    hook = os.environ.get("CLAUDE_LO_PANEL_URL_FILE")      # used by the tests
    if hook:
        with open(hook, "w") as f:
            f.write(url)
    launch_window(ctx, url)
    return server


def find_browser():
    override = os.environ.get("CLAUDE_LO_BROWSER")
    if override:
        return None if override == "none" else override
    if sys.platform == "win32":
        roots = [os.environ.get(k, "") for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
        candidates = [os.path.join(root, rel) for rel in rels for root in roots if root]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                      "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    else:
        candidates = [shutil.which(n) or "" for n in ("microsoft-edge", "google-chrome", "chromium",
                                                       "chromium-browser")]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def launch_window(ctx, url):
    if os.environ.get("CLAUDE_LO_BROWSER") == "none":
        return
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return
    x, y, height = dock(ctx)
    profile = os.path.join(os.path.dirname(claude_actions.settings_path(ctx)), "claude-panel-browser")
    args = [browser, "--app=" + url, "--window-size=%d,%d" % (PANEL_WIDTH, height),
            "--window-position=%d,%d" % (x, y), "--user-data-dir=" + profile,
            "--no-first-run", "--no-default-browser-check"]
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)


def _dpi_scale():
    """Windows display scaling: LibreOffice works in pixels, the browser in scaled units."""
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes
        return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


def dock(ctx):
    """Make room for the panel: shrink the LibreOffice window to the left of it, like a docked
    task pane. Returns the panel's (x, y, height) in browser units."""
    try:
        smgr = ctx.ServiceManager
        area = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx).getWorkArea()
    except Exception:
        return 0, 0, PANEL_HEIGHT
    scale = _dpi_scale()
    panel_px = int(PANEL_WIDTH * scale)
    try:
        desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        window = desktop.getCurrentFrame().getContainerWindow()
        try:
            window.IsMaximized = False
        except Exception:
            pass
        window.setPosSize(area.X, area.Y, area.Width - panel_px, area.Height, 15)   # PosSize.POSSIZE
    except Exception:
        pass
    return (int((area.X + area.Width - panel_px) / scale), int(area.Y / scale), int(area.Height / scale))
