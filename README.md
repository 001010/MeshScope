# MeshScope

A Windows desktop app for exploring a Meshtastic mesh through a radio connected over USB.

- **Nodes**: every node seen, with hops, SNR, last heard, distance, battery, hardware and role. Search, sort and filter.
- **Actions** on any node: **traceroute**, **ping**, **request node info**, plus request position and request telemetry. Any hop limit from 1 to 7.
- **Map**: nodes with GPS positions, and the radio links between them coloured by SNR (Street, Topographic and Satellite layers).
- **Topology**: a force-directed graph of the local mesh. It also shows nodes that have no GPS position.
- **Reach**: pick any node and a hop limit. MeshScope floods outward over the observed links, the same way Meshtastic rebroadcasts a packet. It shows which nodes the message reaches at each hop, the coverage footprint, the farthest node reached and the nodes just past the hop limit.
- **Activity**: request results, including full traceroute paths with SNR for each hop in both directions, and a live packet log.
- **Demo mode**: a simulated mesh for trying the app without a radio.

## Install

Run `dist\MeshScope-<version>-win64.msi`. It installs to `Program Files\MeshScope` and adds Start-menu and desktop shortcuts. Python and the VC++ runtime are bundled in the installer.

The UI opens in a Microsoft Edge app window, so it has no browser tabs or address bar. Edge ships with Windows 10 and 11. The map tiles need an internet connection; everything else works offline.

## Using it

1. Plug the radio in over USB. If Windows doesn't create a COM port, install the USB driver for its chip: CP210x for many ESP32 boards, or CH9102/CH340 for others. Nordic nRF52 and ESP32-S3 boards use the built-in driver.
2. Close any other program that is using the port, such as the Meshtastic web client, the Python CLI or a serial monitor.
3. Pick the port (likely Meshtastic devices are starred) and click **Connect**.

### How links are discovered
Each link is a direct radio hop that was actually observed. MeshScope learns them from several sources:

| Source | Meaning |
|---|---|
| `nodedb` / `direct` | Your radio heard the node with zero hops used |
| `traceroute` | Each hop in a traceroute reply, with SNR in both directions |
| `neighborinfo` | A node's NeighborInfo broadcast listing the nodes it hears |
| `relay` | Inferred from the `relay_node` byte of multi-hop packets (shown dashed) |

Traceroutes are the best way to fill in the map and the reach analysis. Enabling the NeighborInfo module on your nodes also helps.

### Notes on the actions
- **Ping**: Meshtastic has no ICMP. MeshScope sends a small want-ACK packet on the private app port and times the destination's routing ACK. "Implicit ACK" means a neighbour was heard repeating the packet, but the destination never confirmed it.
- **Traceroute**: the firmware limits traceroutes to roughly one every 30 seconds. If you send them faster, you get a "rate limited" result.
- **Node info**: sends your own User record and asks for theirs. This also refreshes public keys that encrypted DMs need.

## Data

`%LOCALAPPDATA%\MeshScope\` holds:
- `meshscope-db.json`: the node and link history, which is kept between sessions. Clear it with **Forget** on a node, or delete the file.
- `meshscope.log`: the diagnostic log.

## Building from source

Requires Python 3.10+ on Windows.

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

The script creates `.venv`, installs the dependencies, runs `tests\smoke_test.py` (an end-to-end check of the API over the simulated mesh) and builds `dist\MeshScope-<version>-win64.msi` with cx_Freeze.

To run it without building:

```powershell
.venv\Scripts\python -m meshscope            # opens the app window
.venv\Scripts\python -m meshscope --no-window # prints the URL instead
```

`tests\screenshots.py <dir>` takes a headless-Edge screenshot of every tab in demo mode.

When you release a new version, bump `__version__` in `meshscope/__init__.py`. Keep `UPGRADE_CODE` in `setup.py` unchanged, so that a new MSI upgrades an existing install instead of installing beside it.

## Layout

```
meshscope/
  app.py      entry point: local server + Edge app window
  server.py   localhost JSON API (token-protected) + static files
  radio.py    USB serial connection, packet decoding, actions
  state.py    node/link store, topology, reach (flood) analysis, persistence
  demo.py     simulated mesh that drives the real packet-handling code
  static/     UI (vanilla JS + Leaflet)
setup.py      cx_Freeze / MSI configuration
build.ps1     one-shot build
```
