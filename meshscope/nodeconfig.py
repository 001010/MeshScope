"""Read/edit the connected radio's configuration, and back it up / restore it.

Editing is schema-driven from the protobuf descriptors, so every firmware setting the
python library knows about is editable without hand-written forms. Every apply makes an
automatic backup first. Backups use the official ``meshtastic --export-config`` YAML, and
restores run ``meshtastic --configure`` through the in-app console, so the files are
interchangeable with the Meshtastic CLI.
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.protobuf.descriptor import FieldDescriptor as FD

log = logging.getLogger(__name__)

SENSITIVE = {("security", "private_key")}
# the sections meshtastic's Node.writeConfig() can write (it sys.exit()s on any other name)
WRITABLE = {
    "config": ["device", "position", "power", "network", "display", "lora", "bluetooth", "security"],
    "module": ["mqtt", "serial", "external_notification", "store_forward", "range_test", "telemetry",
               "canned_message", "audio", "remote_hardware", "neighbor_info", "detection_sensor",
               "ambient_lighting", "paxcounter", "traffic_management"],
}
# writing these makes the firmware reboot after the commit
REBOOTS = {"device", "position", "power", "network", "display", "lora", "bluetooth", "security"}
CHANNEL_ROLES = ["DISABLED", "PRIMARY", "SECONDARY"]
MAX_AUTO_BACKUPS = 20  # per node; manual and imported backups are never pruned


def _field_kind(f) -> str:
    if f.type == FD.TYPE_BOOL:
        return "bool"
    if f.type == FD.TYPE_ENUM:
        return "enum"
    if f.type in (FD.TYPE_FLOAT, FD.TYPE_DOUBLE):
        return "float"
    if f.type == FD.TYPE_STRING:
        return "string"
    if f.type == FD.TYPE_BYTES:
        return "bytes"
    if f.type == FD.TYPE_MESSAGE:
        return "message"
    return "int"


def _is_repeated(f) -> bool:
    rep = getattr(f, "is_repeated", None)
    if rep is not None:
        return bool(rep)
    return f.label == FD.LABEL_REPEATED


def _describe(msg, section: str, prefix: str = "") -> List[Dict[str, Any]]:
    out = []
    for f in msg.DESCRIPTOR.fields:
        path = f"{prefix}{f.name}"
        kind = _field_kind(f)
        val = getattr(msg, f.name)
        entry: Dict[str, Any] = {"path": path, "kind": kind}
        if _is_repeated(f):
            if kind == "bytes":
                entry.update(kind="list", value=[base64.b64encode(v).decode() for v in val], readOnly=True)
            elif kind == "message":
                continue
            elif kind == "enum":
                entry.update(kind="list", value=[f.enum_type.values_by_number[v].name if v in f.enum_type.values_by_number
                                                 else v for v in val], readOnly=True)
            else:
                entry.update(kind="list", value=list(val), readOnly=True)
        elif kind == "message":
            out.extend(_describe(val, section, prefix=f"{path}."))
            continue
        elif kind == "enum":
            entry["options"] = [v.name for v in f.enum_type.values]
            ev = f.enum_type.values_by_number.get(val)
            entry["value"] = ev.name if ev else val
        elif kind == "bytes":
            entry["value"] = base64.b64encode(val).decode()
            entry["readOnly"] = True
            if (section, f.name) in SENSITIVE:
                entry["value"] = "(hidden)"
        else:
            entry["value"] = round(val, 6) if kind == "float" else val
        out.append(entry)
    return out


def schema(iface) -> Dict[str, Any]:
    ln = iface.localNode
    sections = []
    for group, root in (("config", ln.localConfig), ("module", ln.moduleConfig)):
        names = {f.name for f in root.DESCRIPTOR.fields if f.type == FD.TYPE_MESSAGE}
        for name in WRITABLE[group]:
            if name in names:
                sections.append({"group": group, "name": name, "reboots": name in REBOOTS,
                                 "fields": _describe(getattr(root, name), name)})
    me = iface.getMyUser() or {}
    channels = []
    for c in ln.channels or []:
        s = c.settings
        channels.append({
            "index": c.index, "role": CHANNEL_ROLES[c.role] if c.role < 3 else c.role, "name": s.name,
            "psk": base64.b64encode(s.psk).decode(), "uplink_enabled": s.uplink_enabled,
            "downlink_enabled": s.downlink_enabled, "position_precision": s.module_settings.position_precision,
        })
    num = iface.myInfo.my_node_num if iface.myInfo else None
    return {"node": {"num": num, "id": f"!{num:08x}" if num is not None else "", "name": me.get("longName", "")},
            "owner": {"long_name": me.get("longName", ""), "short_name": me.get("shortName", ""),
                      "is_licensed": bool(me.get("isLicensed"))},
            "sections": sections, "channels": channels}


def _section(ln, name: str):
    if name in WRITABLE["config"]:
        return ln.localConfig, getattr(ln.localConfig, name)
    if name in WRITABLE["module"]:
        return ln.moduleConfig, getattr(ln.moduleConfig, name)
    raise ValueError(f"{name} can't be written")


def _set_path(msg, path: str, value):
    parts = path.split(".")
    for p in parts[:-1]:
        msg = getattr(msg, p)
    f = msg.DESCRIPTOR.fields_by_name[parts[-1]]
    kind = _field_kind(f)
    if _is_repeated(f) or kind in ("bytes", "message"):
        raise ValueError(f"{path} is read-only here")
    if kind == "bool":
        value = bool(value)
    elif kind == "enum":
        value = f.enum_type.values_by_name[value].number if isinstance(value, str) else int(value)
    elif kind == "float":
        value = float(value)
    elif kind == "int":
        value = int(value)
    else:
        value = str(value)
    setattr(msg, parts[-1], value)


class ConfigManager:
    def __init__(self, app, backup_dir: Path):
        self.app = app
        self.dir = backup_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _iface(self):
        radio = self.app.radio
        if getattr(radio, "SOURCE", "") != "radio" or not radio.connected:
            raise RuntimeError("Connect to a real radio first (configuration isn't available in demo mode)")
        return radio.iface

    def schema(self) -> Dict[str, Any]:
        return schema(self._iface())

    # ---------------------------------------------------------------- apply
    def apply(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        """changes = {"sections": {name: {path: value}}, "owner": {...}, "channels": [{index, ...}]}."""
        iface = self._iface()
        ln = iface.localNode
        sections = {k: v for k, v in (changes.get("sections") or {}).items() if v}
        chans = changes.get("channels") or []
        owner = changes.get("owner")
        if not (sections or chans or owner):
            raise ValueError("Nothing to change")
        # validate everything on copies before touching the radio or its cached config
        staged = {}
        for name, fields in sections.items():
            _root, live = _section(ln, name)
            copy = type(live)()
            copy.CopyFrom(live)
            for path, value in fields.items():
                try:
                    _set_path(copy, path, value)
                except (KeyError, ValueError, TypeError, AttributeError) as e:
                    raise ValueError(f"{name}.{path}: invalid value {value!r} ({e})")
            staged[name] = (live, copy)
        for ch in chans:
            idx = int(ch["index"])
            if not 0 <= idx < len(ln.channels or []):
                raise ValueError(f"No channel {idx}")
            if "psk" in ch:
                base64.b64decode(ch["psk"], validate=True)
            if ch.get("role") not in (None, *CHANNEL_ROLES):
                raise ValueError(f"Bad channel role {ch.get('role')}")
        if owner and owner.get("short_name") and len(owner["short_name"]) > 4:
            raise ValueError("Short name can be at most 4 characters")
        with self._lock:
            backup = self.backup("auto: before edit")
            written = []
            for live, copy in staged.values():
                live.CopyFrom(copy)
            ln.beginSettingsTransaction()
            try:
                for name in sections:
                    ln.writeConfig(name)
                    written.append(name)
                for ch in chans:
                    c = ln.channels[int(ch["index"])]
                    if "name" in ch:
                        c.settings.name = str(ch["name"])[:11]
                    if "psk" in ch:
                        c.settings.psk = base64.b64decode(ch["psk"])
                    if "uplink_enabled" in ch:
                        c.settings.uplink_enabled = bool(ch["uplink_enabled"])
                    if "downlink_enabled" in ch:
                        c.settings.downlink_enabled = bool(ch["downlink_enabled"])
                    if "position_precision" in ch:
                        c.settings.module_settings.position_precision = int(ch["position_precision"])
                    if "role" in ch and ch["index"] != 0:  # never demote the primary
                        c.role = CHANNEL_ROLES.index(ch["role"])
                    ln.writeChannel(int(ch["index"]))
                    written.append(f"channel {ch['index']}")
                if owner:
                    ln.setOwner(long_name=owner.get("long_name") or None, short_name=owner.get("short_name") or None,
                                is_licensed=bool(owner.get("is_licensed")))
                    written.append("owner")
            except SystemExit as e:  # the library exits on some errors
                raise RuntimeError(f"The meshtastic library rejected the change: {e}")
            finally:
                ln.commitSettingsTransaction()
            reboot = any(n in REBOOTS for n in sections)
            self.app.state.add_log("conn", f"Wrote config: {', '.join(written) or 'nothing'} (backup {backup['file']})"
                                   + ("; the radio will reboot" if reboot else ""))
            try:
                self.app.radio.refresh_channels()
            except Exception:
                pass
            return {"ok": True, "written": written, "backup": backup, "reboot": reboot}

    # -------------------------------------------------------------- backups
    def backup(self, label: str = "") -> Dict[str, Any]:
        import meshtastic.__main__ as cli

        iface = self._iface()
        yaml_text = cli.export_config(iface)
        num = iface.myInfo.my_node_num
        me = iface.getMyUser() or {}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", (label or "manual").lower()).strip("-")[:40] or "manual"
        path = self.dir / f"{num:08x}_{stamp}_{slug}.yaml"
        header = (f"# MeshScope backup\n# label: {label or 'manual'}\n# node: !{num:08x} {me.get('longName', '')}\n"
                  f"# created: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        path.write_text(header + yaml_text, encoding="utf-8")
        if label.startswith("auto"):
            self._prune(num)
        return self._meta(path)

    def _prune(self, num: int):
        autos = sorted((m for m in self.list_backups() if m["file"].startswith(f"{num:08x}_")
                        and m["label"].startswith("auto")), key=lambda m: -m["mtime"])
        for m in autos[MAX_AUTO_BACKUPS:]:
            try:
                (self.dir / m["file"]).unlink()
            except OSError:
                pass

    def auto_backup_on_connect(self):
        """Keep a recent known-good copy of each radio's config (at most one a day)."""
        try:
            num = self._iface().myInfo.my_node_num
            recent = [m for m in self.list_backups() if m["file"].startswith(f"{num:08x}_")
                      and time.time() - m["mtime"] < 86400]
            if not recent:
                self.backup("auto: on connect")
        except Exception:
            log.exception("automatic config backup failed")

    def _meta(self, path: Path) -> Dict[str, Any]:
        head = {}
        try:
            with path.open(encoding="utf-8") as fh:
                for _ in range(6):
                    line = fh.readline()
                    m = re.match(r"# (label|node|created): (.*)", line)
                    if m:
                        head[m.group(1)] = m.group(2).strip()
        except OSError:
            pass
        return {"file": path.name, "label": head.get("label", path.stem), "node": head.get("node", ""),
                "created": head.get("created", ""), "size": path.stat().st_size, "mtime": path.stat().st_mtime}

    def list_backups(self) -> List[Dict[str, Any]]:
        return sorted((self._meta(p) for p in self.dir.glob("*.yaml")), key=lambda m: -m["mtime"])

    def _path(self, name: str) -> Path:
        p = (self.dir / Path(name).name).resolve()
        if p.parent != self.dir.resolve() or not p.exists():
            raise FileNotFoundError(name)
        return p

    def read(self, name: str) -> bytes:
        return self._path(name).read_bytes()

    def delete(self, name: str):
        self._path(name).unlink()

    def upload(self, name: str, data: bytes) -> Dict[str, Any]:
        text = data.decode("utf-8", "replace")
        if "config" not in text and "channel_url" not in text and "channelUrl" not in text:
            raise ValueError("That doesn't look like a Meshtastic configuration YAML")
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(name).stem)[:60]
        path = self.dir / f"imported_{time.strftime('%Y%m%d-%H%M%S')}_{stem}.yaml"
        if not text.startswith("# MeshScope backup"):
            text = f"# MeshScope backup\n# label: imported {Path(name).name}\n# node: \n# created: {time.strftime('%Y-%m-%d %H:%M:%S')}\n" + text
        path.write_text(text, encoding="utf-8")
        return self._meta(path)

    def restore(self, name: str) -> Dict[str, Any]:
        path = self._path(name)
        self._iface()
        safety = self.backup("auto: before restore")
        res = self.app.console.run_args(["--configure", str(path)])
        return {"ok": True, "safetyBackup": safety, **res}
