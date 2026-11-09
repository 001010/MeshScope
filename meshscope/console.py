"""Run `meshtastic` CLI commands against the already-open USB connection, and show the device log.

The serial port can only be opened once, so instead of spawning the CLI we drive its
own argument parser and ``onConnected`` routine in-process with our interface.
"""

from __future__ import annotations

import argparse
import io
import logging
import shlex
import sys
import threading
import time
import traceback
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Arguments that would open another connection, block forever, or fight our session
BLOCKED = {
    "--port", "--serial", "-s", "--host", "--tcp", "-t", "--ble", "-b", "--ble-scan", "--ble-logdir",
    "--listen", "--tunnel", "--noproto", "--seriallog", "--test", "--reply", "--debug", "--support",
    "--wait-to-disconnect",
}
DANGEROUS = {
    "--factory-reset", "--factory-reset-device", "--reset-nodedb", "--reboot", "--reboot-ota",
    "--enter-dfu", "--shutdown", "--remove-node", "--configure", "--ota-update", "--set-owner",
    "--ch-del", "--seturl", "--set-canned-message", "--set-ringtone",
}


class _NoCloseInterface:
    """Proxy that hands the CLI our interface but ignores its attempts to close it."""

    def __init__(self, iface):
        object.__setattr__(self, "_iface", iface)

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._iface, name)

    def __setattr__(self, name, value):
        setattr(self._iface, name, value)


class _LineWriter(io.TextIOBase):
    def __init__(self, console: "Console", kind: str):
        self.console, self.kind, self.buf = console, kind, ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.console.add(self.kind, line)
        return len(s)

    def flush(self):
        if self.buf:
            self.console.add(self.kind, self.buf)
            self.buf = ""

    def isatty(self):
        return False


class Console:
    def __init__(self, app):
        self.app = app
        self.lines: deque = deque(maxlen=4000)
        self.seq = 0
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.busy = False
        self.history: List[str] = []

    def add(self, kind: str, text: str):
        with self._lock:
            for part in str(text).splitlines() or [""]:
                self.seq += 1
                self.lines.append({"seq": self.seq, "t": time.time(), "kind": kind, "text": part})

    def device_line(self, line: str):
        self.add("device", line.rstrip())

    def since(self, seq: int) -> Dict[str, Any]:
        with self._lock:
            return {"seq": self.seq, "busy": self.busy, "lines": [l for l in self.lines if l["seq"] > seq][-1500:]}

    def clear(self):
        with self._lock:
            self.lines.clear()

    def run(self, argline: str, confirmed: bool = False) -> Dict[str, Any]:
        argline = (argline or "").strip()
        if argline.startswith("meshtastic "):
            argline = argline[len("meshtastic "):]
        try:
            args = shlex.split(argline, posix=True)
        except ValueError as e:
            raise ValueError(f"Could not parse command: {e}")
        if not args:
            raise ValueError("Type CLI arguments, e.g. --info or --get lora")
        flags = {a.split("=", 1)[0] for a in args if a.startswith("-")}
        blocked = sorted(flags & BLOCKED)
        if blocked:
            raise ValueError(f"{', '.join(blocked)} can't be used here (MeshScope already owns the connection)")
        danger = sorted(flags & DANGEROUS)
        if danger and not confirmed:
            return {"confirm": True, "flags": danger}
        return self.run_args(args, display=argline)

    def run_args(self, args: List[str], display: Optional[str] = None) -> Dict[str, Any]:
        """Run pre-split CLI arguments (no shell-style parsing, so Windows paths are safe)."""
        radio = self.app.radio
        if not getattr(radio, "connected", False) or getattr(radio, "SOURCE", "") != "radio":
            raise RuntimeError("Connect to a real radio first (the CLI isn't available in demo mode)")
        if self.busy:
            raise RuntimeError("A command is still running")
        display = display or " ".join(args)
        self.history.append(display)
        self.add("cmd", f"meshtastic {display}")
        self.busy = True
        threading.Thread(target=self._run, args=(args, radio.iface), daemon=True, name="cli").start()
        return {"ok": True}

    def _run(self, args: List[str], iface):
        out, err = _LineWriter(self, "out"), _LineWriter(self, "err")
        started = time.time()
        with self._run_lock:
            old_argv = sys.argv
            try:
                import meshtastic.__main__ as cli
                from meshtastic import mt_config

                parser = argparse.ArgumentParser(prog="meshtastic", add_help=False)
                mt_config.parser = parser
                sys.argv = ["meshtastic"] + args
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        cli.initParser()
                        cli.onConnected(_NoCloseInterface(iface))
                    except SystemExit as e:
                        if e.code not in (0, None):
                            if isinstance(e.code, str):
                                err.write(e.code + "\n")
                            else:
                                err.write(f"(exit status {e.code})\n")
            except Exception:
                err.write(traceback.format_exc())
            finally:
                sys.argv = old_argv
                out.flush()
                err.flush()
                self.busy = False
                self.add("done", f"finished in {time.time() - started:.1f}s")
