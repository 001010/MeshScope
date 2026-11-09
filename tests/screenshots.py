"""Start the app in demo mode, then capture every tab with headless Edge and fail on UI script errors.

    .venv\\Scripts\\python tests\\screenshots.py [output-dir]
"""

import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meshscope.app import find_edge  # noqa: E402
from meshscope.paths import static_dir  # noqa: E402
from meshscope.server import App, start_server  # noqa: E402
from meshscope.state import MeshState  # noqa: E402

TABS = ("nodes", "mynodes", "messages", "map", "topology", "reach", "activity", "config", "console", "addons", "mqtt")

out = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
out.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp())
state = MeshState(tmp / "db.json")
state.upsert_my_node(0xBEF73C95, longName="SS_WIO_Mobile1", shortName="WIO1", hwModel="SEEED_WIO_TRACKER_L1",
                     lastPort="COM9", lastConnected=time.time() - 3600, usbSerial="ABC123", vid=0x2886)
app = App(state, static_dir(), workdir=tmp)
app.addons.create("Keyword alert", "keyword_alert")
srv = start_server(app)
base = f"http://127.0.0.1:{srv.server_address[1]}"


def call(path, body=None):
    req = urllib.request.Request(base + path, headers={"X-MeshScope-Token": app.token})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req).read())


call("/api/connect", {"demo": True})
time.sleep(3)
snap = call("/api/state")
others = [n for n in snap["nodes"] if not n["num"] == snap["localNum"]]
for n in others[:3]:
    call("/api/favorite", {"node": n["num"], "favorite": True})
for n in others[:6]:
    call("/api/action", {"kind": "traceroute", "node": n["num"]})
    call("/api/action", {"kind": "ping", "node": n["num"]})
call("/api/send", {"text": "Hello mesh from MeshScope", "channel": 0})
call("/api/send", {"text": "Direct hello", "to": others[0]["num"]})
time.sleep(20)

edge = find_edge()
errors = {}
for tab in TABS:
    url = f"{base}/?t={app.token}#{tab}"
    common = [edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={tmp / 'edge'}",
              "--window-size=1440,900", "--virtual-time-budget=9000"]
    png = out / f"{tab}.png"
    subprocess.run(common + [f"--screenshot={png}", url], timeout=90, capture_output=True)
    dom = subprocess.run(common + ["--dump-dom", url], timeout=90, capture_output=True, text=True, encoding="utf-8").stdout
    m = re.search(r'<pre id="jsErrors"[^>]*>(.*?)</pre>', dom, re.S)
    errs = m.group(1).strip() if m else "(page did not render)"
    if errs:
        errors[tab] = errs
    print(f"{tab:9} {png} {'OK' if not errs else 'ERRORS: ' + errs}")
srv.shutdown()
if errors:
    sys.exit("UI errors found")
print("UI CHECK PASSED")
