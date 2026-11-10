"""The App object: owns the state, the radio, MQTT, addons, the CLI console and coverage."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .addons import AddonManager
from .console import Console
from .demo import DemoRadio, demo_node_names
from .ingest import PacketProcessor, SeenTracker
from .mqtt import MqttBridge
from .nodeconfig import ConfigManager
from .paths import data_dir
from .radio import Radio, list_ports
from .state import MeshState

log = logging.getLogger(__name__)


class App:
    """Holds the persistent (real) mesh state and, while demo mode runs, a throwaway demo state.

    ``self.state`` is whatever the UI should currently see. Demo mode swaps in an
    in-memory MeshState (no db_path) so simulated nodes never reach the real
    database; leaving demo mode discards it and swaps the real state back.
    """

    def __init__(self, state: MeshState, static: Path, workdir: Optional[Path] = None):
        self.real_state = state
        self.state = state
        self.static = static
        self.workdir = workdir or (state.db_path.parent if state.db_path else data_dir())
        self.token = secrets.token_urlsafe(16)
        self.last_poll = 0.0
        self.quit_event = threading.Event()
        self._swap_lock = threading.RLock()
        self.seen = SeenTracker()
        self.console = Console(self)
        self.addons = AddonManager(self, self.workdir / "addons")
        self.mqtt = MqttBridge(self)
        self.config = ConfigManager(self, self.workdir / "config-backups")
        self.mqtt_processor = PacketProcessor(state)
        self.mqtt_processor.SOURCE = "mqtt"
        self.mqtt_processor.on_packet = self._on_packet
        self._coverage = None
        self.want_node: Optional[int] = None  # a My Node to keep connected (auto-reconnect)
        self._last_reconnect = 0.0
        self._last_probe_ports: tuple = ()
        self.radio: Radio = self._new_radio(Radio, state)
        self._purge_legacy_demo_nodes()

    # ----------------------------------------------------------- lifecycle
    def start(self):
        """Background services: addons, MQTT, auto-connect, reconnect watchdog."""
        self.addons.start_enabled()
        if self.real_state.settings["mqtt"].get("enabled"):
            self.mqtt.start()
        auto = self.real_state.settings.get("autoConnect")
        if auto is not None and int(auto) in self.real_state.my_nodes:
            self.want_node = int(auto)
        threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()

    def shutdown(self):
        self.want_node = None
        self.addons.stop_all()
        self.mqtt.stop()
        try:
            self.radio.disconnect()
        except Exception:
            log.exception("disconnect on exit")

    def _new_radio(self, cls, state):
        r = cls(state)
        r.on_packet = self._on_packet
        r.on_connected = self._on_radio_connected
        r.on_log_line = self.console.device_line
        return r

    @property
    def in_demo(self) -> bool:
        return self.state is not self.real_state

    def _purge_legacy_demo_nodes(self):
        """Remove demo nodes that older versions wrote into the real database."""
        demo = demo_node_names()
        st = self.real_state
        stale = [num for num, n in st.nodes.items() if num in demo and n.get("longName") in (None, demo[num])]
        if stale:
            st.forget_nodes(stale)
            st.save(force=True)
            log.info("Removed %d leftover demo nodes from the database", len(stale))

    # ---------------------------------------------------------------- demo
    def _enter_demo(self):
        with self._swap_lock:
            if self.in_demo:
                return
            self.want_node = None
            self.radio.disconnect()
            demo_state = MeshState(None)
            demo_state.version = self.real_state.version + 1  # make the UI notice the swap
            demo_state.settings = self.real_state.settings
            demo_state.favorites = set(self.real_state.favorites)
            self.state = demo_state
            self.radio = self._new_radio(DemoRadio, demo_state)

    def _exit_demo(self):
        with self._swap_lock:
            if not self.in_demo:
                return
            self.radio.disconnect()
            demo_state = self.state
            self.state = self.real_state
            self.radio = self._new_radio(Radio, self.real_state)
            self.real_state.version = max(self.real_state.version, demo_state.version) + 1
            self.real_state.add_log("conn", "Demo mode ended; simulated nodes discarded")

    # ------------------------------------------------------------- connect
    def connect(self, body):
        if body.get("demo"):
            self._enter_demo()
            self.radio.connect("DEMO")
            return {"ok": True}
        self._exit_demo()
        if body.get("myNode") is not None:
            return self.connect_my_node(int(body["myNode"]))
        port = body.get("port")
        if not port:
            raise ValueError("No COM port selected")
        self.want_node = None
        self.radio.connect(port)
        return {"ok": True}

    def disconnect(self):
        self.want_node = None
        if self.in_demo:
            self._exit_demo()
        else:
            self.radio.disconnect()

    # ------------------------------------------------------------ my nodes
    def candidate_ports(self, num: int, ports: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Where is a remembered radio plugged in right now?

        COM numbers are assigned by Windows and can change (different USB socket, hub,
        driver reinstall), so a radio is identified by its node number. To find it we use,
        in order: the USB serial number of the port it was last seen on (stable for CP210x,
        ESP32-S3 and nRF52 boards), the last COM port, then any other port that looks like
        Meshtastic hardware - each is opened and its node number checked before use.
        """
        ports = ports if ports is not None else list_ports()
        rec = self.real_state.my_nodes.get(num, {})
        by_dev = {p["device"]: p for p in ports}
        exact = [p["device"] for p in ports
                 if rec.get("usbSerial") and p.get("serial") == rec["usbSerial"] and p.get("vid") == rec.get("vid")]
        cands = list(exact)
        last = rec.get("lastPort")
        if last in by_dev and last not in cands:
            cands.append(last)
        for p in ports:
            if p["likelyMeshtastic"] and p["device"] not in cands:
                cands.append(p["device"])
        match = "usb-serial" if exact else ("last-port" if last in by_dev else ("probe" if cands else None))
        return {"ports": cands, "match": match, "present": bool(exact) or (last in by_dev and
                                                                          by_dev[last].get("vid") == rec.get("vid"))}

    def connect_my_node(self, num: int, auto: bool = False):
        rec = self.real_state.my_nodes.get(num)
        if rec is None:
            raise KeyError("Unknown My Node")
        c = self.candidate_ports(num)
        label = rec.get("nickname") or rec.get("shortName") or rec.get("longName") or rec.get("id")
        if not c["ports"]:
            raise RuntimeError(f"{label} is not plugged in (no Meshtastic-like USB serial ports found)")
        self.want_node = num
        self.radio.connect(c["ports"], expect_num=num, label=label)
        return {"ok": True, "trying": c["ports"], "match": c["match"]}

    def _on_radio_connected(self, port: str, iface):
        st = self.real_state
        num = st.local_num
        if num is None:
            return
        usb = next((p for p in list_ports() if p["device"] == port), {})
        st.upsert_my_node(num, lastPort=port, lastConnected=time.time(), usbSerial=usb.get("serial"),
                          vid=usb.get("vid"), pid=usb.get("pid"), usbDescription=usb.get("description"))
        self.want_node = num  # keep this radio connected through unplug/replug
        st.save(force=True)
        threading.Thread(target=self.config.auto_backup_on_connect, daemon=True, name="autobackup").start()

    def my_nodes(self) -> List[Dict[str, Any]]:
        ports = list_ports()
        out = []
        st = self.real_state
        for rec in st.my_nodes.values():
            r = dict(rec)
            c = self.candidate_ports(rec["num"], ports)
            r["presentPort"] = c["ports"][0] if c["present"] else None
            r["match"] = c["match"] if c["present"] else None
            r["connected"] = (not self.in_demo and st.status == "connected" and st.local_num == rec["num"])
            r["autoConnect"] = st.settings.get("autoConnect") == rec["num"]
            out.append(r)
        out.sort(key=lambda r: -(r.get("lastConnected") or 0))
        return out

    def _watchdog(self):
        """Reconnect a wanted My Node when it is plugged back in (on whatever COM port)."""
        while not self.quit_event.wait(5):
            try:
                st = self.real_state
                num = self.want_node
                if (num is None or self.in_demo or st.status in ("connected", "connecting")
                        or time.time() - self._last_reconnect < 15):
                    continue
                c = self.candidate_ports(num)
                port_set = tuple(sorted(c["ports"]))
                # probe unidentified ports only when the set of USB ports has changed
                if c["present"] or (port_set and port_set != self._last_probe_ports):
                    self._last_probe_ports = port_set
                    self._last_reconnect = time.time()
                    log.info("Auto-connecting My Node !%08x via %s", num, c["ports"])
                    self.connect_my_node(num, auto=True)
            except Exception:
                log.exception("watchdog")

    # ------------------------------------------------------------- packets
    def _on_packet(self, p: Dict[str, Any], source: str, info: Dict[str, Any]):
        key = (p.get("from"), p.get("id")) if p.get("id") else None
        rec = self.seen.mark(key, source) if key else {"first": True, "sources": {source}}
        if rec["first"]:
            self.addons.dispatch(p, source, info)
        if source == "radio":
            self.mqtt.consider_uplink(p, key, rec)

    def ingest_mqtt(self, p: Dict[str, Any], gateway: Optional[int]) -> str:
        """Entry point for packets from the MQTT bridge (always into the real state)."""
        key = (p.get("from"), p.get("id")) if p.get("id") else None
        if key and self.seen.get(key) is not None:
            # already seen (over RF or from another gateway): only learn the gateway's RF link
            self.seen.mark(key, "mqtt")
            hs, hl = p.get("hopStart"), p.get("hopLimit", 0)
            if hs and hs == hl and gateway is not None:
                self.real_state.add_link(p["from"], gateway, p.get("rxSnr"), "mqtt")
            return "dup"
        self.mqtt_processor.state = self.real_state
        self.mqtt_processor.handle_packet(p, source="mqtt", receiver=gateway)
        return "new"

    # ------------------------------------------------------------ coverage
    def coverage(self, body: Dict[str, Any]) -> Dict[str, Any]:
        from .terrain import CoverageEngine

        if self._coverage is None:
            self._coverage = CoverageEngine(self.workdir / "terrain-cache")
        st = self.state
        params = dict(body.get("params", {}))
        params.setdefault("defaultHeight", float(st.settings.get("defaultAntennaHeight", 2.0)))
        txs = []
        for num in body.get("nodes", []):
            t = st.coverage_inputs(int(num))
            if t:
                t["height"] = float(t["antennaHeight"]) if t.get("antennaHeight") else None
                txs.append(t)
        if not txs:
            raise ValueError("None of the selected nodes has a known position")
        if len(txs) > 40:
            txs = txs[:40]
        return self._coverage.compute(txs, params)
