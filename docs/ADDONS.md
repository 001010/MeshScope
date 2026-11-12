# Writing a MeshScope addon

Addons are small Python scripts that MeshScope runs for you. Every packet your mesh produces (from your USB radio, from MQTT, or from the demo mesh) is handed to each running addon, so an addon can log things, pop up alerts, keep statistics, or reply to messages.

This guide walks you through creating your first addon in the app, then builds up to a few practical examples. You need only basic Python: functions, `if` statements, strings and dictionaries.

- [1. Create your first addon](#1-create-your-first-addon)
- [2. How an addon is structured](#2-how-an-addon-is-structured)
- [3. What's in a packet](#3-whats-in-a-packet)
- [4. The `api` object](#4-the-api-object)
- [5. Examples](#5-examples)
- [6. Sending messages safely](#6-sending-messages-safely)
- [7. Testing with the demo mesh](#7-testing-with-the-demo-mesh)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Sharing addons](#9-sharing-addons)

> **Only run code you trust.** An addon is ordinary Python running on your PC with your permissions, the same as any program you install. Imported addons always start disabled so you can read them first.

---

## 1. Create your first addon

1. Open MeshScope and go to the **Addons** tab.
2. Click **New**, type a name such as `Message logger`, leave the template on **Blank**, and click **Create**.
3. The editor opens with a starter script. Replace it with:

   ```python
   NAME = "Message logger"


   def on_packet(packet, api):
       if packet["portnum"] == "TEXT_MESSAGE_APP":
           api.log(f'{packet["fromName"]}: {packet["text"]}')
   ```

4. Press **Ctrl+S** (or click **Save**).
5. Tick **Enabled**. The status pill turns green (`running`) and the **Output** pane shows `started`.
6. Every text message heard on the mesh now appears in the Output pane:

   ```
   6:42:10 PM  Ridge Hilltop: checking in from the trailhead
   ```

No radio handy? Click **Demo** at the top right first. The simulated mesh sends text messages every few seconds (see [Testing with the demo mesh](#7-testing-with-the-demo-mesh)).

**Editor tips:** **Tab** and **Shift+Tab** indent and outdent by four spaces, Enter keeps the current indentation (and adds a level after a line ending in `:`), and **Ctrl+S** saves. Saving an enabled addon restarts it with the new code. If the code has a syntax error, it isn't saved and the error is shown.

---

## 2. How an addon is structured

An addon is one `.py` file. MeshScope looks for these names, and all of them are optional:

```python
NAME = "Friendly name"      # shown in the addon list
TICK_SECONDS = 60           # if set, on_tick() runs this often


def on_start(api):          # once, when the addon is enabled or MeshScope starts
    ...

def on_packet(packet, api): # for every new packet
    ...

def on_tick(api):           # every TICK_SECONDS
    ...

def on_stop(api):           # once, when the addon is disabled or MeshScope closes
    ...
```

Things worth knowing:

- **Each packet arrives once.** If the same packet is heard by your radio and also comes in through MQTT, your addon only gets the first copy.
- **Each addon runs in its own background thread.** A slow or broken addon can't freeze the app or the other addons. Your functions are called one at a time, so you don't need locks.
- **Enabled addons start automatically** every time MeshScope starts.
- **`print()` works** and writes to the Output pane, the same as `api.log()`.
- Variables at the top of the file keep their values while the addon runs, but are reset when it restarts. Use [`api.store`](#4-the-api-object) for anything you want to keep.

---

## 3. What's in a packet

`packet` is a Python dictionary. These keys are always present:

| Key | Example | Meaning |
|---|---|---|
| `source` | `"radio"` | Where it came from: `"radio"` (your USB radio heard it), `"mqtt"` (the MQTT bridge), or `"demo"` |
| `portnum` | `"TEXT_MESSAGE_APP"` | The type of packet (see the list below) |
| `from` | `3203874709` | Sender's node number |
| `fromId` | `"!bef73c95"` | Sender's node ID |
| `fromName` | `"Ridge Hilltop"` | Sender's name (or ID if the name isn't known yet) |
| `to` | `4294967295` | Destination node number; `4294967295` means everyone (a broadcast) |
| `toName` | `"everyone"` | Destination name |
| `text` | `"hello"` | The message text for text messages, otherwise `None` |
| `hops` | `1` | How many relays it passed through (`None` if unknown) |
| `receiver` | `3203874709` | The node that heard it: your radio, or the MQTT gateway |
| `decoded` | `{...}` | The decoded contents (see below); empty for packets that couldn't be decrypted (`portnum` is then `"ENCRYPTED"`) |

These keys are present only when the radio provides them, so read them with `packet.get(...)`:

| Key | Meaning |
|---|---|
| `rxSnr`, `rxRssi` | Signal quality when your radio received it |
| `channel` | Channel index (missing means channel 0) |
| `id` | Packet ID |
| `hopStart`, `hopLimit` | The hop budget it was sent with, and what was left |

**Common `portnum` values and where their data is:**

| `portnum` | What it is | Useful data |
|---|---|---|
| `TEXT_MESSAGE_APP` | A chat message | `packet["text"]` |
| `POSITION_APP` | A GPS position | `packet["decoded"]["position"]["latitude"]`, `["longitude"]`, `["altitude"]` |
| `TELEMETRY_APP` | Battery and device health | `packet["decoded"]["telemetry"]["deviceMetrics"]["batteryLevel"]`, `["voltage"]`, `["channelUtilization"]` |
| `NODEINFO_APP` | A node announcing its name | `packet["decoded"]["user"]["longName"]`, `["shortName"]`, `["hwModel"]` |
| `NEIGHBORINFO_APP` | Who a node can hear directly | `packet["decoded"]["payload"]` (raw) |
| `TRACEROUTE_APP` | A traceroute | `packet["decoded"]["payload"]` (raw) |
| `ROUTING_APP` | Delivery confirmations (ACKs) | `packet["decoded"]["routing"]` |

Not every packet has every field. A telemetry packet might carry environment readings instead of device metrics, for example. Use `.get()` with a default so a missing key doesn't raise an error:

```python
metrics = packet["decoded"].get("telemetry", {}).get("deviceMetrics", {})
battery = metrics.get("batteryLevel")        # None if this packet didn't include it
```

**To see exactly what a packet contains**, log it whole for a minute:

```python
def on_packet(packet, api):
    if packet["portnum"] == "TELEMETRY_APP":
        api.log(packet)
```

---

## 4. The `api` object

Every function receives `api`, which is how an addon talks to MeshScope:

| Call | What it does |
|---|---|
| `api.log(*values)` | Write a line to this addon's Output pane |
| `api.notify(text)` | Pop up a notification in the app and add it to the **Activity** log |
| `api.store` | A dictionary that is saved to disk and restored the next time the addon starts. Keys must be strings; values must be JSON-friendly (numbers, strings, lists, dicts, `True`/`False`/`None`). It is saved when the addon stops and every 60 seconds. |
| `api.save()` | Save `api.store` right now |
| `api.local_num` | Your radio's node number (`None` if not connected) |
| `api.connected` | `True` while a radio (or the demo) is connected |
| `api.nodes()` | A list of every node MeshScope knows, as dicts with keys such as `num`, `id`, `longName`, `shortName`, `hwModel`, `lastHeard`, `battery`, `lat`, `lon`, `hopsAway`, `snr` |
| `api.node(x)` | One node, looked up by number or by `"!id"` string; `None` if unknown |
| `api.name(num)` | A node's display name |
| `api.send_text(text, to=..., channel=0)` | **Transmits** a text message. Without `to` it goes to everyone on the channel; with `to=` (a node number or `"!id"`) it's a direct message. See [Sending messages safely](#6-sending-messages-safely). |
| `api.send_text(..., reply_to=packet)` | Same, sent as a reply to a received message (it shows with a quote of that message) |
| `api.react(packet, emoji)` | **Transmits** an emoji reaction to a received text message, back where it came from: to the sender for a direct message, otherwise to the same channel. Shares the `send_text` rate limit. Example: react to "ping" with the hop count as `0️⃣`–`7️⃣`. |
| `api.request(kind, node)` | Ask a node for something: `"traceroute"`, `"ping"`, `"nodeinfo"`, `"position"` or `"telemetry"`. The result appears in the Activity tab. |

The same reference is built into the app: open the Addons tab with no addon selected and expand **Addon API reference**.

---

## 5. Examples

Each example is a complete addon: create a new Blank addon, paste the code over it, save and enable it.

### Count the busiest nodes

Keeps a running count of packets per node, saved between restarts, and logs the top five every minute.

```python
NAME = "Busiest nodes"
TICK_SECONDS = 60


def on_start(api):
    api.store.setdefault("counts", {})


def on_packet(packet, api):
    counts = api.store["counts"]
    name = packet["fromName"]
    counts[name] = counts.get(name, 0) + 1


def on_tick(api):
    top = sorted(api.store["counts"].items(), key=lambda item: item[1], reverse=True)[:5]
    if top:
        api.log("Top talkers: " + ", ".join(f"{name} ({count})" for name, count in top))
```

### Alert when a new node appears

Remembers every node ID it has seen, and pops up a notification the first time a new one shows up.

```python
NAME = "New node alert"


def on_start(api):
    # On the very first run, treat everything already known as "seen"
    api.store.setdefault("seen", [n["id"] for n in api.nodes()])


def on_packet(packet, api):
    seen = api.store["seen"]
    if packet["fromId"] not in seen:
        seen.append(packet["fromId"])
        api.notify(f'New node on the mesh: {packet["fromName"]} ({packet["fromId"]})')
```

### Low battery warning

Warns at most once a day per node when a node reports a battery level under 20%.

```python
import time

NAME = "Low battery warning"
THRESHOLD = 20  # percent


def on_packet(packet, api):
    if packet["portnum"] != "TELEMETRY_APP":
        return
    metrics = packet["decoded"].get("telemetry", {}).get("deviceMetrics", {})
    level = metrics.get("batteryLevel")
    if level is None or level > THRESHOLD:
        return
    warned = api.store.setdefault("warned", {})
    if time.time() - warned.get(packet["fromId"], 0) > 24 * 3600:
        warned[packet["fromId"]] = time.time()
        api.notify(f'{packet["fromName"]} battery is low: {level}%')
```

### Save text messages to a spreadsheet file

Appends every text message to a CSV file that Excel can open. `pathlib.Path.home()` is your user folder, so this writes `C:\Users\<you>\Documents\mesh-messages.csv`.

```python
import csv
import datetime
from pathlib import Path

NAME = "Message archive"
FILE = Path.home() / "Documents" / "mesh-messages.csv"


def on_packet(packet, api):
    if packet["portnum"] != "TEXT_MESSAGE_APP":
        return
    new_file = not FILE.exists()
    with FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["time", "from", "to", "source", "hops", "text"])
        writer.writerow([datetime.datetime.now().isoformat(timespec="seconds"), packet["fromName"],
                         packet["toName"], packet["source"], packet["hops"], packet["text"]])
```

### Reply "status" with your radio's battery

When someone sends your radio a **direct message** saying `status`, it replies with your battery level and how many nodes you can see. This one **transmits**, so read [Sending messages safely](#6-sending-messages-safely) first.

```python
NAME = "Status reply"


def on_packet(packet, api):
    if packet["portnum"] != "TEXT_MESSAGE_APP" or packet["source"] == "mqtt":
        return                                   # only messages heard over the air
    if packet["to"] != api.local_num:
        return                                   # only direct messages to my radio
    if (packet["text"] or "").strip().lower() != "status":
        return
    me = api.node(api.local_num) or {}
    battery = me.get("battery")
    battery_text = "external power" if battery and battery > 100 else f"{battery}%" if battery is not None else "unknown"
    api.send_text(f"Battery {battery_text}, {len(api.nodes())} nodes known", to=packet["from"])
```

MeshScope also ships templates you can start from (choose one in the **New** form): *Keyword alert*, *Low battery watch*, *Traffic stats* and *Ping auto-reply*.

---

## 6. Sending messages safely

`api.send_text()` and `api.request()` put real traffic on a shared radio channel, and every node in range has to share that airtime. Some guard rails are built in:

- Each addon can send **at most one message every 10 seconds**. Extra calls are skipped, and a note is written to Output.
- Messages are cut to **200 bytes**, the Meshtastic limit.
- In **demo mode** nothing is transmitted. Sends are simulated, so the demo is a safe place to try a replying addon.

The rest is up to your code:

- **Only reply to direct messages**, or to a very specific keyword. Never reply to every broadcast.
- **Never reply to your own radio**, and be careful with other bots: two auto-repliers answering each other will loop. Checking `packet["to"] == api.local_num` (a DM to you) and matching an exact command, as the example above does, avoids this.
- **Ignore MQTT packets** (`packet["source"] == "mqtt"`) when replying. Those senders may be hundreds of miles away and won't hear your radio.
- Use `api.request()` sparingly. The firmware itself rate-limits traceroutes to about one every 30 seconds.

---

## 7. Testing with the demo mesh

Click **Demo** (top right) to run a simulated mesh of about 20 nodes around Denver. Every few seconds it produces positions, telemetry, node info, neighbor info and text messages on two channels, including occasional direct messages to "your" radio. Your addons receive these packets exactly as they would from a real radio, with `packet["source"] == "demo"`.

Tips:

- Use **Messages** to send a direct message to a demo node. The demo doesn't answer it, but your addon's sends show up there and in **Activity**.
- To test a reply addon against a specific message, temporarily loosen the checks (for example, reply to any DM), then tighten them again before connecting a real radio.
- Click **Disconnect** to leave demo mode. Demo data never touches your real node database.

---

## 8. Troubleshooting

**Where do I see what's going on?** In the **Output** pane under the editor. Lines in red are errors, with the Python traceback. The counters above it show packets handled, errors and *dropped* packets.

| Symptom | Cause and fix |
|---|---|
| "Not saved: invalid syntax ..." | A Python syntax error. The line number is in the message; the old version is still on disk. |
| Status pill shows `error` right after enabling | The file failed while loading: usually a failed `import` or an error in code at the top of the file. The traceback is in Output. Fix it and save. |
| Status `running` but errors keep counting | One of your functions raised an exception. The addon keeps running and skips that packet. Look for the traceback in Output; it is usually a missing key, so use `.get()`. |
| Nothing happens | Is the addon **Enabled**? Is a radio or the demo connected? Add `api.log(packet["portnum"])` at the top of `on_packet` to confirm packets are arriving. |
| "dropped" goes up | `on_packet` is too slow (each addon queues up to 1000 packets). Move slow work, such as web requests, into `on_tick`. |
| A send was skipped with "rate limit" | Sends are limited to one every 10 seconds per addon. |
| `ModuleNotFoundError` | The installed app includes Python's standard library (`csv`, `json`, `datetime`, `re`, `math`, `statistics`, `urllib`, `smtplib`, `email` and more) plus `requests`, `numpy`, `PIL`, `yaml` and `paho.mqtt`. `sqlite3`, `winsound` and `zoneinfo` are **not** included, and you can't `pip install` into the installed app. |

**Where are the files?** Addons are stored in `%LOCALAPPDATA%\MeshScope\addons\` (paste that into File Explorer's address bar). Each addon is `<name>.py`, its saved `api.store` is `<name>.store.json`, and the enabled/disabled state is in `addons.json`. You can edit the `.py` files in another editor. Then re-select the addon in MeshScope to load the new code, and untick and tick **Enabled** to restart it.

---

## 9. Sharing addons

- **Export one:** select it and click **Export** in the editor header to get the `.py` file.
- **Export several:** tick the checkboxes in the addon list (or **Select all**) and click **Export** at the top of the list to get a `.zip`. With nothing ticked, every addon is exported.
- **Import:** click **Import** and choose `.py` files or a `.zip`. Imported addons start **disabled**, so read the code before you enable it.
