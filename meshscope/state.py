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
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

BROADCAST_NUM = 0xFFFFFFFF
MAX_JOBS = 200
MAX_LOG = 500
DB_VERSION = 1


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
        self._job_seq = 0
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
            entry = {"t": time.time(), "kind": kind, "text": text}
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
        self.merge_node(
            num,
            longName=user.get("longName"),
            shortName=user.get("shortName"),
            hwModel=user.get("hwModel"),
            role=user.get("role", "CLIENT") if user else None,
            lat=lat,
            lon=lon,
            alt=pos.get("altitude"),
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

    def forget_node(self, num: int):
        self.forget_nodes([num])

    def forget_nodes(self, nums):
        nums = set(nums)
        with self._lock:
            for num in nums:
                self.nodes.pop(num, None)
            for k in [k for k, l in self.links.items() if l["tx"] in nums or l["rx"] in nums]:
                del self.links[k]
            self.jobs = [j for j in self.jobs if j.get("target") not in nums]
            self._touch()

    def clear_history(self):
        with self._lock:
            keep = {self.local_num} if self.local_num is not None else set()
            self.nodes = {k: v for k, v in self.nodes.items() if k in keep}
            self.links.clear()
            self.jobs.clear()
            self.log.clear()
            self._touch()

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
                c["isLocal"] = n["num"] == self.local_num
                nodes.append(c)
            return {
                "version": self.version,
                "status": self.status,
                "statusDetail": self.status_detail,
                "port": self.port,
                "localNum": self.local_num,
                "hopLimit": self.hop_limit,
                "nodes": nodes,
                "edges": self.undirected_links(),
                "jobs": [{k: v for k, v in j.items() if k != "deadline"} for j in self.jobs],
                "log": list(self.log)[-200:],
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
            }
            self._dirty_since_save = False
        tmp = self.db_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
            tmp.replace(self.db_path)
        except Exception:
            log.exception("Could not save database")
