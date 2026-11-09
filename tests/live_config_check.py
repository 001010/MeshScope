"""READ-ONLY check of channels + configuration export on a real radio. Writes nothing to the radio.

    .venv\\Scripts\\python tests\\live_config_check.py COM9
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meshscope.core import App  # noqa: E402
from meshscope.paths import static_dir  # noqa: E402
from meshscope.state import MeshState  # noqa: E402

port = sys.argv[1] if len(sys.argv) > 1 else "COM9"
tmp = Path(tempfile.mkdtemp())
app = App(MeshState(None), static_dir(), workdir=tmp)
app.radio.connect(port)
deadline = time.time() + 90
while app.state.status in ("connecting", "disconnected") and time.time() < deadline:
    time.sleep(0.5)
assert app.state.status == "connected", app.state.status_detail
try:
    st = app.state
    print("connected:", st.name_of(st.local_num), f"!{st.local_num:08x}")
    print("channels:", [(c["index"], c["name"], "primary" if c["primary"] else "secondary",
                         "default key" if c["defaultKey"] else "private key") for c in st.channels])
    s = app.config.schema()
    print("owner:", s["owner"])
    print("sections:", ", ".join(f"{x['name']}({len(x['fields'])})" for x in s["sections"]))
    lora = next(x for x in s["sections"] if x["name"] == "lora")
    print("lora:", {f["path"]: f["value"] for f in lora["fields"] if f["path"] in ("region", "modem_preset", "hop_limit", "use_preset")})
    sec = next(x for x in s["sections"] if x["name"] == "security")
    pk = next(f for f in sec["fields"] if f["path"] == "private_key")
    assert pk["value"] == "(hidden)", "private key exposed to the UI"
    b = app.config.backup("live check")
    text = (app.config.dir / b["file"]).read_text(encoding="utf-8")
    print(f"backup: {b['file']} {b['size']} bytes, has channel url: {'channel_url' in text or 'channelUrl' in text}, "
          f"has lora: {'lora' in text}")
    assert "config" in text
    print("LIVE CONFIG CHECK PASSED (read-only)")
finally:
    app.radio.disconnect()
