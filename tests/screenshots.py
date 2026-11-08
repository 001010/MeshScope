"""Start the app in demo mode and capture each tab with headless Edge (manual UI check)."""

import json
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

out = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
out.mkdir(parents=True, exist_ok=True)
tmp = Path(tempfile.mkdtemp())
state = MeshState(tmp / "db.json")
app = App(state, static_dir())
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
for n in others[:6]:
    call("/api/action", {"kind": "traceroute", "node": n["num"]})
    call("/api/action", {"kind": "ping", "node": n["num"]})
time.sleep(25)

edge = find_edge()
for tab in ("nodes", "map", "topology", "reach", "activity"):
    png = out / f"{tab}.png"
    subprocess.run([edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={tmp / 'edge'}",
                    "--window-size=1440,900", "--virtual-time-budget=8000", f"--screenshot={png}",
                    f"{base}/?t={app.token}#{tab}"], timeout=60, capture_output=True)
    print(tab, png, png.exists())
srv.shutdown()
