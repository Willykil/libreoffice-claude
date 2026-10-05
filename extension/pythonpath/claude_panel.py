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

import base64
import tempfile

import claude_actions
import claude_api
import claude_edits
import claude_office
import claude_voice

PANEL_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "panel"))
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/diff.js": ("diff.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
PANEL_WIDTH, PANEL_HEIGHT = 440, 860
SEEN_WINDOW = 4.0      # seconds: a panel that polled this recently counts as open
MAX_BODY = 32 * 1024 * 1024     # a 20 MB document, base64-encoded, plus room
MAX_IMAGES = 5                  # screenshots sent with one request
MAX_IMAGE_BYTES = 5 * 1024 * 1024       # the API's limit for one image
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
IMAGES_ONLY = "Look at the attached image and help with what it shows, in the context of this file."


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
        self._state_lock = threading.Lock()
        self._last_state = None
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
        """The active Writer/Calc/Impress/Draw document, or the last one used while the panel has focus."""
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
        # One at a time: a slow read must not stack up behind the next poll.
        if not self._state_lock.acquire(blocking=False):
            return dict(self._last_state or {}, pending=None)
        try:
            self._last_state = self._state()
            return self._last_state
        finally:
            self._state_lock.release()

    def _state(self):
        pending, self.pending = self.pending, None
        settings = claude_api.load_settings(self.settings_path)
        model = settings.get("model") or ""
        out = {"pending": pending, "busy": self._request is not None,
               "connection": "api" if settings.get("backend") == claude_api.API else "subscription",
               "doc": None, "quick": [],
               "model": model, "effort": settings.get("effort") or "medium",
               "effort_supported": claude_api.supports_effort(model),
               "track_changes": bool(settings.get("track_changes")),
               "models": [{"id": m, "label": claude_api.MODEL_LABELS.get(m, m)} for m in claude_api.MODELS],
               "efforts": [{"id": e, "label": claude_api.EFFORT_LABELS[e]} for e in claude_api.EFFORTS]}
        if model and model not in claude_api.MODELS:
            out["models"].append({"id": model, "label": model})
        voice = claude_voice.summary(claude_voice.load(self._voice_path()))
        out["voice"] = {"ready": voice["ready"], "default": voice["default"], "samples": len(voice["samples"])}
        doc = self.document()
        if doc is not None:
            # Polled every couple of seconds while LibreOffice is in use, so only cheap reads here:
            # reading the whole document each time froze LibreOffice on long documents.
            kind = claude_office.doc_kind(doc)
            try:
                label, has_selection = claude_office.selection_summary(doc)
            except Exception:
                label, has_selection = "", False
            out["doc"] = {"kind": kind, "title": self._title(doc), "label": label,
                          "has_selection": has_selection}
            out["doc"]["writable"] = out["doc"]["kind"] in claude_office.WRITABLE
            out["quick"] = [{"label": l, "prompt": p, "needs_selection": n,
                             "hint": claude_actions.QUICK_HINTS.get(l, "")}
                            for l, p, n in claude_actions.QUICK_ACTIONS[out["doc"]["kind"]]]
        return out

    def ask(self, body, doc=None):
        instruction = (body.get("instruction") or "").strip()
        images, problem = check_images(body.get("images"))
        if problem:
            return {"error": problem}
        if images and not instruction and not body.get("action") and not body.get("tone"):
            instruction = IMAGES_ONLY
        if body.get("action") in claude_actions.PROMPTS:
            instruction = claude_actions.PROMPTS[body["action"]]
        tone = body.get("tone")
        if tone == "formal" and not instruction:
            instruction = claude_voice.FORMAL_REWRITE
        voice = None
        if tone == "voice":
            voice = claude_voice.load(self._voice_path())
            if not voice["profile"].strip():
                return {"error": "Claude doesn't know your writing voice yet. Add a few things you wrote, "
                                 "then let it learn.", "open_voice": True}
            if not instruction:
                instruction = claude_voice.VOICE_REWRITE
        if not instruction:
            return {"error": "Type what you'd like Claude to do."}
        doc = doc or self.document()
        if doc is None:
            return {"error": "Open a document, spreadsheet or presentation first."}
        try:
            context = claude_office.get_context(doc)
        except claude_office.OfficeError as e:
            return {"error": str(e)}
        settings = claude_api.load_settings(self.settings_path)
        if settings.get("backend") == claude_api.API and not claude_api.resolve_api_key(settings):
            return {"error": "Add your Anthropic API key in Settings, or switch the connection to "
                             "Claude Code to use your Claude subscription.", "open_settings": True}
        system = claude_office.system_prompt(context.kind, settings.get("instructions_" + context.kind))
        if voice is not None:
            system += claude_voice.system_addition(voice)
        user_text = conversation_prompt(context.text, body.get("history") or [], instruction)
        # The paragraphs as Claude sees them, so its [P12] edits land there even if the user types meanwhile.
        paragraphs = claude_office.writer_paragraphs(doc) if context.kind == claude_office.WRITER else None

        result = self._claude(settings, system, user_text, images)
        if isinstance(result, dict):
            return result
        text, truncated = result
        if not text:
            return {"error": "Claude returned an empty reply."}
        text, ops, problem = claude_edits.parse(text)
        if ops or problem:
            edits = {"done": [], "failed": [problem] if problem else []}
            if ops:
                try:
                    result = claude_edits.apply(doc, ops, settings.get("track_changes"), paragraphs)
                except Exception as e:      # the document was closed meanwhile, say
                    result = {"done": [], "failed": ["Couldn't edit the document: %s" % e]}
                edits["done"] += result["done"]
                edits["failed"] += result["failed"]
            edits["tracked"] = bool(edits["done"] and settings.get("track_changes")
                                    and context.kind == claude_office.WRITER)
            return {"text": text or ("Done." if edits["done"] else ""), "truncated": truncated,
                    "kind": context.kind, "grid": None, "edits": edits, "selection": "",
                    "history_text": claude_edits.summary_for_history(text, edits),
                    "voice": voice is not None, "voice_samples": len(voice["samples"]) if voice else 0}
        grid = None
        if context.kind == claude_office.CALC:
            rows = claude_office.parse_grid(text)
            if any(len(r) > 1 for r in rows):
                grid = rows
        return {"text": text, "truncated": truncated, "kind": context.kind, "grid": grid,
                "selection": context.selection, "paragraph": context.paragraph,
                "before": context.before, "after": context.after,
                "voice": voice is not None, "voice_samples": len(voice["samples"]) if voice else 0}

    def _claude(self, settings, system, user_text, images=None):
        """Run one Claude request (one at a time; Stop cancels it). (text, truncated) or an error dict."""
        cancel = threading.Event()
        with self._lock:
            if self._request is not None:
                return {"error": "Claude is still working on the previous request."}
            self._request = cancel
        try:
            return _run_cancellable(lambda: claude_api.ask(settings, system, user_text, cancel, images), cancel)
        except claude_api.Cancelled:
            return {"cancelled": True}
        except claude_api.ClaudeError as e:
            return {"error": str(e)}
        finally:
            with self._lock:
                self._request = None

    # ------------------------------------------------------------ My voice

    MAX_UPLOAD = 20 * 1024 * 1024
    TEXT_FILES = (".txt", ".md")
    OFFICE_FILES = (".odt", ".docx", ".doc", ".rtf", ".fodt", ".ott", ".dotx")

    def _voice_path(self):
        return claude_voice.path_for(self.settings_path)

    def voice(self, body):
        path = self._voice_path()
        if body.get("learn"):
            return self._learn_voice(path)
        with self._lock:
            data = claude_voice.load(path)
        error = None
        if body.get("add") in ("selection", "document"):
            doc = self.document()
            if doc is None or claude_office.doc_kind(doc) != claude_office.WRITER:
                return {"error": "Open a Writer document first."}
            title = self._title(doc) or "Untitled document"
            if body["add"] == "selection":
                text, name = claude_office.selected_text(doc), "Selection from %s" % title
            else:
                text, name = claude_office.document_text(doc), title
            _, error = claude_voice.add_sample(data, name, body["add"], text)
        elif body.get("add") == "file":
            name = os.path.basename(str(body.get("name") or "document"))
            try:
                raw = base64.b64decode(body.get("data") or "", validate=True)
            except ValueError:
                return {"error": "Couldn't read that file."}
            if len(raw) > self.MAX_UPLOAD:
                return {"error": "That file is over 20 MB."}
            text, error = self._file_text(name, raw)
            if text is not None:
                _, error = claude_voice.add_sample(data, name, "file", text)
        elif body.get("remove"):
            claude_voice.remove_sample(data, body["remove"])
        if "profile" in body:
            data["profile"] = str(body["profile"])
        if "default" in body:
            data["default"] = bool(body["default"])
        with self._lock:
            claude_voice.save(path, data)
        out = claude_voice.summary(data)
        if error:
            out["error"] = error
        return out

    def _learn_voice(self, path):
        data = claude_voice.load(path)
        if not data["samples"]:
            return dict(claude_voice.summary(data), error="Add some of your own writing first.")
        settings = claude_api.load_settings(self.settings_path)
        system, user_text = claude_voice.learn_request(data)
        result = self._claude(settings, system, user_text)
        if isinstance(result, dict):
            return dict(claude_voice.summary(data), **result)
        claude_voice.finish_learning(data, result[0])
        with self._lock:
            claude_voice.save(path, data)
        return claude_voice.summary(data)

    def _file_text(self, name, raw):
        """(text, None) or (None, reason): plain text files directly, documents through LibreOffice."""
        ext = os.path.splitext(name)[1].lower()
        if ext in self.TEXT_FILES:
            return raw.decode("utf-8", "replace"), None
        if ext not in self.OFFICE_FILES:
            return None, "Add a text document: .odt, .docx, .doc, .rtf or .txt."
        fd, tmp = tempfile.mkstemp(suffix=ext, prefix="claude-voice-")
        doc = None
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            import uno
            from com.sun.star.beans import PropertyValue
            hidden = PropertyValue()
            hidden.Name, hidden.Value = "Hidden", True
            desktop = self.ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
            doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(tmp), "_blank", 0, (hidden,))
            if claude_office.doc_kind(doc) != claude_office.WRITER:
                return None, "That isn't a text document."
            return claude_office.document_text(doc), None
        except Exception:
            return None, "LibreOffice couldn't open %s." % name
        finally:
            if doc is not None:
                try:
                    doc.close(True)
                except Exception:
                    pass
            try:
                os.remove(tmp)
            except OSError:
                pass

    def cancel(self):
        req = self._request
        if req is not None:
            req.set()
        return {"ok": True}

    def apply(self, body, doc=None):
        text = body.get("text") or ""
        mode = claude_office.REPLACE if body.get("mode") == "replace" else claude_office.INSERT_AFTER
        doc = doc or self.document()
        if doc is None:
            return {"error": "Open a document, spreadsheet or presentation first."}
        settings = claude_api.load_settings(self.settings_path)
        try:
            if claude_office.doc_kind(doc) == claude_office.CALC and not body.get("confirm"):
                count, where = claude_office.calc_overwrites(doc, text, mode)
                if count:
                    return {"confirm": "This replaces %d cell%s that already %s content (%s)."
                                       % (count, "s" if count > 1 else "", "have" if count > 1 else "has", where)}
            claude_office.apply_result(doc, text, mode, track_changes=settings.get("track_changes"))
        except claude_office.OfficeError as e:
            return {"error": str(e)}
        return {"ok": True, "tracked": bool(settings.get("track_changes"))
                and claude_office.doc_kind(doc) == claude_office.WRITER}

    def undo(self, body):
        doc = self.document()
        if doc is None:
            return {"error": "Open a document, spreadsheet or presentation first."}
        try:
            claude_edits.undo_last(doc)
        except claude_edits.EditError as e:
            return {"error": str(e)}
        return {"ok": True}

    def goto(self, body):
        doc = self.document()
        if doc is None:
            return {"error": "Open a document, spreadsheet or presentation first."}
        try:
            claude_office.goto(doc, body.get("ref"))
        except claude_office.OfficeError as e:
            return {"error": str(e)}
        except Exception:
            return {"error": "Couldn't find %s in the document." % body.get("ref")}
        return {"ok": True}

    # Settings the panel edits. Model, effort and tracked changes also change from the composer.
    _TEXT_KEYS = ("backend", "model", "effort", "claude_path")
    _FREE_TEXT_KEYS = ("instructions_writer", "instructions_calc", "instructions_impress", "instructions_draw")

    def get_settings(self):
        s = claude_api.load_settings(self.settings_path)
        out = {k: s[k] for k in self._TEXT_KEYS + self._FREE_TEXT_KEYS}
        out.update({"max_tokens": s["max_tokens"], "track_changes": bool(s["track_changes"]),
                    "native_sidebar": bool(s["native_sidebar"]), "windows": sys.platform == "win32",
                    "has_api_key": bool(s["api_key"]), "models": claude_api.MODELS,
                    "efforts": claude_api.EFFORTS})
        return out

    def set_settings(self, body):
        s = claude_api.load_settings(self.settings_path)
        for key in self._TEXT_KEYS:
            if key in body:
                s[key] = str(body[key]).strip()
        for key in self._FREE_TEXT_KEYS:
            if key in body:
                s[key] = str(body[key])
        for key in ("track_changes", "native_sidebar"):
            if key in body:
                s[key] = bool(body[key])
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

    # Chat history lives next to the settings, on this computer only, like the M365 add-ins
    # keep theirs in the browser. The panel's own storage can't be used: its port changes
    # every session, and with it the page's origin.
    MAX_CONVERSATIONS = 50

    def _history_path(self):
        return os.path.join(os.path.dirname(self.settings_path), "claude-panel-history.json")

    def _load_history(self):
        try:
            with open(self._history_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self, items):
        path = self._history_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items[:self.MAX_CONVERSATIONS], f)
        os.replace(tmp, path)

    def history(self, body):
        with self._lock:
            items = self._load_history()
            if body.get("clear"):
                items = []
            elif body.get("delete"):
                items = [c for c in items if c.get("id") != body["delete"]]
            elif isinstance(body.get("save"), dict) and body["save"].get("id"):
                convo = body["save"]
                convo["updated"] = time.time()
                items = [convo] + [c for c in items if c.get("id") != convo["id"]]
            else:
                return {"conversations": items}
            self._save_history(items)
            return {"conversations": items}


