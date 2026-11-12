"""User addons: Python scripts that receive every inbound packet and can react to it.

An addon is a single ``.py`` file in ``%LOCALAPPDATA%\\MeshScope\\addons``. It may define:

    NAME = "Friendly name"             # optional
    TICK_SECONDS = 60                  # optional: call on_tick this often

    def on_start(api): ...
    def on_packet(packet, api): ...    # every inbound packet (radio, MQTT or demo)
    def on_tick(api): ...
    def on_stop(api): ...

Each addon runs in its own worker thread with its own queue, so a slow or crashing
addon can't stall packet handling or the other addons. Addons are ordinary Python
with full access to this PC: only run code you trust.
"""

from __future__ import annotations

import io
import json
import logging
import queue
import re
import threading
import time
import traceback
import types
import zipfile
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ingest import strip_raw
from .state import BROADCAST_NUM, node_id

log = logging.getLogger(__name__)

SEND_MIN_INTERVAL = 10.0  # seconds between transmissions per addon (protects the mesh)

API_DOC = """An addon is one .py file. Define any of these (all optional):

    NAME = "Friendly name"
    TICK_SECONDS = 60                # call on_tick() this often

    def on_start(api): ...
    def on_packet(packet, api): ...  # every inbound packet, each one once
    def on_tick(api): ...
    def on_stop(api): ...

packet is a dict:
    packet["source"]    "radio" | "mqtt" | "demo"
    packet["portnum"]   e.g. "TEXT_MESSAGE_APP", "POSITION_APP", "TELEMETRY_APP"
    packet["from"], packet["fromId"], packet["fromName"]
    packet["to"], packet["toName"]        ("everyone" for broadcasts)
    packet["text"]      text messages only
    packet["hops"]      hops used (None if unknown)
    packet["rxSnr"], packet["rxRssi"], packet["channel"]
    packet["decoded"]   the decoded payload, e.g. decoded["telemetry"]["deviceMetrics"]
    packet["receiver"]  node that heard it (your radio, or the MQTT gateway)

api:
    api.log(*values)            write to this addon's Output pane (print() does the same)
    api.notify(text)            pop-up in the app + entry in the Activity log
    api.store                   dict saved to disk between runs (api.save() to force)
    api.local_num, api.connected
    api.nodes(), api.node(num_or_"!id"), api.name(num)
    api.send_text(text, to=BROADCAST, channel=0, reply_to=None)   TRANSMITS (max 1 per 10 s per addon)
    api.react(packet, emoji)    TRANSMITS an emoji reaction to a received text message (same limit)
    api.request(kind, node)     kind: traceroute | ping | nodeinfo | position | telemetry

Each addon runs in its own thread; a slow or crashing addon can't block the others.
Addons are normal Python with your permissions: only enable code you trust.
"""

TEMPLATES: Dict[str, str] = {
    "blank": '''"""My addon"""
NAME = "My addon"


def on_start(api):
    api.log("started")


def on_packet(packet, api):
    # packet["portnum"], packet["fromName"], packet["text"], packet["decoded"], packet["source"] ...
    pass
''',
    "keyword_alert": '''"""Pop up a notification when a text message contains one of the keywords."""
NAME = "Keyword alert"
KEYWORDS = ["help", "emergency", "sos"]


def on_packet(packet, api):
    text = (packet.get("text") or "").lower()
    if any(k in text for k in KEYWORDS):
        api.notify(f'{packet["fromName"]}: {packet["text"]}')
''',
    "low_battery": '''"""Warn once a day per node when a node reports a low battery."""
import time

NAME = "Low battery watch"
THRESHOLD = 20  # percent


def on_packet(packet, api):
    metrics = (packet.get("decoded", {}).get("telemetry") or {}).get("deviceMetrics") or {}
    level = metrics.get("batteryLevel")
    if level is None or level > THRESHOLD:
        return
    warned = api.store.setdefault("warned", {})
    key = str(packet["from"])
    if time.time() - warned.get(key, 0) > 86400:
        warned[key] = time.time()
        api.notify(f'{packet["fromName"]} battery is at {level}%')
''',
    "traffic_stats": '''"""Count packets per type and log a summary every minute."""
from collections import Counter

NAME = "Traffic stats"
TICK_SECONDS = 60
counts = Counter()


def on_packet(packet, api):
    counts[(packet["source"], packet["portnum"])] += 1


def on_tick(api):
    if counts:
        api.log(", ".join(f"{src}/{port}: {n}" for (src, port), n in counts.most_common(8)))
        counts.clear()
''',
    "auto_reply": '''"""Reply to direct messages that say "ping". THIS TRANSMITS on your radio."""
NAME = "Ping auto-reply"


def on_packet(packet, api):
    if packet["source"] != "radio" or packet["portnum"] != "TEXT_MESSAGE_APP":
        return
    if packet["to"] == api.local_num and (packet.get("text") or "").strip().lower() == "ping":
        api.send_text(f"pong ({packet.get('rxSnr')} dB, {packet.get('hops')} hops)", to=packet["from"])
''',
}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "_", (name or "").strip().lower()).strip("_")
    return s[:60] or "addon"


