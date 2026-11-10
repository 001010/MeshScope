"""Start the app in demo mode, then capture every tab (Night and Day themes) with headless Edge driven over
the DevTools protocol, and fail on any uncaught script error, console error or message in the UI error panel.

    .venv\\Scripts\\python tests\\screenshots.py [output-dir]
"""

import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from meshscope.app import find_edge  # noqa: E402
from meshscope.paths import static_dir  # noqa: E402
from meshscope.server import App, start_server  # noqa: E402
from meshscope.state import MeshState  # noqa: E402
from cdp import HeadlessEdge  # noqa: E402

READY = "typeof S !== 'undefined' && S.booted"

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
with HeadlessEdge(edge) as page:
    # the detector itself must work, or a broken page would pass silently
    page.goto(f"{base}/?t={app.token}#nodes", READY)
    page.eval("setTimeout(() => { throw new Error('detector self-check') }, 0)")
    page.pump(0.6)
    if not any("detector self-check" in e for e in page.errors):
        sys.exit("UI CHECK BROKEN: a deliberate script error was not detected")
    page.errors.clear()
    for theme in ("night", "day"):
        call("/api/ui-settings", {"theme": theme})
        for tab in TABS:
            page.errors.clear()
            ok = page.goto(f"{base}/?t={app.token}#{tab}", READY)
            page.pump(2.5)  # let maps, graphs and polling settle
            png = out / f"{tab}-{theme}.png"
            page.screenshot(png)
            panel = page.eval("(document.getElementById('jsErrors') || {}).textContent || ''") or ""
            errs = list(page.errors) + ([f"error panel: {panel.strip()}"] if panel.strip() else [])
            if not ok:
                errs.append("page did not finish loading")
            if errs:
                errors[f"{tab}/{theme}"] = errs
            print(f"{tab:9} {theme:5} {png} {'OK' if not errs else 'ERRORS: ' + ' | '.join(errs)}")
srv.shutdown()
if errors:
    sys.exit("UI errors found")
print("UI CHECK PASSED")