def check_images(images):
    """(images, problem) for the pictures sent with a request: at most MAX_IMAGES, each a supported
    type and within the API's size limit."""
    if not images:
        return [], None
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        return [], "Attach up to %d images at a time." % MAX_IMAGES
    out = []
    for i in images:
        if not isinstance(i, dict) or i.get("media_type") not in IMAGE_TYPES or not isinstance(i.get("data"), str):
            return [], "That image type isn't supported. Use PNG, JPEG, GIF or WebP."
        if len(i["data"]) * 3 // 4 > MAX_IMAGE_BYTES:
            return [], "One of the images is larger than 5 MB."
        out.append({"media_type": i["media_type"], "data": i["data"]})
    return out, None


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
                if length > MAX_BODY:
                    return self._send(413, {"error": "That's too large to send."})
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "bad request"})
            routes = {"/api/ask": panel.ask, "/api/apply": panel.apply, "/api/settings": panel.set_settings,
                      "/api/cancel": lambda b: panel.cancel(), "/api/goto": panel.goto, "/api/undo": panel.undo,
                      "/api/history": panel.history, "/api/voice": panel.voice}
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
    """The panel in its own window. Never moves or resizes LibreOffice (an earlier version did, and
    with several screens or display scaling it threw LibreOffice onto the wrong screen)."""
    if os.environ.get("CLAUDE_LO_BROWSER") == "none":
        return None
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return None
    return subprocess.Popen(browser_args(ctx, browser, url, PANEL_WIDTH, PANEL_HEIGHT),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            close_fds=True)


def browser_args(ctx, browser, url, width, height, extra=()):
    profile = os.path.join(os.path.dirname(claude_actions.settings_path(ctx)), "claude-panel-browser")
    return [browser, "--app=" + url, "--window-size=%d,%d" % (width, height), "--user-data-dir=" + profile,
            "--no-first-run", "--no-default-browser-check"] + list(extra)
