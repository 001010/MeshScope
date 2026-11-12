"""Tiny localhost JSON API + static file server for the UI."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import __version__
from .core import App  # noqa: F401  (re-exported for callers/tests)
from .radio import list_ports
from .terrain import ENVIRONMENTS, PRESETS

log = logging.getLogger(__name__)

MAX_BODY = 32 * 1024 * 1024


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"MeshScope/{__version__}"

        def log_message(self, fmt, *args):
            log.debug("http: " + fmt, *args)

        # ---- helpers
        def _json(self, obj, code=200):
            data = json.dumps(obj, default=_json_default).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _bytes(self, data: bytes, ctype: str, filename: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            # The custom header can't be sent cross-origin without a CORS preflight, which we
            # never approve, so other web pages can't drive the radio through this server.
            return secrets.compare_digest(self.headers.get("X-MeshScope-Token", ""), app.token)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("Request too large")
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
                except (RuntimeError, ValueError, KeyError, FileNotFoundError) as e:
                    log.warning("GET %s failed: %s", u.path, e)
                    return self._json({"error": str(e) or e.__class__.__name__}, 400)
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
                return self._json({"error": str(e) or e.__class__.__name__}, 400)

        def _api_get(self, path, q):
            st = app.state
            if path == "/api/state":
                app.last_poll = time.time()
                since = int(q.get("since", -1))
                if since == st.version:
                    st.expire_jobs()
                    if since == st.version:
                        return self._json({"unchanged": True, "version": st.version})
                snap = st.snapshot()
                snap["inDemo"] = app.in_demo
                snap["mqttStatus"] = app.mqtt.status
                snap["addonsVersion"] = app.addons.version
                return self._json(snap)
            if path == "/api/ports":
                return self._json({"ports": list_ports()})
            if path == "/api/reach":
                max_age = float(q.get("maxAgeH", 0)) * 3600 or None
                return self._json(st.reach(int(q["node"]), int(q.get("hops", st.hop_limit)),
                                           q.get("bidir", "1") == "1", max_age, q.get("mqtt") == "1"))
            if path == "/api/info":
                return self._json({"version": __version__, "dataDir": str(app.workdir),
                                   "presets": PRESETS, "environments": list(ENVIRONMENTS)})
            if path == "/api/mynodes":
                return self._json({"myNodes": app.my_nodes(), "ports": list_ports()})
            if path == "/api/console":
                return self._json(app.console.since(int(q.get("since", 0))))
            if path == "/api/addons":
                return self._json(app.addons.list())
            if path == "/api/addon":
                return self._json(app.addons.detail(q["slug"], int(q.get("since", 0))))
            if path == "/api/addons/export":
                slugs = [s for s in q.get("slugs", "").split(",") if s]
                name, ctype, data = app.addons.export(slugs)
                return self._bytes(data, ctype, name)
            if path == "/api/mqtt":
                return self._json(app.mqtt.info())
            if path == "/api/config":
                return self._json(app.config.schema())
            if path == "/api/config/backups":
                return self._json({"backups": app.config.list_backups(), "dir": str(app.config.dir)})
            if path == "/api/config/backup/download":
                name = q["file"]
                return self._bytes(app.config.read(name), "application/x-yaml", name)
            return self._json({"error": "not found"}, 404)

        def _api_post(self, path, body):
            st = app.state
            # -- connection
            if path == "/api/connect":
                return self._json(app.connect(body))
            if path == "/api/disconnect":
                app.disconnect()
                return self._json({"ok": True})
            # -- node actions / data
            if path == "/api/action":
                job = app.radio.run_action(body["kind"], int(body["node"]), body.get("hopLimit"))
                return self._json({"ok": True, "jobId": job["id"]})
            if path == "/api/forget":
                st.forget_node(int(body["node"]))
                return self._json({"ok": True})
            if path == "/api/favorite":
                st.set_favorite(int(body["node"]), bool(body.get("favorite", True)))
                return self._json({"ok": True})
            if path == "/api/ui-settings":
                patch = {}
                if body.get("theme") in ("system", "day", "night"):
                    patch["theme"] = body["theme"]
                if body.get("units") in ("metric", "imperial"):
                    patch["units"] = body["units"]
                if not patch:
                    raise ValueError("theme must be system/day/night and units metric/imperial")
                app.real_state.update_settings({"ui": patch})
                app.real_state.save(force=True)
                if app.state is not app.real_state:
                    app.state.update_settings({})  # demo shares the settings; bump its version so the UI refreshes
                return self._json({"ok": True, "ui": app.real_state.settings["ui"]})
            if path == "/api/node-settings":
                h = body.get("antennaHeight")
                st.merge_node(int(body["node"]), antennaHeight=None if h in (None, "") else max(0.1, float(h)))
                return self._json({"ok": True})
            if path == "/api/clear":
                st.clear_history()
                return self._json({"ok": True})
            if path == "/api/quit":
                app.quit_event.set()
                return self._json({"ok": True})
            # -- my nodes
            if path == "/api/mynodes/update":
                num = int(body["num"])
                if "nickname" in body:
                    app.real_state.upsert_my_node(num, nickname=str(body["nickname"])[:40])
                if "autoConnect" in body:
                    app.real_state.update_settings({"autoConnect": num if body["autoConnect"] else None})
                app.real_state.save(force=True)
                return self._json({"ok": True})
            if path == "/api/mynodes/remove":
                app.real_state.remove_my_node(int(body["num"]))
                app.real_state.save(force=True)
                return self._json({"ok": True})
            # -- messages
            if path == "/api/send":
                to = body.get("to")
                reply_id = body.get("replyId")  # answer to that packet; with "emoji" the text is a reaction to it
                pkt = app.radio.send_text(body.get("text", ""), int(to) if to not in (None, "") else 0xFFFFFFFF,
                                          int(body.get("channel") or 0),
                                          reply_id=int(reply_id) if reply_id not in (None, "") else None,
                                          emoji=bool(body.get("emoji")))
                return self._json({"ok": True, "packetId": pkt.id})
            # -- node configuration
            if path == "/api/config/apply":
                return self._json(app.config.apply(body))
            if path == "/api/config/backup":
                return self._json({"ok": True, "backup": app.config.backup(str(body.get("label") or "manual")[:60])})
            if path == "/api/config/backup/upload":
                return self._json({"ok": True, "backup": app.config.upload(body["name"], base64.b64decode(body["data"]))})
            if path == "/api/config/backup/delete":
                app.config.delete(body["file"])
                return self._json({"ok": True})
            if path == "/api/config/restore":
                return self._json(app.config.restore(body["file"]))
            # -- console
            if path == "/api/console/run":
                return self._json(app.console.run(body.get("args", ""), bool(body.get("confirmed"))))
            if path == "/api/console/clear":
                app.console.clear()
                return self._json({"ok": True})
            # -- addons
            if path == "/api/addons/create":
                a = app.addons.create(body.get("name") or "New addon", body.get("template", "blank"))
                return self._json({"ok": True, "slug": a.slug})
            if path == "/api/addons/save":
                a = app.addons.save(body["slug"], body["code"])
                return self._json({"ok": True, "slug": a.slug, "status": a.status, "error": a.error})
            if path == "/api/addons/enable":
                app.addons.set_enabled(body["slug"], bool(body.get("enabled")))
                return self._json({"ok": True})
            if path == "/api/addons/delete":
                app.addons.delete(body["slug"])
                return self._json({"ok": True})
            if path == "/api/addons/import":
                files = [{"name": f["name"], "data": base64.b64decode(f["data"])} for f in body.get("files", [])]
                return self._json({"ok": True, "created": app.addons.import_files(files)})
            # -- mqtt
            if path == "/api/mqtt/settings":
                patch = {k: v for k, v in body.items() if k in app.real_state.settings["mqtt"]}
                if patch.get("password") == "********":
                    patch.pop("password")
                app.real_state.update_settings({"mqtt": patch})
                app.real_state.save(force=True)
                if app.mqtt.client is not None and any(k in patch for k in ("host", "port", "tls", "username", "password", "topics")):
                    app.mqtt.start()  # reconnect with the new settings
                return self._json({"ok": True})
            if path == "/api/mqtt/start":
                app.real_state.update_settings({"mqtt": {"enabled": True}})
                app.mqtt.start()
                return self._json({"ok": True})
            if path == "/api/mqtt/stop":
                app.real_state.update_settings({"mqtt": {"enabled": False}})
                app.mqtt.stop()
                return self._json({"ok": True})
            # -- coverage
            if path == "/api/coverage":
                return self._json(app.coverage(body))
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


def _json_default(o):
    if isinstance(o, (bytes, bytearray)):
        return base64.b64encode(bytes(o)).decode()
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def start_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    return srv
