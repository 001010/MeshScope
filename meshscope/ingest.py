"""Turning received packets (from the USB radio, the demo or MQTT) into MeshState updates."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

from .state import BROADCAST_NUM, MeshState

log = logging.getLogger(__name__)

UNKNOWN_SNR = -128


def strip_raw(obj):
    """Deep-copy a packet dict without the protobuf 'raw' objects (for addons / JSON)."""
    if isinstance(obj, dict):
        return {k: strip_raw(v) for k, v in obj.items() if k != "raw"}
    if isinstance(obj, list):
        return [strip_raw(v) for v in obj]
    return obj


def packet_to_dict(mp) -> Dict[str, Any]:
    """Build the same dict shape the meshtastic library produces, from a MeshPacket protobuf."""
    from google.protobuf.json_format import MessageToDict
    from meshtastic import protocols

    d = MessageToDict(mp)
    d["raw"] = mp
    d.setdefault("from", 0)
    d.setdefault("to", 0)
    if mp.HasField("decoded"):
        dec = d.setdefault("decoded", {})
        dec["payload"] = mp.decoded.payload
        dec.setdefault("portnum", "UNKNOWN_APP")
        h = protocols.get(mp.decoded.portnum)
        if h is not None:
            if h.protobufFactory is not None:
                try:
                    pb = h.protobufFactory()
                    pb.ParseFromString(mp.decoded.payload)
                    sub = MessageToDict(pb)
                    if h.name == "position" and "latitudeI" in sub:
                        sub["latitude"] = sub["latitudeI"] * 1e-7
                        sub["longitude"] = sub.get("longitudeI", 0) * 1e-7
                    dec[h.name] = sub
                except Exception:
                    pass
            elif h.name in ("text", "rangetest", "detectionsensor"):
                dec["text"] = mp.decoded.payload.decode("utf-8", "replace")
    return d


class SeenTracker:
    """Remembers (from, id) of recent packets and where each was seen (radio / mqtt / demo)."""

    def __init__(self, size: int = 20000):
        self._d: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            return self._d.get(key)

    def mark(self, key, source: str) -> Dict[str, Any]:
        """Record a sighting; returns the record with ``first`` True on the first sighting."""
        with self._lock:
            rec = self._d.get(key)
            if rec is None:
                rec = {"first": True, "firstSource": source, "sources": {source}, "t": time.time(), "published": False}
                self._d[key] = rec
                if len(self._d) > self._size:
                    self._d.popitem(last=False)
            else:
                rec["first"] = False
                rec["sources"].add(source)
            return rec


class PacketProcessor:
    """Decodes packets and records nodes, links and log entries into a MeshState."""

    SOURCE = "radio"

    def __init__(self, state: MeshState):
        self.state = state
        # on_packet(packet, source, info) is called after every processed packet
        self.on_packet: Optional[Callable[[Dict[str, Any], str, Dict[str, Any]], None]] = None

    # ------------------------------------------------------------------ main
    def handle_packet(self, p: Dict[str, Any], source: Optional[str] = None, receiver: Optional[int] = None):
        st = self.state
        source = source or self.SOURCE
        frm = p.get("from")
        to = p.get("to")
        if frm is None:
            return
        local = st.local_num
        receiver = receiver if receiver is not None else local
        decoded = p.get("decoded") or {}
        port = decoded.get("portnum", "ENCRYPTED" if "encrypted" in p else "UNKNOWN")
        rx_time = p.get("rxTime") or time.time()
        if rx_time > time.time() + 60:  # gateways with bad clocks
            rx_time = time.time()
        snr = p.get("rxSnr")
        rssi = p.get("rxRssi")
        via_mqtt = bool(p.get("viaMqtt"))
        hop_start = p.get("hopStart")
        hop_limit = p.get("hopLimit", 0)
        hops = (hop_start - hop_limit) if hop_start else None
        relay = None

        if source == "mqtt":
            if frm != local:
                prev = st.nodes.get(frm, {})
                st.merge_node(frm, lastHeardMqtt=rx_time, lastHeard=max(rx_time, prev.get("lastHeard") or 0))
            if hops == 0 and receiver is not None:
                st.add_link(frm, receiver, snr, "mqtt", ts=rx_time)
        elif frm != local:
            fields = dict(lastHeard=rx_time, viaMqtt=via_mqtt)
            if not via_mqtt:
                fields["lastHeardRf"] = rx_time
            if hops is not None:
                fields["hopsAway"] = hops
            if hops == 0:
                fields["snr"] = snr
                fields["rssi"] = rssi
            st.merge_node(frm, **fields)
            if local is not None and not via_mqtt and hops is not None:
                if hops == 0:
                    st.add_link(frm, local, snr, "direct", ts=rx_time)
                else:
                    relay = self._resolve_relay(p.get("relayNode"))
                    if relay is not None and relay != frm:
                        st.add_link(relay, local, snr, "relay", ts=rx_time)
                        if hops == 1:
                            st.add_link(frm, relay, None, "relay", ts=rx_time)

        # best guess of the path the packet took, for the live flow animation
        path, known = None, False
        if frm != receiver and receiver is not None and not via_mqtt:
            path, known = st.guess_path(frm, receiver, hops, relay)

        text = decoded.get("text")
        desc = f"{port} from {st.name_of(frm)}"
        if to not in (BROADCAST_NUM, None, 0):
            desc += f" to {st.name_of(to)}"
        if source == "mqtt":
            desc += f" (MQTT via {st.name_of(receiver)})"
        if text:
            desc += f': "{text[:200]}"'
        st.add_log("rx", desc, port=port, frm=frm, to=to, snr=snr, rssi=rssi, hops=hops,
                   mqtt=via_mqtt or source == "mqtt", src=source, path=path, known=known)

        if port == "TEXT_MESSAGE_APP" and text is not None and frm != local:
            if source == "mqtt":
                ch_name = p.get("channelId")
                ch_idx = st.channel_index_by_name(ch_name)
            else:
                ch_idx = int(p.get("channel") or 0)
                ch_name = next((c["name"] for c in st.channels if c["index"] == ch_idx), None)
            dm = to not in (BROADCAST_NUM, None, 0) and to == local
            st.add_message(packetId=p.get("id"), t=rx_time, **{"from": frm}, to=to, text=text, src=source,
                           dm=dm, channel=None if dm else ch_idx, channelName=None if dm else ch_name,
                           hops=hops, snr=snr if source != "mqtt" else None,
                           rssi=rssi if source != "mqtt" else None,
                           gateway=receiver if source == "mqtt" else None)

        try:
            if port == "TRACEROUTE_APP":
                self._handle_traceroute(p, decoded, source)
            elif port == "NEIGHBORINFO_APP":
                self._handle_neighborinfo(p, decoded, source)
            elif port == "TELEMETRY_APP":
                self._handle_telemetry(p, decoded)
            elif port == "POSITION_APP":
                self._handle_position(p, decoded)
            elif port == "NODEINFO_APP":
                self._handle_nodeinfo(p, decoded)
        except Exception:
            log.exception("decoder for %s failed", port)

        self.after_packet(p, decoded, port, source)

        if self.on_packet is not None:
            try:
                self.on_packet(p, source, {"path": path, "known": known, "hops": hops, "receiver": receiver,
                                           "port": port, "text": text})
            except Exception:
                log.exception("on_packet hook failed")

    def after_packet(self, p, decoded, port, source):
        """Hook for subclasses (request/response matching)."""

    def _resolve_relay(self, relay_byte) -> Optional[int]:
        """Map the 1-byte relay_node hint onto a unique node we hear directly."""
        if not relay_byte:
            return None
        st = self.state
        cands = [n for n, d in list(st.nodes.items())
                 if (n & 0xFF) == relay_byte and n != st.local_num and d.get("hopsAway") == 0]
        return cands[0] if len(cands) == 1 else None

    # ---- per-port decoders -------------------------------------------------
    def _handle_traceroute(self, p, decoded, source):
        from meshtastic.protobuf import mesh_pb2

        payload = decoded.get("payload")
        if not payload or decoded.get("requestId") is None:
            return  # a request passing through, not a reply
        rd = mesh_pb2.RouteDiscovery()
        rd.ParseFromString(payload)
        origin, dest = p.get("to"), p.get("from")
        st = self.state
        ts = p.get("rxTime") or time.time()

        def scale(vals):
            return [None if v == UNKNOWN_SNR else v / 4.0 for v in vals]

        fwd = [origin] + list(rd.route) + [dest]
        fsnr = scale(rd.snr_towards)
        for i in range(len(fwd) - 1):
            st.add_link(fwd[i], fwd[i + 1], fsnr[i] if i < len(fsnr) else None, "traceroute", ts=ts)

        back = None
        bsnr: List[Optional[float]] = []
        if p.get("hopStart") is not None:
            back = [dest] + list(rd.route_back) + [origin]
            bsnr = scale(rd.snr_back)
            # the final hop back is measured by the receiver of the response itself
            if source != "mqtt" and len(bsnr) < len(back) - 1 and p.get("rxSnr") is not None:
                bsnr.append(p.get("rxSnr"))
            for i in range(len(back) - 1):
                st.add_link(back[i], back[i + 1], bsnr[i] if i < len(bsnr) else None, "traceroute", ts=ts)

        if source != "mqtt" and origin == st.local_num:
            st.merge_node(dest, hopsAway=len(rd.route))
        decoded["_meshscope_traceroute"] = {
            "towards": [{"num": n, "name": st.name_of(n), "snr": (fsnr[i - 1] if 0 < i <= len(fsnr) else None)}
                        for i, n in enumerate(fwd)],
            "back": None if back is None else
                    [{"num": n, "name": st.name_of(n), "snr": (bsnr[i - 1] if 0 < i <= len(bsnr) else None)}
                     for i, n in enumerate(back)],
        }

    def _handle_neighborinfo(self, p, decoded, source):
        from meshtastic.protobuf import mesh_pb2

        payload = decoded.get("payload")
        if not payload:
            return
        ni = mesh_pb2.NeighborInfo()
        ni.ParseFromString(payload)
        reporter = ni.node_id or p.get("from")
        ts = p.get("rxTime") or time.time()
        for nb in ni.neighbors:
            self.state.add_link(nb.node_id, reporter, nb.snr, "neighborinfo", ts=ts)

    def _handle_telemetry(self, p, decoded):
        dm = (decoded.get("telemetry") or {}).get("deviceMetrics")
        if dm:
            self.state.merge_node(p["from"], battery=dm.get("batteryLevel"), voltage=dm.get("voltage"),
                                  chUtil=dm.get("channelUtilization"), airUtilTx=dm.get("airUtilTx"),
                                  uptime=dm.get("uptimeSeconds"))

    def _handle_position(self, p, decoded):
        pos = decoded.get("position") or {}
        lat, lon = pos.get("latitude"), pos.get("longitude")
        if lat is None and pos.get("latitudeI") is not None:
            lat, lon = pos["latitudeI"] * 1e-7, pos.get("longitudeI", 0) * 1e-7
        if lat is not None and lon is not None and not (abs(lat) < 1e-6 and abs(lon) < 1e-6):
            self.state.record_position(p["from"], lat, lon, pos.get("altitude"), pos.get("precisionBits"))
            self.state.merge_node(p["from"], positionTime=pos.get("time") or time.time())

    def _handle_nodeinfo(self, p, decoded):
        u = decoded.get("user") or {}
        if u:
            self.state.merge_node(p["from"], longName=u.get("longName"), shortName=u.get("shortName"),
                                  hwModel=u.get("hwModel"), role=u.get("role", "CLIENT"))
