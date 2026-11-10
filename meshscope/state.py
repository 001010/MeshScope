"""Thread-safe store of everything MeshScope knows about the mesh.

Nodes are keyed by node number. Links are *directed* radio observations:
a link ``tx -> rx`` means ``rx`` was observed receiving ``tx`` directly
(with the SNR measured at ``rx`` when known). Links come from several sources:

* ``direct``      - our radio heard a packet with zero hops used
* ``traceroute``  - hops reported in a RouteDiscovery response
* ``neighborinfo``- a node broadcast the list of neighbours it hears
* ``relay``       - inferred from the ``relay_node`` byte of a received packet
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

BROADCAST_NUM = 0xFFFFFFFF
MAX_JOBS = 200
MAX_LOG = 600
MAX_MESSAGES = 2000
DB_VERSION = 1
MAX_POS_HISTORY = 60            # reported positions kept per node
ALT_WINDOW_S = 30 * 86400       # altitude reports used for the elevation estimate
ALT_NEAR_KM = 0.3               # ...and only those reported near the node's current position


def precision_error_m(bits) -> Optional[float]:
    """Approximate position error for Meshtastic's position precision (bits kept of the 32-bit coordinate).
    32 (or unset) = full precision; e.g. 16 bits ~ +-364 m, 13 bits ~ +-2.9 km."""
    if not bits or int(bits) >= 32:
        return None
    return 23905787.925 / (2 ** int(bits))

DEFAULT_SETTINGS: Dict[str, Any] = {
    "mqtt": {
        "enabled": False,
        "host": "mqtt.meshtastic.org",
        "port": 1883,
        "tls": False,
        "username": "meshdev",
        "password": "large4cats",
        "topics": ["msh/US/#"],
        "maxDistanceKm": 150,       # drop nodes known to be farther than this from your radio (0 = off)
        "strictDistance": False,    # also drop nodes whose position is unknown
        "channelKeys": [],          # [{"name": "...", "psk": "base64"}] extra keys for decrypting
        "uplink": False,            # publish RF packets that MQTT hasn't seen (opt-in)
        "uplinkRoot": "msh/US",
        "uplinkChannels": [],       # channel names explicitly allowed for uplink
        "uplinkDelay": 5,           # seconds to wait for another gateway to publish it first
    },
    "autoConnect": None,            # node number of a My Node to connect at startup
    "defaultAntennaHeight": 2.0,
    "ui": {"theme": "system", "units": "metric"},  # theme: system|day|night; units: metric|imperial
}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def node_id(num: int) -> str:
    return f"!{num & 0xFFFFFFFF:08x}"


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class MeshState:
    def __init__(self, db_path: Optional[Path] = None):
        self._lock = threading.RLock()
        self.db_path = db_path
        self.version = 0
        self.status = "disconnected"  # disconnected | connecting | connected | error
        self.status_detail = ""
        self.port: Optional[str] = None
        self.local_num: Optional[int] = None
        self.hop_limit = 3
        self.nodes: Dict[int, Dict[str, Any]] = {}
        self.links: Dict[str, Dict[str, Any]] = {}
        self.jobs: List[Dict[str, Any]] = []
        self.log: deque = deque(maxlen=MAX_LOG)
        self.favorites: set = set()
        self.my_nodes: Dict[int, Dict[str, Any]] = {}
        self.messages: deque = deque(maxlen=MAX_MESSAGES)
        self.channels: List[Dict[str, Any]] = []  # the connected radio's channels (no keys)
        self.settings: Dict[str, Any] = _deep_merge(DEFAULT_SETTINGS, {})
        self._job_seq = 0
        self._log_seq = 0
        self._msg_seq = 0
        self._links_ver = 0
        self._adj_cache = None
        self._dirty_since_save = False
        self.load()

    # ------------------------------------------------------------------ util
    def _touch(self):
        self.version += 1
        self._dirty_since_save = True

    def set_status(self, status: str, detail: str = "", port: Optional[str] = None):
        with self._lock:
            self.status = status
            self.status_detail = detail
            if port is not None or status == "disconnected":
                self.port = port
            self._touch()

    def add_log(self, kind: str, text: str, **extra):
        with self._lock:
            self._log_seq += 1
            entry = {"seq": self._log_seq, "t": time.time(), "kind": kind, "text": text}
            entry.update(extra)
            self.log.append(entry)
            self._touch()

    # ----------------------------------------------------------------- nodes
    def _node(self, num: int) -> Dict[str, Any]:
        n = self.nodes.get(num)
        if n is None:
            n = {"num": num, "id": node_id(num), "firstSeen": time.time()}
            self.nodes[num] = n
        return n

    def name_of(self, num: Optional[int]) -> str:
        if num is None:
            return "?"
        if num == BROADCAST_NUM:
            return "Unknown"
        n = self.nodes.get(num)
        if n:
            return n.get("longName") or n.get("shortName") or n["id"]
        return node_id(num)

    def merge_node(self, num: int, **fields):
        """Merge fields into a node; ``None`` values are ignored."""
        if num is None or num == BROADCAST_NUM or num == 0:
            return
        with self._lock:
            n = self._node(num)
            for k, v in fields.items():
                if v is not None:
                    n[k] = v
            self._touch()

    def merge_lib_node(self, d: Dict[str, Any]):
        """Merge a node dict in the shape produced by the meshtastic python library."""
        num = d.get("num")
        if num is None:
            return
        user = d.get("user") or {}
        pos = d.get("position") or {}
        dm = d.get("deviceMetrics") or {}
        lat, lon = pos.get("latitude"), pos.get("longitude")
        if lat is None and pos.get("latitudeI") is not None:
            lat = pos["latitudeI"] * 1e-7
            lon = pos.get("longitudeI", 0) * 1e-7
        if lat is not None and lon is not None and abs(lat) < 1e-6 and abs(lon) < 1e-6:
            lat = lon = None  # 0,0 means "no fix"
        with self._lock:
            existing = self.nodes.get(num, {})
            # import the radio's own favourites once; after that the app's star is authoritative
            if d.get("isFavorite") and not existing.get("deviceFavImported"):
                self.favorites.add(num)
                self.merge_node(num, deviceFavImported=True)
        self.merge_node(
            num,
            lastHeardRf=None if d.get("viaMqtt") else d.get("lastHeard"),
            longName=user.get("longName"),
            shortName=user.get("shortName"),
            hwModel=user.get("hwModel"),
            role=user.get("role", "CLIENT") if user else None,
            positionTime=pos.get("time"),
            lastHeard=d.get("lastHeard"),
            snr=d.get("snr"),
            hopsAway=d.get("hopsAway"),
            viaMqtt=d.get("viaMqtt"),
            battery=dm.get("batteryLevel"),
            voltage=dm.get("voltage"),
            chUtil=dm.get("channelUtilization"),
            airUtilTx=dm.get("airUtilTx"),
            uptime=dm.get("uptimeSeconds"),
        )
        if lat is not None and lon is not None:
            self.record_position(num, lat, lon, pos.get("altitude"), pos.get("precisionBits"), ts=pos.get("time"))

    def record_position(self, num: int, lat: float, lon: float, alt=None, precision_bits=None,
                        ts: Optional[float] = None):
        """Set a node's current position and keep a short history of what it reported (incl. altitude),
        which the coverage model uses for the antenna's elevation."""
        if num is None or num in (0, BROADCAST_NUM) or lat is None or lon is None:
            return
        now = time.time()
        ts = float(ts) if ts and 0 < float(ts) <= now + 300 else now
        alt = None if alt in (None, 0, 0.0) else round(float(alt), 1)  # 0 = firmware's "no altitude"
        bits = int(precision_bits) if precision_bits else None
        with self._lock:
            n = self._node(num)
            n["lat"], n["lon"] = lat, lon
            if alt is not None:
                n["alt"] = alt
            if bits is not None:
                n["precisionBits"] = bits
            h = n.setdefault("posHistory", [])
            last = h[-1] if h else None
            same = last and abs(last[1] - lat) < 1e-6 and abs(last[2] - lon) < 1e-6 and last[3] == alt
            if not same or ts - last[0] >= 900:  # identical reports are kept at most every 15 min
                h.append([round(ts), round(lat, 7), round(lon, 7), alt, bits])
                h.sort(key=lambda e: e[0])
                del h[:-MAX_POS_HISTORY]
            self._touch()

    @staticmethod
    def _elevation(n: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Median of the altitudes the node reported recently, near where it is now (metres above sea level)."""
        if n.get("lat") is None:
            return None
        cutoff = time.time() - ALT_WINDOW_S
        alts = [e[3] for e in n.get("posHistory", ()) if e[3] is not None and e[0] >= cutoff
                and haversine_km(e[1], e[2], n["lat"], n["lon"]) <= ALT_NEAR_KM]
        if not alts:
            return None
        return {"m": round(statistics.median(alts), 1), "reports": len(alts),
                "spread": round(max(alts) - min(alts), 1)}

    def coverage_inputs(self, num: int, max_link_age_s: float = 7 * 86400) -> Optional[Dict[str, Any]]:
        """Position, reported elevation and observed radio links of a node, for the terrain coverage model."""
        now = time.time()
        with self._lock:
            n = self.nodes.get(num)
            if not n or n.get("lat") is None:
                return None
            nbs = {}
            for l in self.links.values():
                if now - l["lastSeen"] > max_link_age_s or num not in (l["tx"], l["rx"]):
                    continue
                other = l["rx"] if l["tx"] == num else l["tx"]
                o = self.nodes.get(other)
                if not o or o.get("lat") is None:
                    continue
                e = nbs.setdefault(other, {"num": other, "lat": o["lat"], "lon": o["lon"],
                                           "posErrM": precision_error_m(o.get("precisionBits")),
                                           "snr": None, "heard": False})
                if l["tx"] == num:  # the neighbour heard this node: its SNR measures this node's signal
                    e["heard"] = True
                    if l.get("snr") is not None:
                        e["snr"] = l["snr"]
            return {"num": num, "lat": n["lat"], "lon": n["lon"], "antennaHeight": n.get("antennaHeight"),
                    "elevation": self._elevation(n), "posErrM": precision_error_m(n.get("precisionBits")),
                    "neighbors": list(nbs.values())}

    def forget_node(self, num: int):
        self.forget_nodes([num])

    def forget_nodes(self, nums):
        nums = set(nums)
        with self._lock:
            for num in nums:
                self.nodes.pop(num, None)
                self.favorites.discard(num)
            for k in [k for k, l in self.links.items() if l["tx"] in nums or l["rx"] in nums]:
                del self.links[k]
                self._links_ver += 1
            self.jobs = [j for j in self.jobs if j.get("target") not in nums]
            self._touch()

    # ------------------------------------------------- favourites & settings
    def set_favorite(self, num: int, fav: bool):
        with self._lock:
            (self.favorites.add if fav else self.favorites.discard)(num)
            self._touch()

    def update_settings(self, patch: Dict[str, Any]):
        with self._lock:
            merged = _deep_merge(self.settings, patch)
            # update in place: demo mode shares this dict with the real state
            self.settings.clear()
            self.settings.update(merged)
            self._touch()

    # ------------------------------------------------------------ my nodes
    def upsert_my_node(self, num: int, **fields):
        """Remember a radio we connected to, keyed by its node number (not by COM port)."""
        with self._lock:
            rec = self.my_nodes.get(num)
            if rec is None:
                rec = {"num": num, "id": node_id(num), "firstConnected": time.time(), "nickname": ""}
                self.my_nodes[num] = rec
            for k, v in fields.items():
                if v is not None:
                    rec[k] = v
            n = self.nodes.get(num, {})
            for k in ("longName", "shortName", "hwModel"):
                if n.get(k):
                    rec[k] = n[k]
            self._touch()
            return rec

    def remove_my_node(self, num: int):
        with self._lock:
            self.my_nodes.pop(num, None)
            if self.settings.get("autoConnect") == num:
                self.settings["autoConnect"] = None
            self._touch()

    def clear_history(self):
        with self._lock:
            keep = {self.local_num} if self.local_num is not None else set()
            self.nodes = {k: v for k, v in self.nodes.items() if k in keep}
            self.links.clear()
            self._links_ver += 1
            self.jobs.clear()
            self.log.clear()
            self._touch()

    # -------------------------------------------------------------- messages
    def add_message(self, **m) -> Dict[str, Any]:
        """Record a text message. The same packet (from, id) heard again - e.g. over RF after
        MQTT - is merged into the existing entry; RF wins as the source, so a message only
        keeps its MQTT badge if it was *only* received through the MQTT bridge."""
        with self._lock:
            pid, frm = m.get("packetId"), m.get("from")
            if pid and not m.get("outgoing"):
                for old in reversed(self.messages):
                    if old.get("packetId") == pid and old.get("from") == frm and not old.get("outgoing"):
                        srcs = set(old.get("sources") or [old.get("src")]) | {m.get("src")}
                        old["sources"] = sorted(s for s in srcs if s)
                        if m.get("src") in ("radio", "demo") and old.get("src") == "mqtt":
                            for k in ("src", "hops", "snr", "rssi", "channel", "channelName"):
                                if m.get(k) is not None:
                                    old[k] = m[k]
                            old.pop("gateway", None)
                        self._touch()
                        return old
            self._msg_seq += 1
            msg = {"seq": self._msg_seq, "t": time.time(), "status": None}
            msg.update(m)
            msg.setdefault("sources", [m.get("src")] if m.get("src") else [])
            self.messages.append(msg)
            self._touch()
            return msg

    def find_outgoing_message(self, packet_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            for m in reversed(self.messages):
                if m.get("outgoing") and m.get("packetId") == packet_id:
                    return m
        return None

    def update_message(self, msg: Dict[str, Any], **fields):
        with self._lock:
            msg.update(fields)
            self._touch()

    def set_channels(self, channels: List[Dict[str, Any]]):
        with self._lock:
            self.channels = [{k: v for k, v in c.items() if k != "key"} for c in channels]
            self._touch()

    def channel_index_by_name(self, name: Optional[str]) -> Optional[int]:
        for c in self.channels:
            if name and c.get("name") == name:
                return c["index"]
        return None

    # ----------------------------------------------------------------- links
    def add_link(self, tx: int, rx: int, snr: Optional[float], source: str, ts: Optional[float] = None):
        if tx is None or rx is None or tx == rx:
            return
        if BROADCAST_NUM in (tx, rx) or 0 in (tx, rx):
            return
        ts = ts or time.time()
        key = f"{tx}>{rx}"
        with self._lock:
            self._node(tx)
            self._node(rx)
            l = self.links.get(key)
            if l is None:
                l = {"tx": tx, "rx": rx, "snr": None, "firstSeen": ts, "lastSeen": ts, "count": 0, "sources": []}
                self.links[key] = l
                self._links_ver += 1
            if snr is not None:
                l["snr"] = round(float(snr), 2)
            l["lastSeen"] = max(l["lastSeen"], ts)
            l["count"] += 1
            if source not in l["sources"]:
                l["sources"].append(source)
            self._touch()

    # ------------------------------------------------------------------ jobs
    def new_job(self, kind: str, target: int, timeout: float, **extra) -> Dict[str, Any]:
        with self._lock:
            self._job_seq += 1
            job = {
                "id": self._job_seq,
                "kind": kind,
                "target": target,
                "targetName": self.name_of(target),
                "status": "pending",
                "started": time.time(),
                "finished": None,
                "deadline": time.time() + timeout,
                "packetId": None,
                "summary": "Sent, waiting for reply...",
                "result": None,
            }
            job.update(extra)
            self.jobs.insert(0, job)
            del self.jobs[MAX_JOBS:]
            self._touch()
            return job

    def update_job(self, job: Dict[str, Any], **fields):
        with self._lock:
            job.update(fields)
            if fields.get("status") in ("ok", "failed", "timeout"):
                job["finished"] = time.time()
            self._touch()

    def find_job_by_packet(self, packet_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            for j in self.jobs:
                if j.get("packetId") == packet_id and j["status"] in ("pending", "relayed"):
                    return j
        return None

    def expire_jobs(self):
        now = time.time()
        with self._lock:
            for j in self.jobs:
                if j["status"] in ("pending", "relayed") and now > j["deadline"]:
                    if j["status"] == "relayed":
                        self.update_job(j, status="timeout",
                                        summary="Only an implicit ACK (a relay repeated it); no reply from the destination")
                    else:
                        self.update_job(j, status="timeout", summary="No response before timeout")
                    self.add_log("job", f"{j['kind']} to {j['targetName']} timed out")
            for m in self.messages:
                if m.get("status") == "sending" and now - m["t"] > 90:
                    m["status"] = "noack"
                    self._touch()

    # ------------------------------------------------------------- analysis
    def _pos(self, num):
        n = self.nodes.get(num)
        if n and n.get("lat") is not None and n.get("lon") is not None:
            return n["lat"], n["lon"]
        return None

    def link_distance(self, a, b) -> Optional[float]:
        pa, pb = self._pos(a), self._pos(b)
        if pa and pb:
            return haversine_km(pa[0], pa[1], pb[0], pb[1])
        return None

    def _adjacency(self) -> Dict[int, set]:
        """Undirected adjacency of links seen in the last 24h (cached until links change)."""
        now = time.time()
        c = self._adj_cache
        if c and c[0] == self._links_ver and now - c[1] < 300:
            return c[2]
        adj: Dict[int, set] = {}
        for l in list(self.links.values()):
            if now - l["lastSeen"] > 86400:
                continue
            adj.setdefault(l["tx"], set()).add(l["rx"])
            adj.setdefault(l["rx"], set()).add(l["tx"])
        self._adj_cache = (self._links_ver, now, adj)
        return adj

    def _bfs(self, adj, src, dst, limit=8):
        if src == dst:
            return [src]
        prev = {src: None}
        frontier = [src]
        for _ in range(limit):
            nxt = []
            for u in frontier:
                for v in adj.get(u, ()):
                    if v not in prev:
                        prev[v] = u
                        if v == dst:
                            path = [v]
                            while prev[path[-1]] is not None:
                                path.append(prev[path[-1]])
                            return path[::-1]
                        nxt.append(v)
            frontier = nxt
            if not frontier:
                break
        return None

    def guess_path(self, src: int, dst: int, hops: Optional[int] = None, relay: Optional[int] = None):
        """Best guess at the hop-by-hop path a packet took from src to dst.

        Returns (path, known). ``known`` is True when the guess is consistent with the
        packet's hop count (and relay byte, if any); otherwise the path is a plausible
        route, or simply [src, (relay,) dst] when nothing better is known.
        """
        with self._lock:
            if hops == 0:
                return [src, dst], True
            adj = self._adjacency()
            if relay is not None:
                head = self._bfs(adj, src, relay)
                path = (head + [dst]) if head else [src, relay, dst]
            else:
                path = self._bfs(adj, src, dst) or [src, dst]
            known = hops is not None and len(path) - 2 == hops and all(
                path[i + 1] in adj.get(path[i], ()) for i in range(len(path) - 1))
            return path, known

    def undirected_links(self, max_age_s: Optional[float] = None) -> List[Dict[str, Any]]:
        """Collapse directed links into undirected edges for drawing."""
        now = time.time()
        out: Dict[tuple, Dict[str, Any]] = {}
        with self._lock:
            for l in self.links.values():
                if max_age_s and now - l["lastSeen"] > max_age_s:
                    continue
                a, b = sorted((l["tx"], l["rx"]))
                e = out.get((a, b))
                if e is None:
                    e = {"a": a, "b": b, "snrAB": None, "snrBA": None, "lastSeen": 0, "count": 0,
                         "sources": [], "distanceKm": self.link_distance(a, b)}
                    out[(a, b)] = e
                if l["tx"] == a:
                    e["snrAB"] = l["snr"]
                else:
                    e["snrBA"] = l["snr"]
                e["lastSeen"] = max(e["lastSeen"], l["lastSeen"])
                e["count"] += l["count"]
                for s in l["sources"]:
                    if s not in e["sources"]:
                        e["sources"].append(s)
        return list(out.values())

    def reach(self, origin: int, max_hops: int = 3, bidirectional: bool = True,
              max_age_s: Optional[float] = None, include_mqtt: bool = False) -> Dict[str, Any]:
        """Breadth-first flood from ``origin`` over observed radio links.

        Meshtastic floods packets: each hop is a rebroadcast by any node that
        heard the previous transmission, so the set of nodes reachable within
        ``max_hops`` rebroadcasts approximates where ``origin``'s messages land.
        Returns per-node hop counts, the BFS tree and coverage statistics.
        """
        now = time.time()
        with self._lock:
            adj: Dict[int, Dict[int, Optional[float]]] = {}
            for l in self.links.values():
                if max_age_s and now - l["lastSeen"] > max_age_s:
                    continue
                if not include_mqtt and (self.nodes.get(l["tx"], {}).get("viaMqtt") or
                                         self.nodes.get(l["rx"], {}).get("viaMqtt")):
                    continue
                adj.setdefault(l["tx"], {})[l["rx"]] = l["snr"]
                if bidirectional:
                    adj.setdefault(l["rx"], {}).setdefault(l["tx"], l["snr"])

            hops = {origin: 0}
            parent: Dict[int, int] = {}
            frontier = [origin]
            for h in range(1, max_hops + 1):
                nxt = []
                for u in frontier:
                    # visit stronger links first so the tree prefers good paths
                    for v, _snr in sorted(adj.get(u, {}).items(), key=lambda kv: -(kv[1] if kv[1] is not None else -99)):
                        if v not in hops:
                            hops[v] = h
                            parent[v] = u
                            nxt.append(v)
                frontier = nxt
                if not frontier:
                    break

            # neighbours one hop past the limit: they hear the last rebroadcast
            # but will not repeat it further
            beyond = set()
            for u in frontier:
                for v in adj.get(u, {}):
                    if v not in hops:
                        beyond.add(v)

            # a per-node coverage radius = the longest link it has been heard over
            radius: Dict[int, float] = {}
            for l in self.links.values():
                d = self.link_distance(l["tx"], l["rx"])
                if d is None:
                    continue
                radius[l["tx"]] = max(radius.get(l["tx"], 0.0), d)
                if bidirectional:
                    radius[l["rx"]] = max(radius.get(l["rx"], 0.0), d)

            per_hop: Dict[int, int] = {}
            for h in hops.values():
                per_hop[h] = per_hop.get(h, 0) + 1
            farthest = None
            op = self._pos(origin)
            if op:
                for n in hops:
                    p = self._pos(n)
                    if p and n != origin:
                        d = haversine_km(op[0], op[1], p[0], p[1])
                        if farthest is None or d > farthest["distanceKm"]:
                            farthest = {"num": n, "name": self.name_of(n), "distanceKm": round(d, 2)}

            return {
                "origin": origin,
                "originName": self.name_of(origin),
                "maxHops": max_hops,
                "hops": {str(k): v for k, v in hops.items()},
                "tree": [{"parent": p, "child": c} for c, p in parent.items()],
                "beyond": sorted(beyond),
                "radiusKm": {str(k): round(v, 3) for k, v in radius.items() if k in hops},
                "perHop": {str(k): v for k, v in sorted(per_hop.items())},
                "reachable": len(hops) - 1,
                "farthest": farthest,
                "hasLinks": origin in adj,
            }

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> Dict[str, Any]:
        self.expire_jobs()
        with self._lock:
            nodes = []
            for n in self.nodes.values():
                c = dict(n)
                c.pop("posHistory", None)  # can be long; the UI gets a summary
                c["elevation"] = self._elevation(n)
                c["positionReports"] = len(n.get("posHistory", ()))
                c["posErrM"] = precision_error_m(n.get("precisionBits"))
                c["isLocal"] = n["num"] == self.local_num
                c["favorite"] = n["num"] in self.favorites
                nodes.append(c)
            settings = _deep_merge(self.settings, {})
            if settings["mqtt"].get("password"):
                settings["mqtt"]["password"] = "********"
            return {
                "version": self.version,
                "logSeq": self._log_seq,
                "settings": settings,
                "myNodes": list(self.my_nodes.values()),
                "status": self.status,
                "statusDetail": self.status_detail,
                "port": self.port,
                "localNum": self.local_num,
                "hopLimit": self.hop_limit,
                "nodes": nodes,
                "edges": self.undirected_links(),
                "jobs": [{k: v for k, v in j.items() if k != "deadline"} for j in self.jobs],
                "log": list(self.log)[-200:],
                "channels": list(self.channels),
                "messages": list(self.messages)[-400:],
                "msgSeq": self._msg_seq,
            }

    # ---------------------------------------------------------- persistence
    def load(self):
        if not self.db_path or not self.db_path.exists():
            return
        try:
            data = json.loads(self.db_path.read_text(encoding="utf-8"))
            if data.get("dbVersion") != DB_VERSION:
                return
            self.nodes = {int(n["num"]): n for n in data.get("nodes", [])}
            self.links = {f"{l['tx']}>{l['rx']}": l for l in data.get("links", [])}
            self.jobs = [j for j in data.get("jobs", []) if j.get("status") not in ("pending", "relayed")]
            self._job_seq = max([j["id"] for j in self.jobs] + [0])
            self.favorites = set(int(x) for x in data.get("favorites", []))
            self.my_nodes = {int(m["num"]): m for m in data.get("myNodes", [])}
            self.settings = _deep_merge(DEFAULT_SETTINGS, data.get("settings", {}))
            for m in data.get("messages", []):
                if m.get("status") == "sending":
                    m["status"] = "noack"
                self.messages.append(m)
            self._msg_seq = max([m.get("seq", 0) for m in self.messages] + [0])
            log.info("Loaded %d nodes, %d links from %s", len(self.nodes), len(self.links), self.db_path)
        except Exception:
            log.exception("Could not load database %s", self.db_path)

    def save(self, force: bool = False):
        if not self.db_path:
            return
        with self._lock:
            if not (self._dirty_since_save or force):
                return
            data = {
                "dbVersion": DB_VERSION,
                "saved": time.time(),
                "nodes": list(self.nodes.values()),
                "links": list(self.links.values()),
                "jobs": self.jobs[:100],
                "favorites": sorted(self.favorites),
                "myNodes": list(self.my_nodes.values()),
                "settings": self.settings,
                "messages": list(self.messages)[-1000:],
            }
            self._dirty_since_save = False
        tmp = self.db_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
            tmp.replace(self.db_path)
        except Exception:
            log.exception("Could not save database")
