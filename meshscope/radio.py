"""USB (serial) connection to a Meshtastic node and the request/response actions."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from google.protobuf import json_format
from pubsub import pub

from .state import BROADCAST_NUM, MeshState

log = logging.getLogger(__name__)

# USB vendor ids commonly used by Meshtastic hardware
KNOWN_VIDS = {
    0x10C4: "Silicon Labs CP210x",
    0x1A86: "WCH CH340/CH9102",
    0x303A: "Espressif ESP32-S3",
    0x239A: "Adafruit nRF52 (RAK/T-Echo)",
    0x2886: "Seeed",
    0x0403: "FTDI",
    0x2E8A: "Raspberry Pi RP2040",
}

UNKNOWN_SNR = -128


def list_ports() -> List[Dict[str, Any]]:
    from serial.tools import list_ports as lp

    out = []
    for p in lp.comports():
        likely = p.vid in KNOWN_VIDS
        out.append({
            "device": p.device,
            "description": p.description,
            "manufacturer": p.manufacturer,
            "vid": p.vid,
            "pid": p.pid,
            "chip": KNOWN_VIDS.get(p.vid),
            "likelyMeshtastic": likely,
        })
    out.sort(key=lambda d: (not d["likelyMeshtastic"], d["device"]))
    return out


class Radio:
    """Owns the meshtastic SerialInterface and translates packets into MeshState updates."""

    ACTIONS = ("traceroute", "ping", "nodeinfo", "position", "telemetry")

    def __init__(self, state: MeshState):
        self.state = state
        self.iface = None
        self._send_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._subscribed = False
        self._connecting = False

    # ------------------------------------------------------------ lifecycle
    def _subscribe(self):
        if self._subscribed:
            return
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_established, "meshtastic.connection.established")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        pub.subscribe(self._on_node_updated, "meshtastic.node.updated")
        self._subscribed = True

    @property
    def connected(self) -> bool:
        return self.iface is not None and self.state.status == "connected"

    def connect(self, port: str):
        """Connect in a background thread (the library blocks while it downloads the node DB)."""
        if not self._connect_lock.acquire(blocking=False):
            raise RuntimeError("A connection attempt is already in progress")
        self.disconnect()
        self.state.set_status("connecting", f"Opening {port}...", port=port)
        threading.Thread(target=self._connect_worker, args=(port,), daemon=True, name="connect").start()

    def _connect_worker(self, port: str):
        try:
            import meshtastic.serial_interface

            self._subscribe()
            self._connecting = True
            iface = meshtastic.serial_interface.SerialInterface(devPath=port, timeout=60)
            self.iface = iface
            self._connecting = False
            self._import_device_state()
            self.state.set_status("connected", "", port=port)
            self.state.add_log("conn", f"Connected to {self.state.name_of(self.state.local_num)} on {port}")
        except (Exception, SystemExit) as e:  # noqa: BLE001 - the library sys.exit()s on some errors
            log.exception("Connect failed")
            self.iface = None
            msg = str(e) or e.__class__.__name__
            if "PermissionError" in repr(e) or "Access is denied" in msg:
                msg = f"{port} is in use by another program (close the Meshtastic app / other serial monitors)."
            self.state.set_status("error", msg, port=port)
            self.state.add_log("error", f"Connect to {port} failed: {msg}")
        finally:
            self._connecting = False
            self._connect_lock.release()

    def _import_device_state(self):
        iface = self.iface
        st = self.state
        if iface.myInfo is not None:
            st.local_num = iface.myInfo.my_node_num
        try:
            st.hop_limit = int(iface.localNode.localConfig.lora.hop_limit) or 3
        except Exception:
            st.hop_limit = 3
        now = time.time()
        for d in (iface.nodesByNum or {}).values():
            st.merge_lib_node(d)
            # Nodes the radio itself reports at 0 hops are direct RF neighbours
            if (d.get("hopsAway") == 0 and d.get("num") != st.local_num and not d.get("viaMqtt")
                    and d.get("lastHeard") and st.local_num is not None):
                st.add_link(d["num"], st.local_num, d.get("snr"), "nodedb", ts=min(now, d["lastHeard"]))
        st.merge_node(st.local_num, hopsAway=0, lastHeard=now)

    def disconnect(self):
        iface, self.iface = self.iface, None
        if iface is not None:
            try:
                iface.close()
            except Exception:
                log.exception("Error closing interface")
            self.state.add_log("conn", "Disconnected")
        if self.state.status != "disconnected":
            self.state.set_status("disconnected")

    # ------------------------------------------------------- pubsub handlers
    def _on_established(self, interface, topic=pub.AUTO_TOPIC):
        pass

    def _on_lost(self, interface, topic=pub.AUTO_TOPIC):
        if interface is self.iface:
            self.iface = None
            self.state.set_status("error", "Connection to the radio was lost (cable unplugged?)")
            self.state.add_log("error", "Connection lost")

    def _owns(self, interface) -> bool:
        if interface is None or interface is self.iface:
            return True
        # while connecting, the library publishes before SerialInterface() returns
        return self.iface is None and self._connecting

    def _on_node_updated(self, node, interface=None):
        if not self._owns(interface):
            return
        try:
            self.state.merge_lib_node(node)
        except Exception:
            log.exception("node.updated handling failed")

    def _on_receive(self, packet, interface=None):
        if not self._owns(interface):
            return  # a packet from a connection this Radio doesn't own
        try:
            self.handle_packet(packet)
        except Exception:
            log.exception("Packet handling failed: %r", {k: v for k, v in packet.items() if k != "raw"})

    # ------------------------------------------------------ packet handling
    def handle_packet(self, p: Dict[str, Any]):
        st = self.state
        frm = p.get("from")
        to = p.get("to")
        if frm is None:
            return
        local = st.local_num
        decoded = p.get("decoded") or {}
        port = decoded.get("portnum", "ENCRYPTED" if "encrypted" in p else "UNKNOWN")
        rx_time = p.get("rxTime") or time.time()
        snr = p.get("rxSnr")
        rssi = p.get("rxRssi")
        via_mqtt = bool(p.get("viaMqtt"))
        hop_start = p.get("hopStart")
        hop_limit = p.get("hopLimit", 0)
        hops = (hop_start - hop_limit) if hop_start else None

        if frm != local:
            fields = dict(lastHeard=rx_time, viaMqtt=via_mqtt)
            if hops is not None:
                fields["hopsAway"] = hops
            if hops == 0:
                fields["snr"] = snr
                fields["rssi"] = rssi
            st.merge_node(frm, **fields)

            # ---- link inference from how the packet reached us
            if local is not None and not via_mqtt and hops is not None:
                if hops == 0:
                    st.add_link(frm, local, snr, "direct", ts=rx_time)
                else:
                    relay = self._resolve_relay(p.get("relayNode"))
                    if relay is not None and relay != frm:
                        st.add_link(relay, local, snr, "relay", ts=rx_time)
                        if hops == 1:
                            st.add_link(frm, relay, None, "relay", ts=rx_time)

        st.add_log("rx", f"{port} from {st.name_of(frm)}"
                   + (f" to {st.name_of(to)}" if to not in (BROADCAST_NUM, None) else ""),
                   port=port, frm=frm, to=to, snr=snr, rssi=rssi, hops=hops, mqtt=via_mqtt)

        if port == "TRACEROUTE_APP":
            self._handle_traceroute(p, decoded)
        elif port == "NEIGHBORINFO_APP":
            self._handle_neighborinfo(p, decoded)
        elif port == "TELEMETRY_APP":
            self._handle_telemetry(p, decoded)
        elif port == "POSITION_APP":
            self._handle_position(p, decoded)
        elif port == "NODEINFO_APP":
            self._handle_nodeinfo(p, decoded)

        req = decoded.get("requestId")
        if req is not None:
            self._resolve_job(p, decoded, int(req), port)

    def _resolve_relay(self, relay_byte) -> Optional[int]:
        """Map the 1-byte relay_node hint onto a unique node we hear directly."""
        if not relay_byte:
            return None
        st = self.state
        cands = [n for n, d in st.nodes.items()
                 if (n & 0xFF) == relay_byte and n != st.local_num and d.get("hopsAway") == 0]
        if len(cands) == 1:
            return cands[0]
        return None

    # ---- per-port decoders -------------------------------------------------
    def _handle_traceroute(self, p, decoded):
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
            # the final hop back to us is measured locally on the response packet itself
            if len(bsnr) < len(back) - 1 and p.get("rxSnr") is not None:
                bsnr.append(p.get("rxSnr"))
            for i in range(len(back) - 1):
                st.add_link(back[i], back[i + 1], bsnr[i] if i < len(bsnr) else None, "traceroute", ts=ts)

        st.merge_node(dest, hopsAway=len(rd.route))
        result = {
            "towards": [{"num": n, "name": st.name_of(n), "snr": (fsnr[i - 1] if 0 < i <= len(fsnr) else None)}
                        for i, n in enumerate(fwd)],
            "back": None if back is None else
                    [{"num": n, "name": st.name_of(n), "snr": (bsnr[i - 1] if 0 < i <= len(bsnr) else None)}
                     for i, n in enumerate(back)],
        }
        decoded["_meshscope_traceroute"] = result

    def _handle_neighborinfo(self, p, decoded):
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
        t = decoded.get("telemetry") or {}
        dm = t.get("deviceMetrics")
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
            self.state.merge_node(p["from"], lat=lat, lon=lon, alt=pos.get("altitude"),
                                  positionTime=pos.get("time") or time.time())

    def _handle_nodeinfo(self, p, decoded):
        u = decoded.get("user") or {}
        if u:
            self.state.merge_node(p["from"], longName=u.get("longName"), shortName=u.get("shortName"),
                                  hwModel=u.get("hwModel"), role=u.get("role", "CLIENT"))

    # ---- request/response matching ----------------------------------------
    def _resolve_job(self, p, decoded, req: int, port: str):
        st = self.state
        job = st.find_job_by_packet(req)
        if job is None:
            return
        elapsed = round(time.time() - job["started"], 2)
        frm = p.get("from")

        if port == "ROUTING_APP":
            err = (decoded.get("routing") or {}).get("errorReason", "NONE")
            if err == "NONE":
                if job["kind"] != "ping":
                    return  # plain ACK for a request that expects a data reply
                if frm == st.local_num:
                    st.update_job(job, status="relayed",
                                  summary=f"Implicit ACK after {elapsed}s: a neighbour rebroadcast it; waiting for the destination...")
                    return
                st.update_job(job, status="ok", rtt=elapsed,
                              summary=f"Reply from {st.name_of(frm)} in {elapsed}s"
                                      + (f" (SNR {p['rxSnr']} dB)" if p.get("rxSnr") is not None else ""),
                              result={"rtt": elapsed, "snr": p.get("rxSnr"), "rssi": p.get("rxRssi"),
                                      "hops": (p["hopStart"] - p.get("hopLimit", 0)) if p.get("hopStart") else None})
                st.add_log("job", f"ping {job['targetName']}: {elapsed}s")
                return
            pretty = {
                "MAX_RETRANSMIT": "no ACK after max retransmissions",
                "NO_RESPONSE": "the destination has no handler for this request",
                "NO_ROUTE": "no route to the destination",
                "NO_CHANNEL": "destination is not on a shared channel",
                "TIMEOUT": "timed out",
                "RATE_LIMIT_EXCEEDED": "rate limited by the firmware - wait ~30s and retry",
                "PKI_UNKNOWN_PUBKEY": "destination's public key is unknown (request nodeinfo first)",
            }.get(err, err)
            st.update_job(job, status="failed", summary=f"Failed: {pretty}", error=err)
            st.add_log("job", f"{job['kind']} to {job['targetName']} failed: {err}")
            return

        kind = job["kind"]
        if kind == "traceroute" and port == "TRACEROUTE_APP":
            res = decoded.get("_meshscope_traceroute")
            if res:
                hops_to = len(res["towards"]) - 2
                path = " > ".join(h["name"] for h in res["towards"])
                how = "Direct (no relays)" if hops_to == 0 else f"{hops_to} relay hop(s)"
                st.update_job(job, status="ok", result=res, rtt=elapsed,
                              summary=f"{how} in {elapsed}s: {path}")
                st.add_log("job", f"traceroute {job['targetName']}: {path}")
        elif kind == "nodeinfo" and port == "NODEINFO_APP":
            u = {k: v for k, v in (decoded.get("user") or {}).items() if k != "raw"}
            st.update_job(job, status="ok", rtt=elapsed, result=u,
                          summary=f"{u.get('longName', '?')} ({u.get('shortName', '?')}), {u.get('hwModel', '?')}, role {u.get('role', 'CLIENT')}")
        elif kind == "position" and port == "POSITION_APP":
            pos = {k: v for k, v in (decoded.get("position") or {}).items() if k != "raw"}
            lat, lon = pos.get("latitude"), pos.get("longitude")
            summ = f"{lat:.5f}, {lon:.5f}" if lat is not None and lon is not None else "Replied without a position fix"
            st.update_job(job, status="ok", rtt=elapsed, result=pos, summary=summ)
        elif kind == "telemetry" and port == "TELEMETRY_APP":
            dm = ((decoded.get("telemetry") or {}).get("deviceMetrics")) or {}
            parts = []
            if "batteryLevel" in dm:
                parts.append(f"battery {dm['batteryLevel']}%")
            if "voltage" in dm:
                parts.append(f"{dm['voltage']:.2f} V")
            if "channelUtilization" in dm:
                parts.append(f"ch util {dm['channelUtilization']:.1f}%")
            if "airUtilTx" in dm:
                parts.append(f"air tx {dm['airUtilTx']:.1f}%")
            st.update_job(job, status="ok", rtt=elapsed, result=dm, summary=", ".join(parts) or "Replied")

    # -------------------------------------------------------------- actions
    def run_action(self, kind: str, target: int, hop_limit: Optional[int] = None) -> Dict[str, Any]:
        if kind not in self.ACTIONS:
            raise ValueError(f"Unknown action {kind}")
        if not self.connected:
            raise RuntimeError("Not connected to a radio")
        st = self.state
        if target == st.local_num:
            raise ValueError("That is the locally connected node")
        hl = hop_limit or st.hop_limit
        timeout = {"traceroute": 45 + 20 * hl}.get(kind, 60)
        job = st.new_job(kind, target, timeout, hopLimit=hl)
        try:
            pkt = self._send(kind, target, hl)
            st.update_job(job, packetId=pkt.id)
            st.add_log("tx", f"{kind} request to {st.name_of(target)} (hop limit {hl})")
        except Exception as e:
            st.update_job(job, status="failed", summary=f"Send failed: {e}")
            raise
        return job

    def _send(self, kind: str, target: int, hop_limit: int):
        from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2

        P = portnums_pb2.PortNum
        iface = self.iface
        with self._send_lock:
            if kind == "traceroute":
                return iface.sendData(mesh_pb2.RouteDiscovery(), destinationId=target,
                                      portNum=P.TRACEROUTE_APP, wantResponse=True, hopLimit=hop_limit)
            if kind == "ping":
                # Meshtastic has no ICMP: a want_ack packet to a port with no handler makes the
                # destination return a routing ACK, which gives us a round-trip time.
                return iface.sendData(b"meshscope-ping", destinationId=target, portNum=P.PRIVATE_APP,
                                      wantAck=True, hopLimit=hop_limit)
            if kind == "nodeinfo":
                # The protocol is "here is my User, send me yours".
                user = mesh_pb2.User()
                me = iface.getMyUser() or {}
                json_format.ParseDict({k: v for k, v in me.items() if k != "raw"}, user, ignore_unknown_fields=True)
                return iface.sendData(user, destinationId=target, portNum=P.NODEINFO_APP,
                                      wantResponse=True, hopLimit=hop_limit)
            if kind == "position":
                return iface.sendData(mesh_pb2.Position(), destinationId=target, portNum=P.POSITION_APP,
                                      wantResponse=True, hopLimit=hop_limit)
            if kind == "telemetry":
                t = telemetry_pb2.Telemetry()
                t.device_metrics.CopyFrom(telemetry_pb2.DeviceMetrics())
                return iface.sendData(t, destinationId=target, portNum=P.TELEMETRY_APP,
                                      wantResponse=True, hopLimit=hop_limit)
        raise ValueError(kind)
