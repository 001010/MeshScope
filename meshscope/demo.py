"""A simulated mesh so the app can be explored (and tested) without a radio.

Synthetic packets are pushed through the real ``Radio.handle_packet`` code path,
so the demo exercises the same decoding, link inference and job matching.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from types import SimpleNamespace

from .radio import Radio
from .state import BROADCAST_NUM, haversine_km

CENTER = (39.7392, -104.9903)
ROLES = ["CLIENT"] * 6 + ["ROUTER", "CLIENT_MUTE", "ROUTER_LATE"]
HW = ["HELTEC_V3", "TBEAM", "RAK4631", "T_ECHO", "STATION_G2", "TRACKER_T1000_E", "HELTEC_WIRELESS_PAPER"]
NAMES = ["Ridge", "Creek", "Summit", "Mesa", "Harbor", "Pine", "Aspen", "Falcon", "Granite", "Willow",
         "Basalt", "Cedar", "Prairie", "Juniper", "Canyon", "Elk", "Bluebird", "Boulder", "Sage", "Timber",
         "Cobalt", "Quartz", "Larkspur", "Ember", "Cottonwood", "Kestrel", "Badger", "Foothill"]


def demo_node_names(seed: int = 7, count: int = 26) -> dict:
    """{node number: long name} for every node the demo generates (it is deterministic)."""
    d = DemoRadio(None, seed=seed, count=count)
    d._build()
    return {num: n["name"] for num, n in d.info.items()}


REACTIONS = ["👍", "❤️", "😂", "👀", "✅", "🔥"]
REPLIES = ["copy that", "heard you loud and clear", "same here", "roger, 2 hops out", "nice, thanks!", "on my way"]
CHATTER = ["anyone copy?", "checking in from the trailhead", "battery at 40%, heading home",
            "test test", "good morning mesh", "copy, 2 hops", "weather looks rough up north", "ping"]


class DemoRadio(Radio):
    SOURCE = "demo"

    DEMO_CHANNELS = [
        {"index": 0, "name": "LongFast", "primary": True, "uplinkEnabled": False, "defaultKey": True, "key": b""},
        {"index": 1, "name": "Trailhead", "primary": False, "uplinkEnabled": False, "defaultKey": False, "key": b""},
    ]

    def channels(self):
        return [dict(c) for c in self.DEMO_CHANNELS]

    def _transmit_text(self, text, dest, channel, reply_id=None, emoji=False):
        """Nothing is transmitted in demo mode; simulate the ACK the firmware would report."""
        pid = self._next_id()
        if dest == BROADCAST_NUM and not emoji:
            self._recent_txt.append((pid, channel or 0))  # others may react or reply to it

        def ack():
            time.sleep(self.rng.uniform(1.0, 2.5))
            if dest == BROADCAST_NUM:
                self.handle_packet({"from": self.local, "to": self.local, "decoded": {
                    "portnum": "ROUTING_APP", "requestId": pid, "routing": {"errorReason": "NONE"}}})
                return
            back = self._path(dest, self.local, 3) if dest in self.truth else None
            if back:
                self._deliver(dest, self.local, "ROUTING_APP", request_id=pid, path=back,
                              decoded_extra={"routing": {"errorReason": "NONE"}})
            else:
                self.handle_packet({"from": self.local, "to": self.local, "decoded": {
                    "portnum": "ROUTING_APP", "requestId": pid, "routing": {"errorReason": "MAX_RETRANSMIT"}}})

        threading.Thread(target=ack, daemon=True).start()
        return SimpleNamespace(id=pid)

    def __init__(self, state, seed: int = 7, count: int = 26):
        super().__init__(state)
        self.rng = random.Random(seed)
        self.count = count
        self._stop = threading.Event()
        self._pkt_id = 1000
        self._recent_txt = deque(maxlen=12)  # (packet id, channel) of recent channel messages
        self.truth = {}
        self.info = {}

    @property
    def connected(self) -> bool:
        return self.state.status == "connected" and not self._stop.is_set()

    # ------------------------------------------------------------- topology
    def _build(self):
        r = self.rng
        self.local = 0x0A1B2C3D
        nodes = {self.local: dict(lat=CENTER[0], lon=CENTER[1], range=9.0, name="Base Station", short="BASE",
                                  hw="STATION_G2", role="CLIENT", gps=True)}
        for i in range(self.count):
            num = r.randrange(0x10000000, 0xFFFFFFF0)
            ang, dist = r.uniform(0, 2 * math.pi), r.uniform(1.0, 26.0) * (r.random() ** 0.5 + 0.2)
            lat = CENTER[0] + (dist / 111.0) * math.cos(ang)
            lon = CENTER[1] + (dist / (111.0 * math.cos(math.radians(CENTER[0])))) * math.sin(ang)
            role = r.choice(ROLES)
            nm = NAMES[i % len(NAMES)]
            nodes[num] = dict(lat=lat, lon=lon, range=r.uniform(14, 22) if role.startswith("ROUTER") else r.uniform(4, 11),
                              name=f"{nm} {r.choice(['Node', 'Relay', 'Base', 'Mobile', 'Hilltop'])}",
                              short=nm[:3].upper() + str(i % 10), hw=r.choice(HW), role=role, gps=r.random() > 0.12)
        # antenna sites and privacy settings (separate RNG so the mesh layout above stays the same)
        r2 = random.Random(1234)
        for num, n in nodes.items():
            tall = n["role"].startswith("ROUTER") or "Hilltop" in n["name"]
            n["agl"] = round(r2.uniform(12, 35) if tall else r2.uniform(1, 3), 1)  # antenna height above ground
            n["alt"] = round(1610 + n["agl"])  # rough Denver ground level + mast; real radios report GPS altitude
            n["precision"] = 16 if num != self.local and r2.random() < 0.15 else 32  # ~+-364 m when reduced
        self.info = nodes
        adj = {n: {} for n in nodes}
        for a in nodes:
            for b in nodes:
                if a >= b:
                    continue
                d = haversine_km(nodes[a]["lat"], nodes[a]["lon"], nodes[b]["lat"], nodes[b]["lon"])
                rng_ab = min(nodes[a]["range"], nodes[b]["range"]) * 1.15
                if d < rng_ab:
                    snr = round(max(-19.5, min(12.0, 11 - 30 * d / rng_ab + r.gauss(0, 1.5))), 2)
                    adj[a][b] = snr
                    adj[b][a] = round(snr + r.gauss(0, 1), 2)
        self.truth = adj

    def _pos_report(self, n):
        """A position packet as the node would send it: GPS altitude jitters a few metres, and nodes with
        reduced precision send coordinates snapped to the centre of their precision cell."""
        lat, lon, bits = n["lat"], n["lon"], n["precision"]
        if bits < 32:
            cell = 2 ** (32 - bits) * 1e-7
            lat, lon = (math.floor(lat / cell) + 0.5) * cell, (math.floor(lon / cell) + 0.5) * cell
        return {"latitude": lat, "longitude": lon, "altitude": round(n["alt"] + self.rng.gauss(0, 3)), "precisionBits": bits}

    def _path(self, src, dst, limit=7):
        prev = {src: None}
        q = deque([src])
        while q:
            u = q.popleft()
            if u == dst:
                break
            for v in sorted(self.truth[u], key=lambda v: -self.truth[u][v]):
                if v not in prev:
                    prev[v] = u
                    q.append(v)
        if dst not in prev:
            return None
        path = [dst]
        while prev[path[-1]] is not None:
            path.append(prev[path[-1]])
        path.reverse()
        return path if len(path) - 1 <= limit + 1 else None

    # ------------------------------------------------------------ lifecycle
    def connect(self, port: str = "DEMO"):
        self.disconnect()
        self._stop.clear()
        self._build()
        st = self.state
        st.local_num = self.local
        st.hop_limit = 3
        now = time.time()
        for num, n in self.info.items():
            path = self._path(num, self.local, 3) if num != self.local else [num]
            if path is None and num != self.local:
                continue  # not in the radio's node DB yet
            st.merge_lib_node({
                "num": num,
                "user": {"longName": n["name"], "shortName": n["short"], "hwModel": n["hw"], "role": n["role"]},
                "position": self._pos_report(n) if n["gps"] else {},
                "lastHeard": now - self.rng.randint(30, 3600),
                "hopsAway": max(0, len(path) - 2),  # relays between it and us
                "snr": self.truth[num].get(self.local) if len(path) == 2 else None,
                "deviceMetrics": {"batteryLevel": self.rng.randint(15, 101), "voltage": round(self.rng.uniform(3.4, 4.2), 2),
                                  "channelUtilization": round(self.rng.uniform(2, 30), 1), "airUtilTx": round(self.rng.uniform(0.1, 6), 2)},
            })
            if len(path) == 2:  # the real radio reports these as 0-hop neighbours
                st.add_link(num, self.local, self.truth[num][self.local], "nodedb")
        st.set_channels(self.channels())
        st.set_status("connected", "Simulated mesh (demo mode)", port="DEMO")
        st.add_log("conn", "Demo mode started: simulated mesh around Denver, CO")
        threading.Thread(target=self._sim_loop, daemon=True, name="demo").start()

    def disconnect(self):
        if self.state is not None and self.state.status != "disconnected":
            self._stop.set()
            self.state.set_channels([])
            self.state.add_log("conn", "Demo stopped")
            self.state.set_status("disconnected")

    # --------------------------------------------------------- packet sim
    def _deliver(self, src, dst, portnum, payload=b"", request_id=None, hop_start=3, path=None, decoded_extra=None,
                 channel=0):
        """Simulate ``src`` flooding a packet and our radio receiving it via ``path``."""
        path = path or self._path(src, self.local, hop_start)
        if not path:
            return False
        hops = max(0, len(path) - 2)  # relays between src and us
        last_tx = path[-2] if len(path) >= 2 else src
        decoded = {"portnum": portnum, "payload": payload}
        if request_id is not None:
            decoded["requestId"] = request_id
        if decoded_extra:
            decoded.update(decoded_extra)
        decoded.setdefault("bitfield", 1)
        pkt = {
            "from": src, "to": dst, "id": self._next_id(), "rxTime": int(time.time()), "channel": channel,
            "rxSnr": self.truth[last_tx].get(self.local), "rxRssi": self.rng.randint(-125, -70),
            "hopStart": hop_start, "hopLimit": hop_start - hops,
            "relayNode": (last_tx & 0xFF) if hops > 0 else 0,
            "decoded": decoded,
        }
        self.handle_packet(pkt)
        return pkt["id"]

    def _next_id(self):
        self._pkt_id += 1
        return self._pkt_id

    def _sim_loop(self):
        from meshtastic.protobuf import mesh_pb2

        r = self.rng
        others = [n for n in self.info if n != self.local]
        while not self._stop.wait(r.uniform(0.6, 1.8)):
            src = r.choice(others)
            roll = r.random()
            n = self.info[src]
            if roll < 0.12:
                ni = mesh_pb2.NeighborInfo(node_id=src, node_broadcast_interval_secs=900)
                for nb, snr in list(self.truth[src].items())[:10]:
                    ni.neighbors.add(node_id=nb, snr=self.truth[nb][src])
                self._deliver(src, BROADCAST_NUM, "NEIGHBORINFO_APP", ni.SerializeToString())
            elif roll < 0.3 and n["gps"]:
                self._deliver(src, BROADCAST_NUM, "POSITION_APP",
                              decoded_extra={"position": self._pos_report(n)})
            elif roll < 0.36:
                txt = r.choice(CHATTER)
                sub = r.random()
                if self._recent_txt and r.random() < 0.35:  # react or reply to a recent channel message
                    pid, ch = r.choice(list(self._recent_txt))
                    if r.random() < 0.6:
                        extra = {"text": r.choice(REACTIONS), "replyId": pid, "emoji": 1}
                    else:
                        extra = {"text": r.choice(REPLIES), "replyId": pid}
                    self._deliver(src, BROADCAST_NUM, "TEXT_MESSAGE_APP", extra["text"].encode(), decoded_extra=extra, channel=ch)
                elif sub < 0.15:  # a direct message to us
                    self._deliver(src, self.local, "TEXT_MESSAGE_APP", txt.encode(), decoded_extra={"text": txt})
                else:
                    ch = 1 if sub < 0.4 else 0
                    pid = self._deliver(src, BROADCAST_NUM, "TEXT_MESSAGE_APP", txt.encode(), decoded_extra={"text": txt},
                                        channel=ch)
                    if pid:
                        self._recent_txt.append((pid, ch))
            elif roll < 0.45:
                self._deliver(src, BROADCAST_NUM, "NODEINFO_APP",
                              decoded_extra={"user": {"longName": n["name"], "shortName": n["short"],
                                                      "hwModel": n["hw"], "role": n["role"]}})
            else:
                self._deliver(src, BROADCAST_NUM, "TELEMETRY_APP", decoded_extra={"telemetry": {"deviceMetrics": {
                    "batteryLevel": r.randint(10, 101), "voltage": round(r.uniform(3.4, 4.2), 2),
                    "channelUtilization": round(r.uniform(2, 30), 1), "airUtilTx": round(r.uniform(0.1, 6), 2)}}})

    # -------------------------------------------------------------- actions
    def _send(self, kind, target, hop_limit):
        pid = self._next_id()
        threading.Thread(target=self._respond, args=(kind, target, hop_limit, pid), daemon=True).start()
        return SimpleNamespace(id=pid)

    def _respond(self, kind, target, hop_limit, pid):
        from meshtastic.protobuf import mesh_pb2

        r = self.rng
        fwd = self._path(self.local, target, hop_limit)
        if fwd is None or len(fwd) - 1 > hop_limit or r.random() < 0.08:
            time.sleep(r.uniform(5, 9))
            if kind == "ping":
                self.handle_packet({"from": self.local, "to": self.local, "decoded": {
                    "portnum": "ROUTING_APP", "requestId": pid, "routing": {"errorReason": "MAX_RETRANSMIT"}}})
            return  # other requests simply time out
        back = self._path(target, self.local, hop_limit) or list(reversed(fwd))
        time.sleep(r.uniform(1.0, 2.2) * len(fwd))
        n = self.info[target]
        if kind == "ping":
            if len(fwd) > 2:
                self.handle_packet({"from": self.local, "to": self.local, "decoded": {
                    "portnum": "ROUTING_APP", "requestId": pid, "routing": {"errorReason": "NONE"}}})
                time.sleep(r.uniform(0.5, 1.5) * len(fwd))
            self._deliver(target, self.local, "ROUTING_APP", request_id=pid, hop_start=hop_limit, path=back,
                          decoded_extra={"routing": {"errorReason": "NONE"}})
        elif kind == "traceroute":
            rd = mesh_pb2.RouteDiscovery()
            rd.route.extend(fwd[1:-1])
            rd.snr_towards.extend(int(self.truth[fwd[i]][fwd[i + 1]] * 4) for i in range(len(fwd) - 1))
            rd.route_back.extend(back[1:-1])
            rd.snr_back.extend(int(self.truth[back[i]][back[i + 1]] * 4) for i in range(len(back) - 2))
            self._deliver(target, self.local, "TRACEROUTE_APP", rd.SerializeToString(), request_id=pid,
                          hop_start=hop_limit, path=back)
        elif kind == "nodeinfo":
            self._deliver(target, self.local, "NODEINFO_APP", request_id=pid, hop_start=hop_limit, path=back,
                          decoded_extra={"user": {"id": f"!{target:08x}", "longName": n["name"], "shortName": n["short"],
                                                  "hwModel": n["hw"], "role": n["role"]}})
        elif kind == "position":
            pos = dict(self._pos_report(n), time=int(time.time())) if n["gps"] else {}
            self._deliver(target, self.local, "POSITION_APP", request_id=pid, hop_start=hop_limit, path=back,
                          decoded_extra={"position": pos})
        elif kind == "telemetry":
            self._deliver(target, self.local, "TELEMETRY_APP", request_id=pid, hop_start=hop_limit, path=back,
                          decoded_extra={"telemetry": {"deviceMetrics": {
                              "batteryLevel": r.randint(10, 101), "voltage": round(r.uniform(3.4, 4.2), 2),
                              "channelUtilization": round(r.uniform(2, 30), 1), "airUtilTx": round(r.uniform(0.1, 6), 2)}}})
