"""Per-radio history: switching USB radios shows only the connected radio's history, switching back
restores it, it survives a restart, older databases are adopted, and Settings can clear it.

Drives the real Radio connect path with a fake meshtastic SerialInterface (no hardware needed).

    .venv\\Scripts\\python tests\\history_test.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import meshtastic.serial_interface  # noqa: E402
from pubsub import pub  # noqa: E402

from meshscope.radio import Radio  # noqa: E402
from meshscope.state import MeshState  # noqa: E402

BC = 0xFFFFFFFF
A, B = 0xA0000001, 0xB0000002
A_PEERS = [0xA1000001, 0xA1000002, 0xA1000003]
B_PEERS = [0xB1000001, 0xB1000002]
OK = True


def check(label, cond, extra=""):
    global OK
    OK &= bool(cond)
    print(f"{label:62} {'ok' if cond else 'FAIL'} {extra}")


def lib_node(num, name):
    return {"num": num, "user": {"id": f"!{num:08x}", "longName": name, "shortName": name[:4]},
            "lastHeard": time.time() - 30, "hopsAway": 0, "snr": 4.0}


def text_pkt(frm, text, pid):
    return {"from": frm, "to": BC, "id": pid, "rxTime": time.time(), "hopStart": 3, "hopLimit": 3, "rxSnr": 5.0,
            "channel": 0, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text, "payload": text.encode()}}


RADIOS = {"COM_A": (A, "Radio A", A_PEERS), "COM_B": (B, "Radio B", B_PEERS)}


class FakeSerialInterface:
    """Publishes like the library does while connecting: node updates and an early packet."""
    pid = 5000

    def __init__(self, devPath, timeout=60):
        num, name, peers = RADIOS[devPath]
        self.myInfo = SimpleNamespace(my_node_num=num)
        self.localNode = SimpleNamespace(localConfig=SimpleNamespace(lora=SimpleNamespace(hop_limit=3)), channels=[])
        self.nodesByNum = {n: lib_node(n, f"{name} peer {i}") for i, n in enumerate(peers)}
        self.nodesByNum[num] = lib_node(num, name)
        for d in self.nodesByNum.values():
            pub.sendMessage("meshtastic.node.updated", node=d, interface=self)
        # the same packet each time this radio connects (as when it's still in the radio's queue)
        pub.sendMessage("meshtastic.receive", packet=text_pkt(peers[0], f"early hello to {name}", num & 0xFFFF),
                        interface=self)

    def close(self):
        pass


meshtastic.serial_interface.SerialInterface = FakeSerialInterface


def connect(radio, port):
    radio.connect(port)
    end = time.time() + 10
    while time.time() < end and radio.state.status != "connected":
        time.sleep(0.05)
    assert radio.state.status == "connected", radio.state.status_detail
    radio.state.expire_jobs()


def shown(st):
    return {n for n in st.nodes}, [m["text"] for m in st.messages]


tmp = Path(tempfile.mkdtemp())
db = tmp / "db.json"
st = MeshState(db)
radio = Radio(st)

# --- radio A
connect(radio, "COM_A")
radio.handle_packet(text_pkt(A_PEERS[1], "A: trail report", 9001))
st.new_job("traceroute", A_PEERS[2], 30)
nodes, texts = shown(st)
check("A connected: shows A and its peers only", nodes == {A, *A_PEERS}, sorted(map(hex, nodes)))
check("A: early packet (sent while connecting) is in A's history", "early hello to Radio A" in texts, texts)
check("A: own message recorded", "A: trail report" in texts)

# --- switch to radio B
radio.disconnect()
connect(radio, "COM_B")
nodes, texts = shown(st)
check("B connected: none of A's nodes are shown", not (nodes & {A, *A_PEERS}), sorted(map(hex, nodes)))
check("B connected: shows B and its peers", nodes == {B, *B_PEERS})
check("B: none of A's messages are shown", texts == ["early hello to Radio B"], texts)
check("B: none of A's requests/activity are shown",
      not st.jobs and not any("Radio A" in (l.get("text") or "") and l["kind"] == "rx" for l in st.log))
check("B: the switch is noted in B's activity log", any("Switched radios" in l["text"] for l in st.log))
radio.handle_packet(text_pkt(B_PEERS[1], "B: rain on the pass", 9002))

# --- back to A: A's last known history returns
radio.disconnect()
connect(radio, "COM_A")
nodes, texts = shown(st)
check("back to A: A's history is restored", nodes == {A, *A_PEERS} and "A: trail report" in texts, texts)
check("back to A: A's traceroute (pending at the switch) is back, unanswered",
      any(j["target"] == A_PEERS[2] and j["status"] == "timeout" for j in st.jobs))
check("back to A: the early message isn't duplicated", texts.count("early hello to Radio A") == 1, texts)
check("back to A: nothing of B's is shown", not (nodes & {B, *B_PEERS}) and "B: rain on the pass" not in texts)
seqs = [m["seq"] for m in st.messages]
check("message sequence numbers stay unique", len(seqs) == len(set(seqs)))

# --- restart: both radios' history persists
st.save(force=True)
st2 = MeshState(db)
check("after restart: A (last connected) is shown", st2.profile == A and {A, *A_PEERS} <= set(st2.nodes))
check("after restart: B's history is stored", B in st2.profiles and
      any(m["text"] == "B: rain on the pass" for m in st2.profiles[B]["messages"]))
summary = {r["num"]: r for r in st2.history_summary()}
check("history list names both radios", summary.keys() == {A, B} and summary[A]["current"] and not summary[B]["current"],
      [(hex(k), v["name"]) for k, v in summary.items()])
check("history list counts (A: 3 nodes, 2 messages, 1 request)",
      (summary[A]["nodes"], summary[A]["messages"], summary[A]["requests"]) == (3, 2, 1),
      (summary[A]["nodes"], summary[A]["messages"], summary[A]["requests"]))

# --- clear B only, then A (the shown one)
n = st2.clear_radio_history([B])
check("clear B: B's stored history is gone, A's untouched", n == 1 and B not in st2.profiles and len(st2.messages) == 2)
st2.local_num = A
n = st2.clear_radio_history([A])
check("clear A (shown): its nodes/messages/requests cleared, own node kept",
      n == 1 and set(st2.nodes) == {A} and not st2.messages and not st2.jobs and not st2.links)

# --- an older database (one shared history) is given to the radio connected most recently
old = tmp / "old.json"
old.write_text(json.dumps({"dbVersion": 1, "nodes": [{"num": 7, "id": "!00000007"}], "messages": [{"seq": 1, "t": 1, "text": "legacy"}],
                           "jobs": [], "links": [], "favorites": [], "settings": {},
                           "myNodes": [{"num": A, "lastConnected": 100}, {"num": B, "lastConnected": 200}]}), encoding="utf-8")
st3 = MeshState(old)
check("old database: history is assigned to the most recently used radio (B)", st3.profile == B and 7 in st3.nodes)
st3.switch_profile(A)
check("old database: connecting A shows an empty history, B's kept", 7 not in st3.nodes and B in st3.profiles)

print("HISTORY TEST PASSED" if OK else "HISTORY TEST FAILED")
sys.exit(0 if OK else 1)