class AddonAPI:
    """The ``api`` object passed to addon callbacks."""

    def __init__(self, addon: "Addon", manager: "AddonManager"):
        self._addon = addon
        self._mgr = manager
        self._last_send = 0.0
        self.store: Dict[str, Any] = {}

    # -- output
    def log(self, *args):
        self._addon.output(" ".join(str(a) for a in args))

    def notify(self, text: str):
        """Show a pop-up in the app and record it in the Activity log."""
        self._addon.output(f"[notify] {text}")
        self._mgr.app.state.add_log("notify", f"{self._addon.name}: {text}", addon=self._addon.slug)

    # -- data
    @property
    def local_num(self) -> Optional[int]:
        return self._mgr.app.state.local_num

    @property
    def connected(self) -> bool:
        return bool(getattr(self._mgr.app.radio, "connected", False))

    def nodes(self) -> List[Dict[str, Any]]:
        return [dict(n) for n in list(self._mgr.app.state.nodes.values())]

    def node(self, ident) -> Optional[Dict[str, Any]]:
        st = self._mgr.app.state
        if isinstance(ident, str):
            ident = int(ident.lstrip("!"), 16)
        n = st.nodes.get(ident)
        return dict(n) if n else None

    def name(self, num) -> str:
        return self._mgr.app.state.name_of(num)

    # -- actions
    def _rate_ok(self, what: str) -> bool:
        now = time.time()
        if now - self._last_send < SEND_MIN_INTERVAL:
            self.log(f"{what} suppressed: rate limit ({SEND_MIN_INTERVAL:.0f}s between sends)")
            return False
        self._last_send = now
        return True

    def send_text(self, text: str, to=BROADCAST_NUM, channel: int = 0, reply_to=None):
        """Transmit a text message (rate-limited to one per 10 s per addon).

        ``reply_to`` may be a received packet (or its id) to send this as a reply to it."""
        if not self._rate_ok("send_text"):
            return False
        if isinstance(to, str) and to.startswith("!"):
            to = int(to[1:], 16)
        reply_id = reply_to.get("id") if isinstance(reply_to, dict) else reply_to
        text = str(text).encode("utf-8")[:200].decode("utf-8", "ignore")
        self._mgr.app.radio.send_text(text, to, channel, reply_id=reply_id)
        self.log(f"sent to {self.name(to) if to != BROADCAST_NUM else 'everyone'}: {text}")
        return True

    def react(self, packet: Dict[str, Any], emoji: str):
        """Transmit an emoji reaction to a received text message, back where it came from:
        to the sender for a direct message, otherwise to the same channel. Same rate limit as send_text."""
        if not packet.get("id"):
            self.log("react: the packet has no id to react to")
            return False
        if not self._rate_ok("react"):
            return False
        dm = packet.get("to") == self.local_num
        to = packet["from"] if dm else BROADCAST_NUM
        channel = 0 if dm else int(packet.get("channel") or 0)
        self._mgr.app.radio.send_text(str(emoji), to, channel, reply_id=packet["id"], emoji=True)
        self.log(f"reacted {emoji} to {packet.get('fromName') or self.name(packet['from'])}: {packet.get('text') or ''}")
        return True

    def request(self, kind: str, node):
        """Run traceroute / ping / nodeinfo / position / telemetry on a node."""
        if isinstance(node, str):
            node = int(node.lstrip("!"), 16)
        return self._mgr.app.radio.run_action(kind, node)["id"]

    def save(self):
        self._addon.save_store()


