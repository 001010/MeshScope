"""MQTT bridge: subscribe to a Meshtastic broker for wider/real-time data, with an optional,
strictly filtered uplink of packets our radio heard over the air.

Rules for uplink (all must hold):
  * the packet was received over RF by the connected USB radio (never demo, never MQTT,
    never a packet the radio itself flags via_mqtt) - MQTT data is never re-published;
  * the sender set the OK_TO_MQTT bit in the packet's bitfield;
  * it is a broadcast (DMs are never uplinked) on a channel the user explicitly allowed;
  * the same (from, id) has not been seen on the broker within ``uplinkDelay`` seconds
    (another gateway already published it).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional

from .ingest import packet_to_dict
from .meshcrypto import channel_hash, crypt, expand_psk, parse_psk
from .state import BROADCAST_NUM, haversine_km

log = logging.getLogger(__name__)

OK_TO_MQTT = 1
UPLINK_PORTS = {"TEXT_MESSAGE_APP", "POSITION_APP", "NODEINFO_APP", "TELEMETRY_APP", "NEIGHBORINFO_APP",
                "TRACEROUTE_APP", "WAYPOINT_APP", "MAP_REPORT_APP", "STORE_FORWARD_APP", "RANGE_TEST_APP",
                "PAXCOUNTER_APP", "DETECTION_SENSOR_APP"}


class MqttBridge:
    def __init__(self, app):
        self.app = app
        self.client = None
        self.status = "stopped"  # stopped | connecting | connected | error
        self.detail = ""
        self.stats = {"received": 0, "decoded": 0, "encrypted": 0, "duplicates": 0, "filtered": 0,
                      "ignored": 0, "published": 0, "uplinkSkippedSeen": 0, "uplinkSkippedFlag": 0}
        self._lock = threading.Lock()

    @property
    def settings(self) -> Dict[str, Any]:
        return self.app.real_state.settings["mqtt"]

    def _set_status(self, status, detail=""):
        self.status, self.detail = status, detail
        self.app.real_state.add_log("mqtt", f"MQTT {status}" + (f": {detail}" if detail else ""))

    def info(self) -> Dict[str, Any]:
        s = dict(self.settings)
        s["password"] = "********" if s.get("password") else ""
        radio_channels = [{k: v for k, v in c.items() if k != "key"} for c in self._radio_channels()]
        return {"status": self.status, "detail": self.detail, "stats": dict(self.stats), "settings": s,
                "radioChannels": radio_channels}

    # ------------------------------------------------------------ lifecycle
    def start(self):
        import paho.mqtt.client as mqtt

        self.stop()
        s = self.settings
        cid = f"meshscope-{random.randrange(16 ** 6):06x}"
        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        if s.get("username"):
            c.username_pw_set(s["username"], s.get("password") or None)
        if s.get("tls"):
            c.tls_set()
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(2, 60)
        self.client = c
        self._set_status("connecting", f"{s['host']}:{s['port']}")
        try:
            c.connect_async(s["host"], int(s["port"]), keepalive=60)
            c.loop_start()
        except Exception as e:
            self._set_status("error", str(e))
            self.client = None

    def stop(self):
        c, self.client = self.client, None
        if c is not None:
            try:
                c.loop_stop()
                c.disconnect()
            except Exception:
                pass
            self._set_status("stopped")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            self._set_status("error", f"broker refused: {reason_code}")
            return
        topics = [t.strip() for t in self.settings.get("topics", []) if t.strip()]
        for t in topics:
            client.subscribe(t, qos=0)
        self._set_status("connected", f"subscribed to {', '.join(topics) or '(nothing)'}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if self.client is client:
            self._set_status("connecting", f"disconnected ({reason_code}); retrying")

    # -------------------------------------------------------------- keys
    def _radio_channels(self) -> List[Dict[str, Any]]:
        radio = self.app.radio
        if getattr(radio, "SOURCE", "") == "radio" and radio.connected:
            try:
                return radio.channels()
            except Exception:
                log.exception("reading radio channels failed")
        return []

    def _keys_for(self, channel_name: str) -> List[bytes]:
        keys: List[bytes] = []
        for c in self._radio_channels():
            if c["name"] == channel_name and c["key"]:
                keys.append(c["key"])
        for ck in self.settings.get("channelKeys", []):
            if ck.get("name") in (channel_name, "*", ""):
                try:
                    k = expand_psk(parse_psk(ck.get("psk", "")))
                    if k:
                        keys.append(k)
                except Exception:
                    pass
        keys.append(expand_psk(b"\x01"))  # the default key covers the public preset channels
        out, seen = [], set()
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append(k)
        return out

    # ----------------------------------------------------------- inbound
    def _on_message(self, client, userdata, msg):
        try:
            self._handle(msg.topic, msg.payload)
        except Exception:
            log.exception("MQTT message handling failed (%s)", msg.topic)

    def _handle(self, topic: str, payload: bytes):
        from meshtastic.protobuf import mesh_pb2, mqtt_pb2

        self.stats["received"] += 1
        if "/2/e/" not in topic and "/2/map/" not in topic:
            self.stats["ignored"] += 1  # json / stat topics
            return
        env = mqtt_pb2.ServiceEnvelope()
        try:
            env.ParseFromString(payload)
        except Exception:
            self.stats["ignored"] += 1
            return
        if not env.HasField("packet"):
            self.stats["ignored"] += 1
            return
        mp = env.packet
        gw = None
        if env.gateway_id.startswith("!"):
            try:
                gw = int(env.gateway_id[1:], 16)
            except ValueError:
                gw = None
        st = self.app.real_state
        if gw is not None and gw == st.local_num:
            self.stats["ignored"] += 1  # our own radio's (or our own) uplink echo
            return

        if mp.WhichOneof("payload_variant") == "encrypted":
            for key in self._keys_for(env.channel_id):
                data = mesh_pb2.Data()
                try:
                    data.ParseFromString(crypt(key, mp.id, getattr(mp, "from"), mp.encrypted))
                except Exception:
                    continue
                if data.portnum:
                    mp2 = mesh_pb2.MeshPacket()
                    mp2.CopyFrom(mp)
                    mp2.decoded.CopyFrom(data)  # replaces the encrypted oneof
                    mp = mp2
                    break
        if mp.WhichOneof("payload_variant") == "decoded":
            self.stats["decoded"] += 1
        else:
            self.stats["encrypted"] += 1
        p = packet_to_dict(mp)
        p["channelId"] = env.channel_id
        if not self._geo_ok(p):
            self.stats["filtered"] += 1
            return
        if self.app.ingest_mqtt(p, gw) == "dup":
            self.stats["duplicates"] += 1

    def _geo_ok(self, p) -> bool:
        s = self.settings
        max_km = float(s.get("maxDistanceKm") or 0)
        if not max_km:
            return True
        st = self.app.real_state
        me = st.nodes.get(st.local_num or -1, {})
        if me.get("lat") is None:
            return True  # we don't know where we are; can't filter
        pos = (p.get("decoded") or {}).get("position") or {}
        lat, lon = pos.get("latitude"), pos.get("longitude")
        if lat is None:
            n = st.nodes.get(p.get("from"), {})
            lat, lon = n.get("lat"), n.get("lon")
        if lat is None or lon is None:
            return not s.get("strictDistance")
        return haversine_km(me["lat"], me["lon"], lat, lon) <= max_km

    # ---------------------------------------------------------- outbound
    def consider_uplink(self, p: Dict[str, Any], key, rec):
        """Called for every packet the USB radio received. May schedule a publish."""
        s = self.settings
        if not (s.get("uplink") and self.client is not None and self.status == "connected" and key):
            return
        if p.get("viaMqtt"):
            return  # the radio got it from MQTT itself: never echo it back
        decoded = p.get("decoded") or {}
        if not decoded or p.get("to") != BROADCAST_NUM or decoded.get("portnum") not in UPLINK_PORTS:
            return
        if not (int(decoded.get("bitfield") or 0) & OK_TO_MQTT):
            self.stats["uplinkSkippedFlag"] += 1
            return
        ch = self._channel_by_index(int(p.get("channel") or 0))
        if ch is None or ch["name"] not in s.get("uplinkChannels", []):
            return
        if "mqtt" in rec["sources"]:
            self.stats["uplinkSkippedSeen"] += 1
            return
        t = threading.Timer(float(s.get("uplinkDelay", 5)), self._publish_if_unseen, args=(p, key, ch))
        t.daemon = True
        t.start()

    def _channel_by_index(self, idx: int) -> Optional[Dict[str, Any]]:
        for c in self._radio_channels():
            if c["index"] == idx:
                return c
        return None

    def _publish_if_unseen(self, p, key, ch):
        from meshtastic.protobuf import mesh_pb2, mqtt_pb2

        rec = self.app.seen.get(key)
        if rec is None or "mqtt" in rec["sources"] or rec.get("published"):
            self.stats["uplinkSkippedSeen"] += 1
            return
        if self.client is None or self.status != "connected":
            return
        raw = p.get("raw")
        if raw is None:
            return
        mp = mesh_pb2.MeshPacket()
        mp.CopyFrom(raw)
        data = mp.decoded.SerializeToString()
        if ch["key"]:
            mp.encrypted = crypt(ch["key"], mp.id, getattr(mp, "from"), data)  # clears decoded
        mp.channel = channel_hash(ch["name"], ch["key"])
        mp.via_mqtt = False
        st = self.app.real_state
        gw_id = f"!{(st.local_num or 0):08x}"
        env = mqtt_pb2.ServiceEnvelope(packet=mp, channel_id=ch["name"], gateway_id=gw_id)
        topic = f"{self.settings.get('uplinkRoot', 'msh/US').rstrip('/')}/2/e/{ch['name']}/{gw_id}"
        self.client.publish(topic, env.SerializeToString(), qos=0)
        rec["published"] = True
        self.stats["published"] += 1
        log.info("uplinked %s from !%08x to %s", (p.get("decoded") or {}).get("portnum"), p.get("from"), topic)
