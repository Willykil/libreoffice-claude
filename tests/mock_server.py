"""A tiny stand-in for the Messages API that records requests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockClaude:
    def __init__(self):
        self.requests = []
        self.reply = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        self.status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                payload = json.dumps(outer.reply).encode()
                self.send_response(outer.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def text(self, text, stop_reason="end_turn"):
        self.status = 200
        self.reply = {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
                      "stop_reason": stop_reason}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