class Addon:
    def __init__(self, mgr: "AddonManager", slug: str):
        self.mgr = mgr
        self.slug = slug
        self.path = mgr.dir / f"{slug}.py"
        self.store_path = mgr.dir / f"{slug}.store.json"
        self.name = slug
        self.enabled = False
        self.status = "stopped"  # stopped | running | error
        self.error = ""
        self.packets = 0
        self.errors = 0
        self.last_activity = None
        self.module: Optional[types.ModuleType] = None
        self.api: Optional[AddonAPI] = None
        self.q: "queue.Queue" = queue.Queue(maxsize=1000)
        self.thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.lines: deque = deque(maxlen=500)
        self.seq = 0
        self.dropped = 0

    def output(self, text: str, kind: str = "out"):
        for part in str(text).splitlines() or [""]:
            self.seq += 1
            self.lines.append({"seq": self.seq, "t": time.time(), "kind": kind, "text": part})
        self.mgr.touch()

    def read_code(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def _peek_name(self, code: str):
        m = re.search(r'^NAME\s*=\s*["\'](.+?)["\']', code, re.M)
        self.name = m.group(1) if m else self.slug

    # -- lifecycle
    def start(self):
        self.stop()
        code = self.read_code()
        self._peek_name(code)
        mod = types.ModuleType(f"meshscope_addon_{self.slug}")
        mod.__file__ = str(self.path)
        api = AddonAPI(self, self.mgr)
        try:
            api.store = json.loads(self.store_path.read_text(encoding="utf-8")) if self.store_path.exists() else {}
        except Exception:
            api.store = {}
        mod.__dict__["print"] = lambda *a, **k: api.log(*a)
        try:
            exec(compile(code, str(self.path), "exec"), mod.__dict__)
        except Exception:
            self.status, self.error = "error", traceback.format_exc(limit=3)
            self.output(self.error, "err")
            return
        self.module, self.api = mod, api
        self.name = getattr(mod, "NAME", self.slug)
        self._stop = threading.Event()
        self.q = queue.Queue(maxsize=1000)
        self.status, self.error = "running", ""
        self.thread = threading.Thread(target=self._worker, daemon=True, name=f"addon-{self.slug}")
        self.thread.start()
        self.output("started", "sys")

    def stop(self):
        if self.thread is not None:
            self._stop.set()
            self.thread.join(timeout=3)
            self.thread = None
            self.save_store()
            if self.status == "running":
                self.status = "stopped"
            self.output("stopped", "sys")
        self.module = None

    def save_store(self):
        if self.api is not None:
            try:
                self.store_path.write_text(json.dumps(self.api.store, default=str), encoding="utf-8")
            except Exception:
                log.exception("addon %s store save failed", self.slug)

    def offer(self, packet: Dict[str, Any]):
        if self.status != "running":
            return
        try:
            self.q.put_nowait(packet)
        except queue.Full:
            self.dropped += 1

    def _call(self, fn_name: str, *args):
        fn = getattr(self.module, fn_name, None) if self.module else None
        if not callable(fn):
            return
        try:
            fn(*args, self.api)
            self.last_activity = time.time()
        except Exception:
            self.errors += 1
            self.error = traceback.format_exc(limit=4)
            self.output(f"error in {fn_name}:\n{self.error}", "err")

    def _worker(self):
        self._call("on_start")
        tick = float(getattr(self.module, "TICK_SECONDS", 0) or 0)
        next_tick = time.time() + tick if tick else None
        last_save = time.time()
        while not self._stop.is_set():
            try:
                pkt = self.q.get(timeout=0.5)
                self.packets += 1
                self._call("on_packet", pkt)
            except queue.Empty:
                pass
            now = time.time()
            if next_tick and now >= next_tick:
                next_tick = now + tick
                self._call("on_tick")
            if now - last_save > 60:
                last_save = now
                self.save_store()
        self._call("on_stop")

    def info(self) -> Dict[str, Any]:
        return {"slug": self.slug, "name": self.name, "enabled": self.enabled, "status": self.status,
                "error": self.error, "packets": self.packets, "errors": self.errors, "dropped": self.dropped,
                "lastActivity": self.last_activity, "outputSeq": self.seq}


class AddonManager:
    def __init__(self, app, directory: Path):
        self.app = app
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.dir / "addons.json"
        self.addons: Dict[str, Addon] = {}
        self.version = 0
        self._lock = threading.RLock()
        self._load()

    def touch(self):
        self.version += 1

    # -- config
    def _load(self):
        cfg = {}
        if self.cfg_path.exists():
            try:
                cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("bad addons.json")
        for f in sorted(self.dir.glob("*.py")):
            a = Addon(self, f.stem)
            a.enabled = bool(cfg.get(f.stem, {}).get("enabled"))
            a._peek_name(a.read_code())
            self.addons[f.stem] = a

    def _save_cfg(self):
        cfg = {s: {"enabled": a.enabled} for s, a in self.addons.items()}
        self.cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        self.touch()

    def start_enabled(self):
        for a in list(self.addons.values()):
            if a.enabled:
                a.start()
        self.touch()

    def stop_all(self):
        for a in list(self.addons.values()):
            a.stop()

    # -- dispatch
    def dispatch(self, p: Dict[str, Any], source: str, info: Dict[str, Any]):
        running = [a for a in self.addons.values() if a.status == "running"]
        if not running:
            return
        st = self.app.state
        pkt = strip_raw(p)
        decoded = pkt.setdefault("decoded", {})  # empty for packets that couldn't be decrypted
        pkt.update({
            "source": source,
            "portnum": info.get("port") or decoded.get("portnum"),
            "text": info.get("text") or decoded.get("text"),
            "hops": info.get("hops"),
            "fromId": node_id(p.get("from", 0)),
            "fromName": st.name_of(p.get("from")),
            "toName": "everyone" if p.get("to") == BROADCAST_NUM else st.name_of(p.get("to")),
            "receiver": info.get("receiver"),
        })
        for a in running:
            a.offer(pkt)

    # -- CRUD
    def get(self, slug: str) -> Addon:
        a = self.addons.get(slug)
        if a is None:
            raise KeyError(f"No addon named {slug}")
        return a

    def _unique_slug(self, base: str) -> str:
        slug, i = slugify(base), 2
        root = slug
        while slug in self.addons:
            slug = f"{root}_{i}"
            i += 1
        return slug

    def create(self, name: str, template: str = "blank", code: Optional[str] = None) -> Addon:
        with self._lock:
            slug = self._unique_slug(name)
            code = code if code is not None else TEMPLATES.get(template, TEMPLATES["blank"])
            if code is TEMPLATES.get(template) and name:
                code = re.sub(r'^NAME\s*=.*$', f'NAME = {json.dumps(name)}', code, count=1, flags=re.M)
            (self.dir / f"{slug}.py").write_text(code, encoding="utf-8")
            a = Addon(self, slug)
            a._peek_name(code)
            self.addons[slug] = a
            self._save_cfg()
            return a

    def save(self, slug: str, code: str) -> Addon:
        with self._lock:
            a = self.get(slug)
            compile(code, a.path.name, "exec")  # raise SyntaxError to the caller before writing
            a.path.write_text(code, encoding="utf-8")
            a._peek_name(code)
            if a.enabled:
                a.start()
            self.touch()
            return a

    def set_enabled(self, slug: str, enabled: bool):
        with self._lock:
            a = self.get(slug)
            a.enabled = enabled
            a.start() if enabled else a.stop()
            self._save_cfg()

    def delete(self, slug: str):
        with self._lock:
            a = self.get(slug)
            a.stop()
            a.path.unlink(missing_ok=True)
            a.store_path.unlink(missing_ok=True)
            del self.addons[slug]
            self._save_cfg()

    def import_files(self, files: List[Dict[str, Any]]) -> List[str]:
        """files: [{"name": "x.py" | "pack.zip", "data": bytes}]. Imported addons start disabled."""
        created = []
        with self._lock:
            for f in files:
                name, data = f["name"], f["data"]
                if name.lower().endswith(".zip"):
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        for info in z.infolist():
                            if info.filename.lower().endswith(".py") and not info.is_dir():
                                stem = Path(info.filename).stem
                                created.append(self.create(stem, code=z.read(info).decode("utf-8", "replace")).slug)
                elif name.lower().endswith(".py"):
                    created.append(self.create(Path(name).stem, code=data.decode("utf-8", "replace")).slug)
                else:
                    raise ValueError(f"{name}: only .py files or .zip archives can be imported")
        return created

    def export(self, slugs: List[str]):
        """Return (filename, content-type, bytes): a .py for one addon, a .zip for several."""
        addons = [self.get(s) for s in slugs] if slugs else list(self.addons.values())
        if not addons:
            raise ValueError("No addons to export")
        if len(addons) == 1 and slugs:
            a = addons[0]
            return f"{a.slug}.py", "text/x-python", a.read_code().encode("utf-8")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for a in addons:
                z.writestr(f"{a.slug}.py", a.read_code())
        return f"meshscope-addons-{time.strftime('%Y%m%d')}.zip", "application/zip", buf.getvalue()

    def list(self) -> Dict[str, Any]:
        return {"version": self.version, "dir": str(self.dir), "templates": sorted(TEMPLATES), "doc": API_DOC,
                "addons": [a.info() for a in sorted(self.addons.values(), key=lambda a: a.name.lower())]}

    def detail(self, slug: str, since: int = 0) -> Dict[str, Any]:
        a = self.get(slug)
        d = a.info()
        d["code"] = a.read_code()
        d["output"] = [l for l in list(a.lines) if l["seq"] > since]
        return d
