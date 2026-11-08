"""Live check against a real radio:  python tests/live_test.py COM9 [--act]

Without --act it only connects and reads (nothing is transmitted).
With --act it sends one traceroute and one ping to the best directly-heard neighbour.
"""

import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meshscope.radio import Radio  # noqa: E402
from meshscope.state import MeshState  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("meshtastic").setLevel(logging.WARNING)

port = sys.argv[1]
act = "--act" in sys.argv
st = MeshState(Path(tempfile.mkdtemp()) / "db.json")
radio = Radio(st)
radio.connect(port)
t0 = time.time()
while st.status == "connecting" and time.time() - t0 < 90:
    time.sleep(0.5)
print(f"status={st.status} detail={st.status_detail!r} after {time.time() - t0:.1f}s")
if st.status != "connected":
    sys.exit(1)

local = st.nodes.get(st.local_num, {})
print(f"local: {local.get('longName')} ({local.get('shortName')}) {local.get('id')} hw={local.get('hwModel')} hop_limit={st.hop_limit}")
nodes = [n for n in st.nodes.values() if n["num"] != st.local_num]
withpos = [n for n in nodes if n.get("lat") is not None]
hops = {}
for n in nodes:
    hops[n.get("hopsAway")] = hops.get(n.get("hopsAway"), 0) + 1
print(f"nodes={len(nodes)} with_position={len(withpos)} hops_histogram={dict(sorted(hops.items(), key=str))} links={len(st.links)}")
now = time.time()
for n in sorted(nodes, key=lambda n: -(n.get("lastHeard") or 0))[:12]:
    lh = n.get("lastHeard")
    print(f"  {n['id']} {str(n.get('shortName')):>5} {str(n.get('longName'))[:24]:<24} hops={n.get('hopsAway')} "
          f"snr={n.get('snr')} heard={'%.0fm ago' % ((now - lh) / 60) if lh else '?'} mqtt={bool(n.get('viaMqtt'))}")

print("listening 20s for live packets...")
time.sleep(20)
rx = [l for l in st.log if l["kind"] == "rx"]
print(f"received {len(rx)} packets; links now {len(st.links)}")
for l in rx[-10:]:
    print("  ", l["text"], "hops", l.get("hops"), "snr", l.get("snr"))

if act:
    cands = [n for n in nodes if n.get("hopsAway") == 0 and not n.get("viaMqtt") and n.get("lastHeard")]
    cands.sort(key=lambda n: (-(n.get("lastHeard") or 0)))
    if not cands:
        print("no direct neighbours to test against")
    else:
        tgt = cands[0]["num"]
        print(f"target: {st.name_of(tgt)} ({cands[0]['id']}) snr={cands[0].get('snr')}")
        jobs = [radio.run_action("traceroute", tgt), radio.run_action("ping", tgt)]
        deadline = time.time() + 120
        while time.time() < deadline and any(j["status"] in ("pending", "relayed") for j in jobs):
            time.sleep(1)
        for j in jobs:
            print(f"  {j['kind']}: {j['status']} - {j['summary']}")
            if j.get("result"):
                print("    result:", j["result"])

radio.disconnect()
print("done")
