"""Minimal Chrome DevTools Protocol client for headless Edge (standard library only).

Used by the UI tests: Edge 157's --dump-dom / --enable-logging no longer return anything,
so the tests drive a real page over the DevTools protocol instead, collect uncaught
exceptions and console errors as they happen, and take screenshots in real time.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


class _WebSocket:
    """Just enough RFC 6455 for CDP: client handshake, masked text frames out, text frames in."""

    def __init__(self, url: str, timeout: float = 30):
        assert url.startswith("ws://")
        hostport, path = url[5:].split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake failed")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError(resp.split(b"\r\n", 1)[0].decode(errors="replace"))
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def send(self, text: str):
        data = text.encode()
        head = bytearray([0x81])  # FIN + text
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> str:
        parts = []
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if b1 & 0x80:
                mask = self._read(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read(n)))
            else:
                payload = self._read(n)
            op = b0 & 0x0F
            if op == 0x8:
                raise ConnectionError("websocket closed by browser")
            if op in (0x9, 0xA):  # ping/pong: ignore
                continue
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class HeadlessEdge:
    """One headless Edge page driven over CDP. Collects JS errors in ``self.errors``."""

    def __init__(self, edge: str, width: int = 1440, height: int = 900):
        self.profile = Path(tempfile.mkdtemp(prefix="ms-edge-"))
        self.proc = subprocess.Popen([edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                                      f"--user-data-dir={self.profile}", "--remote-debugging-port=0",
                                      f"--window-size={width},{height}", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = self.profile / "DevToolsActivePort"
        deadline = time.time() + 30
        while not (port_file.exists() and port_file.read_text().strip()):
            # note: msedge.exe may hand off to the real browser process and exit, so don't watch self.proc
            if time.time() > deadline:
                self.close()
                raise RuntimeError("headless Edge didn't start its DevTools endpoint")
            time.sleep(0.1)
        port = int(port_file.read_text().split()[0])
        self.port = port
        pages = []
        while not pages and time.time() < deadline:
            pages = [t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
                     if t.get("type") == "page"]
            time.sleep(0.1)
        self.ws = _WebSocket(pages[0]["webSocketDebuggerUrl"])
        self._id = 0
        self.errors: List[str] = []
        self.call("Runtime.enable")
        self.call("Page.enable")
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)

    def _event(self, msg: Dict[str, Any]):
        m, p = msg.get("method"), msg.get("params", {})
        if m == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails", {})
            ex = d.get("exception", {})
            self.errors.append(f"{d.get('url', '').split('/')[-1].split('?')[0]}:{d.get('lineNumber', 0) + 1} "
                               f"{ex.get('description') or d.get('text', '')}".split("\n")[0])
        elif m == "Runtime.consoleAPICalled" and p.get("type") == "error":
            self.errors.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args", [])))

    def call(self, method: str, timeout: float = 60, **params) -> Dict[str, Any]:
        self._id += 1
        my = self._id
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        self.ws.sock.settimeout(timeout)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == my:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            self._event(msg)

    def eval(self, expr: str) -> Any:
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True)
        return r.get("result", {}).get("value")

    def pump(self, seconds: float):
        """Let the page run, collecting error events."""
        end = time.time() + seconds
        while time.time() < end:
            self.eval("0")
            time.sleep(0.2)

    def goto(self, url: str, ready: str = "document.readyState === 'complete'", timeout: float = 30) -> bool:
        self.call("Page.navigate", url="about:blank")
        self.call("Page.navigate", url=url)
        end = time.time() + timeout
        while time.time() < end:
            if self.eval(f"!!({ready})"):
                return True
            time.sleep(0.2)
        return False

    def screenshot(self, path: Path):
        data = self.call("Page.captureScreenshot", format="png")["data"]
        Path(path).write_bytes(base64.b64decode(data))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        if getattr(self, "port", None):  # the browser may outlive the launcher process: close it over CDP
            try:
                info = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=5))
                bws = _WebSocket(info["webSocketDebuggerUrl"], timeout=5)
                bws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                time.sleep(0.5)
                bws.close()
            except Exception:
                pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
