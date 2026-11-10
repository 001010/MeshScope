"""End-to-end smoke test: real HTTP server + demo mesh, exercised through the public API."""

import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meshscope.paths import static_dir  # noqa: E402
from meshscope.server import App, start_server  # noqa: E402
from meshscope.state import MeshState  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp())
    state = MeshState(tmp / "db.json")
    app = App(state, static_dir())
    srv = start_server(app)
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, body=None, token=app.token):
        req = urllib.request.Request(base + path, headers={"X-MeshScope-Token": token})
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    # auth guard
    try:
        call("/api/state", token="wrong")
        raise AssertionError("unauthorized request was accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 403
    # static files
    for f in ("/", "/app.js", "/app.css", "/icon.png", "/vendor/leaflet/leaflet.js"):
        with urllib.request.urlopen(base + f) as r:
            assert r.status == 200, f
    try:
        urllib.request.urlopen(base + "/../meshscope/state.py")
        raise AssertionError("path traversal")
    except urllib.error.HTTPError as e:
        assert e.code == 404

    print("ports:", [p["device"] for p in call("/api/ports")["ports"]])
    call("/api/connect", {"demo": True})
    time.sleep(4)
    snap = call("/api/state")
    assert snap["status"] == "connected", snap["status"]
    local = snap["localNum"]
    others = [n for n in snap["nodes"] if n["num"] != local]
    print(f"nodes={len(snap['nodes'])} edges={len(snap['edges'])} log={len(snap['log'])}")
    assert others and snap["edges"]

    far = sorted(others, key=lambda n: -(n.get("hopsAway") or 0))
    targets = [far[0]["num"], far[len(far) // 2]["num"], far[-1]["num"]]
    for t in targets:
        for kind in ("traceroute", "ping", "nodeinfo", "position", "telemetry"):
            call("/api/action", {"kind": kind, "node": t})
    deadline = time.time() + 60
    while time.time() < deadline:
        jobs = call("/api/state")["jobs"]
        if all(j["status"] not in ("pending", "relayed") for j in jobs):
            break
        time.sleep(1)
    jobs = call("/api/state")["jobs"]
    for j in reversed(jobs):
        print(f"  {j['kind']:<10} {j['targetName'][:18]:<18} {j['status']:<8} {j['summary'][:90]}")
    ok = [j for j in jobs if j["status"] == "ok"]
    assert len(ok) >= len(jobs) * 0.6, "too many failed jobs"
    tr = [j for j in ok if j["kind"] == "traceroute"]
    assert tr and tr[0]["result"]["towards"][0]["num"] == local

    r = call(f"/api/reach?node={local}&hops=3")
    print(f"reach from local @3 hops: {r['reachable']} nodes, perHop={r['perHop']}, beyond={len(r['beyond'])}, farthest={r['farthest']}")
    assert r["reachable"] > 0
    r1 = call(f"/api/reach?node={local}&hops=1")
    assert r1["reachable"] <= r["reachable"]
    rf = call(f"/api/reach?node={targets[0]}&hops=7&maxAgeH=24")
    print(f"reach from far node @7 hops: {rf['reachable']} nodes")

    # ---- v1.1: channels + messaging (demo radio simulates the ACKs)
    snap = call("/api/state")
    assert [c["name"] for c in snap["channels"]] == ["LongFast", "Trailhead"], snap["channels"]
    assert all("key" not in c for c in snap["channels"]), "channel keys leaked to the UI"
    call("/api/send", {"text": "hello channel 1", "channel": 1})
    dm_target = next(n["num"] for n in others if n.get("hopsAway") == 0)
    call("/api/send", {"text": "hello direct", "to": dm_target})
    for bad in ({"text": "x" * 201, "channel": 0}, {"text": "hi", "channel": 5}, {"text": "  ", "channel": 0}):
        try:
            call("/api/send", bad)
            raise AssertionError(f"bad send accepted: {bad}")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    deadline = time.time() + 15
    while time.time() < deadline:
        outs = [m for m in call("/api/state")["messages"] if m.get("outgoing")]
        if outs and all(m["status"] != "sending" for m in outs):
            break
        time.sleep(0.5)
    outs = {m["text"]: m for m in call("/api/state")["messages"] if m.get("outgoing")}
    assert outs["hello channel 1"]["channel"] == 1 and outs["hello channel 1"]["status"] == "heard", outs["hello channel 1"]
    assert outs["hello direct"]["dm"] and outs["hello direct"]["status"] == "delivered", outs["hello direct"]
    print("messaging ok: channel send heard, DM delivered, bad sends rejected")

    # ---- v1.1: config / console / addons are guarded in demo mode
    for path in ("/api/config",):
        try:
            call(path)
            raise AssertionError(f"{path} worked in demo mode")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    assert call("/api/config/backups")["backups"] == []
    try:
        call("/api/console/run", {"args": "--info"})
        raise AssertionError("CLI ran in demo mode")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    try:
        call("/api/console/run", {"args": "--port COM3 --info"})
        raise AssertionError("blocked CLI flag accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    a = call("/api/addons/create", {"name": "Alert", "template": "keyword_alert"})
    call("/api/addons/enable", {"slug": a["slug"], "enabled": True})
    app.radio._deliver(dm_target, 0xFFFFFFFF, "TEXT_MESSAGE_APP", b"need help here", decoded_extra={"text": "need help here"})
    time.sleep(1.5)
    assert any(l["kind"] == "notify" and "need help" in l["text"] for l in call("/api/state")["log"]), "addon did not fire"
    det = call(f"/api/addon?slug={a['slug']}")
    assert det["status"] == "running" and det["packets"] >= 1, det
    print(f"addons ok: keyword alert fired after {det['packets']} packets")

    # demo data must never reach the real (persistent) state
    demo_nums = {n["num"] for n in call("/api/state")["nodes"]}
    assert not state.nodes and not state.links, "demo leaked into the real state while running"
    v_demo = call("/api/state")["version"]
    call("/api/disconnect", {})
    after = call("/api/state")
    assert after["version"] > v_demo, "UI would not notice the swap back"
    assert not {n["num"] for n in after["nodes"]} & demo_nums, "demo nodes visible after leaving demo"
    assert after["status"] == "disconnected"
    print("demo isolation ok: real state untouched,", len(demo_nums), "demo nodes discarded")

    # real data survives a demo session, and persists
    state.merge_node(0x12345678, longName="Real Node", lat=33.4, lon=-112.0, lastHeard=time.time())
    call("/api/connect", {"demo": True})
    time.sleep(1.5)
    call("/api/disconnect", {})
    assert {n["num"] for n in call("/api/state")["nodes"]} == {0x12345678}
    state.save(force=True)
    s2 = MeshState(tmp / "db.json")
    assert set(s2.nodes) == {0x12345678}
    print("persistence ok: real node kept, no demo nodes on disk")

    # ---- v1.1: MQTT badge only for messages that arrived *only* via the MQTT bridge
    state.local_num = 0x0BEEF001
    state.set_channels([{"index": 0, "name": "LongFast", "primary": True}])
    sender = 0x0C0FFEE1
    pkt = {"from": sender, "to": 0xFFFFFFFF, "id": 4242, "hopStart": 3, "hopLimit": 1, "channelId": "LongFast",
           "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "seen on mqtt first"}}
    assert app.ingest_mqtt(dict(pkt), gateway=0x0A0A0A0A) == "new"
    msgs = [m for m in call("/api/state")["messages"] if m["text"] == "seen on mqtt first"]
    assert len(msgs) == 1 and msgs[0]["src"] == "mqtt" and msgs[0]["channel"] == 0, msgs
    assert app.ingest_mqtt(dict(pkt), gateway=0x0B0B0B0B) == "dup"
    from meshscope.radio import Radio
    rf = Radio(state)
    rf_pkt = dict(pkt, rxSnr=6.5)
    rf_pkt.pop("channelId")
    rf.handle_packet(rf_pkt)
    msgs = [m for m in call("/api/state")["messages"] if m["text"] == "seen on mqtt first"]
    assert len(msgs) == 1 and msgs[0]["src"] == "radio" and msgs[0]["snr"] == 6.5, msgs
    rf.handle_packet({"from": sender, "to": 0xFFFFFFFF, "id": 4243, "hopStart": 3, "hopLimit": 3, "viaMqtt": True,
                      "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "rf only"}})
    m2 = [m for m in call("/api/state")["messages"] if m["text"] == "rf only"][0]
    assert m2["src"] == "radio", m2
    print("mqtt badge ok: MQTT-only message flagged, cleared once heard over RF; radio packets never flagged")
    # ---- user settings: theme + units (saved with the real state, visible while in demo mode)
    call("/api/connect", {"demo": True})
    assert call("/api/state")["settings"]["ui"] == {"theme": "system", "units": "metric"}
    v0 = call("/api/state")["version"]
    assert call("/api/ui-settings", {"theme": "night", "units": "imperial"})["ui"] == {"theme": "night", "units": "imperial"}
    snap = call("/api/state")
    assert snap["settings"]["ui"] == {"theme": "night", "units": "imperial"} and snap["version"] > v0, snap["settings"]["ui"]
    for bad in ({"theme": "pink"}, {"units": "furlongs"}, {}):
        try:
            call("/api/ui-settings", bad)
            raise AssertionError(f"accepted invalid settings {bad}")
        except urllib.error.HTTPError as e:
            assert e.code == 400, e.code
    call("/api/ui-settings", {"theme": "day"})
    assert json.loads((tmp / "db.json").read_text())["settings"]["ui"] == {"theme": "day", "units": "imperial"}
    print("ui settings ok: theme/units saved, validated, and shown in demo mode")
    call("/api/disconnect", {})
    srv.shutdown()

    # databases polluted by older versions get cleaned on startup
    from meshscope.demo import demo_node_names
    legacy = MeshState(tmp / "legacy.json")
    names = demo_node_names()
    for num, name in names.items():
        legacy.merge_node(num, longName=name)
    legacy.merge_node(0x12345678, longName="Real Node")
    legacy.add_link(next(iter(names)), 0x12345678, 5.0, "direct")
    legacy.save(force=True)
    App(MeshState(tmp / "legacy.json"), static_dir())
    cleaned = MeshState(tmp / "legacy.json")
    assert set(cleaned.nodes) == {0x12345678} and not cleaned.links, cleaned.nodes.keys()
    print(f"legacy purge ok: removed {len(names)} old demo nodes, kept the real one")
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
