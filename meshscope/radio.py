"""USB (serial) connection to a Meshtastic node and the request/response actions."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from google.protobuf import json_format
from pubsub import pub

from .ingest import PacketProcessor
from .meshcrypto import expand_psk, preset_channel_name
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


def list_ports() -> List[Dict[str, Any]]:
    from serial.tools import list_ports as lp

    out = []
    for p in lp.comports():
        out.append({
            "device": p.device,
            "description": p.description,
            "manufacturer": p.manufacturer,
            "vid": p.vid,
            "pid": p.pid,
            "serial": p.serial_number,
            "location": p.location,
            "chip": KNOWN_VIDS.get(p.vid),
            "likelyMeshtastic": p.vid in KNOWN_VIDS,
        })
    out.sort(key=lambda d: (not d["likelyMeshtastic"], d["device"]))
    return out


class _NotTheRadio(Exception):
    pass


class Radio(PacketProcessor):
    """Owns the meshtastic SerialInterface; packets flow through PacketProcessor into MeshState."""

    ACTIONS = ("traceroute", "ping", "nodeinfo", "position", "telemetry")
    SOURCE = "radio"

    def __init__(self, state: MeshState):
        super().__init__(state)
        self.iface = None
        self.port: Optional[str] = None
        self._send_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._subscribed = False
        self._connecting = False
        # hooks set by the App
        self.on_connected: Optional[Callable[[str, Any], None]] = None
        self.on_log_line: Optional[Callable[[str], None]] = None

    # ------------------------------------------------------------ lifecycle
    def _subscribe(self):
        if self._subscribed:
            return
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        pub.subscribe(self._on_node_updated, "meshtastic.node.updated")
        pub.subscribe(self._on_log_line, "meshtastic.log.line")
        self._subscribed = True

    @property
    def connected(self) -> bool:
        return self.iface is not None and self.state.status == "connected"

    def connect(self, ports: Sequence[str] | str, expect_num: Optional[int] = None, label: Optional[str] = None):
        """Connect in a background thread.

        ``ports`` may be several candidates; with ``expect_num`` each is opened in turn until
        the radio whose node number matches is found (COM numbers can change between sessions).
        """
        ports = [ports] if isinstance(ports, str) else list(ports)
        if not ports:
            raise RuntimeError("No serial port to try")
        if not self._connect_lock.acquire(blocking=False):
            raise RuntimeError("A connection attempt is already in progress")
        self.disconnect()
        what = label or ports[0]
        self.state.set_status("connecting", f"Opening {what}...", port=ports[0])
        threading.Thread(target=self._connect_worker, args=(ports, expect_num, label), daemon=True,
                         name="connect").start()

    def _connect_worker(self, ports: List[str], expect_num: Optional[int], label: Optional[str]):
        import meshtastic.serial_interface

        errors = []
        try:
            self._subscribe()
            for port in ports:
                if expect_num is not None:
                    self.state.set_status("connecting", f"Looking for {label or expect_num} on {port}...", port=port)
                iface = None
                try:
                    self._connecting = True
                    iface = meshtastic.serial_interface.SerialInterface(devPath=port, timeout=60)
                    num = iface.myInfo.my_node_num if iface.myInfo else None
                    if expect_num is not None and num != expect_num:
                        raise _NotTheRadio(f"{port} is a different radio (!{(num or 0):08x})")
                    self.iface, self.port = iface, port
                    self._connecting = False
                    self._import_device_state()
                    self.state.set_status("connected", "", port=port)
                    self.state.add_log("conn", f"Connected to {self.state.name_of(self.state.local_num)} on {port}")
                    if self.on_connected:
                        self.on_connected(port, iface)
                    return
                except (Exception, SystemExit) as e:  # noqa: BLE001 - the library sys.exit()s on some errors
                    self._connecting = False
                    if iface is not None and self.iface is not iface:
                        try:
                            iface.close()
                        except Exception:
                            pass
                    msg = str(e) or e.__class__.__name__
                    if "PermissionError" in repr(e) or "Access is denied" in msg:
                        msg = f"{port} is in use by another program (close the Meshtastic app / other serial monitors)."
                    if not isinstance(e, _NotTheRadio):
                        log.warning("Connect to %s failed: %s", port, msg)
                    errors.append(msg)
            self.iface = None
            msg = "; ".join(errors) if len(ports) > 1 or not errors else errors[0]
            if expect_num is not None:
                msg = f"{label or expect_num} was not found on {', '.join(ports)}. ({msg})"
            self.state.set_status("error", msg, port=ports[-1])
            self.state.add_log("error", f"Connect failed: {msg}")
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
        for d in list((iface.nodesByNum or {}).values()):
            st.merge_lib_node(d)
            # Nodes the radio itself reports at 0 hops are direct RF neighbours
            if (d.get("hopsAway") == 0 and d.get("num") != st.local_num and not d.get("viaMqtt")
                    and d.get("lastHeard") and st.local_num is not None):
                st.add_link(d["num"], st.local_num, d.get("snr"), "nodedb", ts=min(now, d["lastHeard"]))
        st.merge_node(st.local_num, hopsAway=0, lastHeard=now, lastHeardRf=now)
        self.refresh_channels()

    def refresh_channels(self):
        try:
            self.state.set_channels(self.channels())
        except Exception:
            log.exception("reading channels failed")

    def disconnect(self):
        iface, self.iface = self.iface, None
        if iface is not None:
            try:
                iface.close()
            except Exception:
                log.exception("Error closing interface")
            self.state.add_log("conn", "Disconnected")
        if self.state.channels:
            self.state.set_channels([])
        if self.state.status != "disconnected":
            self.state.set_status("disconnected")

    # ------------------------------------------------------- pubsub handlers
    def _owns(self, interface) -> bool:
        if interface is None or interface is self.iface:
            return True
        # while connecting, the library publishes before SerialInterface() returns
        return self.iface is None and self._connecting

    def _on_lost(self, interface, topic=pub.AUTO_TOPIC):
        if interface is self.iface and self.iface is not None:
            self.iface = None
            self.state.set_status("error", "Connection to the radio was lost (cable unplugged?)")
            self.state.add_log("error", "Connection lost")

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

    def _on_log_line(self, line, interface=None):
        if self._owns(interface) and self.on_log_line:
            self.on_log_line(line)

    # ---- request/response matching ----------------------------------------
    def after_packet(self, p, decoded, port, source):
        req = decoded.get("requestId")
        if req is not None and source in ("radio", "demo"):
            self._resolve_job(p, decoded, int(req), port)
            if port == "ROUTING_APP":
                self._resolve_message_ack(p, decoded, int(req))

    def _resolve_message_ack(self, p, decoded, req: int):
        st = self.state
        msg = st.find_outgoing_message(req)
        if msg is None or msg.get("status") == "delivered":
            return
        err = (decoded.get("routing") or {}).get("errorReason", "NONE")
        frm = p.get("from")
        if err != "NONE":
            st.update_message(msg, status="failed", error=err)
        elif msg.get("dm") and frm == msg.get("to"):
            st.update_message(msg, status="delivered", ackSnr=p.get("rxSnr"))
        elif frm == st.local_num:
            # implicit ACK: our radio heard a neighbour rebroadcast it (all a broadcast ever gets)
            st.update_message(msg, status="heard")
        else:
            st.update_message(msg, status="delivered")

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
                st.update_job(job, status="ok", result=res, rtt=elapsed, summary=f"{how} in {elapsed}s: {path}")
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

    # -------------------------------------------------------------- channels
    def channels(self) -> List[Dict[str, Any]]:
        """The radio's enabled channels with resolved names and AES keys."""
        if self.iface is None:
            return []
        from meshtastic.protobuf import config_pb2

        ln = self.iface.localNode
        try:
            lora = ln.localConfig.lora
            preset = preset_channel_name(config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)) \
                if lora.use_preset else "Custom"
        except Exception:
            preset = "LongFast"
        out = []
        for c in (ln.channels or []):
            if not c.role:  # DISABLED
                continue
            name = c.settings.name or preset
            out.append({"index": c.index, "name": name, "key": expand_psk(bytes(c.settings.psk)),
                        "primary": c.role == 1, "uplinkEnabled": bool(c.settings.uplink_enabled),
                        "defaultKey": len(c.settings.psk) == 1})
        return out

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
            path, known = st.guess_path(st.local_num, target)
            st.add_log("tx", f"{kind} request to {st.name_of(target)} (hop limit {hl})",
                       frm=st.local_num, to=target, port=kind, path=path, known=known, src=self.SOURCE)
        except Exception as e:
            st.update_job(job, status="failed", summary=f"Send failed: {e}")
            raise
        return job

    MAX_TEXT_BYTES = 200

    def send_text(self, text: str, dest: int | str = BROADCAST_NUM, channel: int = 0):
        """Transmit a text message on a channel (broadcast) or as a DM, and track its ACK."""
        if not self.connected:
            raise RuntimeError("Not connected to a radio")
        text = str(text or "").strip()
        if not text:
            raise ValueError("Message is empty")
        if len(text.encode("utf-8")) > self.MAX_TEXT_BYTES:
            raise ValueError(f"Message is too long (max {self.MAX_TEXT_BYTES} bytes)")
        if isinstance(dest, str):
            dest = BROADCAST_NUM if dest in ("^all", "") else int(dest.lstrip("!"), 16)
        channel = int(channel or 0)
        st = self.state
        if channel and not any(c["index"] == channel for c in st.channels):
            raise ValueError(f"Channel {channel} is not enabled on this radio")
        pkt = self._transmit_text(text, dest, channel)
        dm = dest != BROADCAST_NUM
        st.add_message(packetId=pkt.id, **{"from": st.local_num}, to=dest, text=text, src=self.SOURCE,
                       outgoing=True, dm=dm, channel=None if dm else channel,
                       channelName=None if dm else next((c["name"] for c in st.channels if c["index"] == channel), None),
                       status="sending")
        st.add_log("tx", f'TEXT to {st.name_of(dest) if dm else "everyone"} on ch {channel}: "{text}"',
                   frm=st.local_num, to=dest, port="TEXT_MESSAGE_APP", src=self.SOURCE,
                   path=st.guess_path(st.local_num, dest)[0] if dm else None)
        return pkt

    def _transmit_text(self, text: str, dest: int, channel: int):
        with self._send_lock:
            return self.iface.sendText(text, destinationId=dest, channelIndex=channel, wantAck=True)

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
