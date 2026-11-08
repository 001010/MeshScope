"""Tiny localhost JSON API + static file server for the UI."""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from .demo import DemoRadio
from .radio import Radio, list_ports
from .state import MeshState

log = logging.getLogger(__name__)


class App:
    def __init__(self, state: MeshState, static: Path):
        self.state = state
        self.static = static
        self.radio: Radio = Radio(state)
        self.token = secrets.token_urlsafe(16)
        self.last_poll = 0.0
        self.quit_event = threading.Event()

    def connect(self, body):
        if body.get("demo"):
            if not isinstance(self.radio, DemoRadio):
                self.radio.disconnect()
                self.radio = DemoRadio(self.state)
            self.radio.connect("DEMO")
            return {"ok": True}
        port = body.get("port")
        if not port:
            raise ValueError("No COM port selected")
        if isinstance(self.radio, DemoRadio):
            self.radio.disconnect()
            self.radio = Radio(self.state)
        self.radio.connect(port)
        return {"ok": True}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"MeshScope/{__version__}"

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        # ---- helpers
        def _json(self, obj, code=200):
            data = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            # The custom header can't be sent cross-origin without a CORS preflight, which we
            # never approve, so other web pages can't drive the radio through this server.
            return secrets.compare_digest(self.headers.get("X-MeshScope-Token", ""), app.token)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        # ---- routes
        def do_GET(self):
            u = urlparse(self.path)
            if u.path.startswith("/api/"):
                if not self._authorized():
                    return self._json({"error": "unauthorized"}, 403)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                try:
                    return self._api_get(u.path, q)
                except Exception as e:
                    log.exception("GET %s", u.path)
                    return self._json({"error": str(e)}, 400)
            return self._static(u.path)

        def do_POST(self):
            u = urlparse(self.path)
            if not self._authorized():
                return self._json({"error": "unauthorized"}, 403)
            try:
                return self._api_post(u.path, self._body())
            except Exception as e:
                log.warning("POST %s failed: %s", u.path, e)
                return self._json({"error": str(e)}, 400)

        def _api_get(self, path, q):
            st = app.state
            if path == "/api/state":
                app.last_poll = time.time()
                since = int(q.get("since", -1))
                if since == st.version:
                    st.expire_jobs()
                    if since == st.version:
                        return self._json({"unchanged": True, "version": st.version})
                return self._json(st.snapshot())
            if path == "/api/ports":
                return self._json({"ports": list_ports()})
            if path == "/api/reach":
                max_age = float(q.get("maxAgeH", 0)) * 3600 or None
                return self._json(st.reach(int(q["node"]), int(q.get("hops", st.hop_limit)),
                                           q.get("bidir", "1") == "1", max_age, q.get("mqtt") == "1"))
            if path == "/api/info":
                return self._json({"version": __version__, "dataDir": str(st.db_path.parent) if st.db_path else None})
            return self._json({"error": "not found"}, 404)

        def _api_post(self, path, body):
            st = app.state
            if path == "/api/connect":
                return self._json(app.connect(body))
            if path == "/api/disconnect":
                app.radio.disconnect()
                return self._json({"ok": True})
            if path == "/api/action":
                job = app.radio.run_action(body["kind"], int(body["node"]), body.get("hopLimit"))
                return self._json({"ok": True, "jobId": job["id"]})
            if path == "/api/forget":
                st.forget_node(int(body["node"]))
                return self._json({"ok": True})
            if path == "/api/clear":
                st.clear_history()
                return self._json({"ok": True})
            if path == "/api/quit":
                app.quit_event.set()
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            target = (app.static / path.lstrip("/")).resolve()
            if not str(target).startswith(str(app.static.resolve())) or not target.is_file():
                self.send_error(404)
                return
            data = target.read_bytes()
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if target.suffix == ".js":
                ctype = "text/javascript"  # Windows registry can map .js to text/plain
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def start_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    return srv
