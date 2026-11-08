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

    # persistence round trip
    call("/api/disconnect", {})
    state.save(force=True)
    s2 = MeshState(tmp / "db.json")
    assert len(s2.nodes) == len(state.nodes) and len(s2.links) == len(state.links)
    print("persistence ok:", len(s2.nodes), "nodes,", len(s2.links), "links")
    srv.shutdown()
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
