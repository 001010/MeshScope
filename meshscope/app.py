"""MeshScope entry point: start the local server and open the UI in an app window."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import APP_NAME, __version__
from .paths import data_dir, is_frozen, static_dir
from .server import App, start_server
from .state import MeshState

log = logging.getLogger("meshscope")


def setup_logging(debug: bool):
    d = data_dir()
    handlers = [RotatingFileHandler(d / "meshscope.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")]
    if sys.stderr is not None and not is_frozen():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("meshtastic").setLevel(logging.WARNING)
    # GUI builds have no console; the meshtastic library prints in places.
    if sys.stdout is None or is_frozen():
        sys.stdout = open(d / "stdout.log", "w", encoding="utf-8", buffering=1)
    if sys.stderr is None or is_frozen():
        sys.stderr = sys.stdout


def find_edge() -> str | None:
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return shutil.which("msedge")


def open_window(url: str, quit_event: threading.Event, app: App):
    """Show the UI in a chromeless Edge window; quit when it closes."""
    edge = find_edge()
    if edge:
        profile = data_dir() / "webview"
        cmd = [edge, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
               "--no-default-browser-check", "--disable-features=Translate", "--window-size=1440,920"]
        log.info("Launching UI window via Edge")
        proc = subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def watch():
            proc.wait()
            # Edge may hand off to an already-running instance with the same profile and exit
            # immediately; in that case fall back to heartbeat monitoring.
            if time.time() - started > 5:
                quit_event.set()
            else:
                heartbeat_watch(quit_event, app)

        started = time.time()
        threading.Thread(target=watch, daemon=True).start()
    else:
        log.info("Edge not found; opening default browser")
        webbrowser.open(url)
        threading.Thread(target=heartbeat_watch, args=(quit_event, app), daemon=True).start()


def heartbeat_watch(quit_event: threading.Event, app: App):
    """Quit once the UI has stopped polling for a while (browser tab closed)."""
    while not quit_event.wait(5):
        if app.last_poll and time.time() - app.last_poll > 45:
            log.info("UI stopped polling; exiting")
            quit_event.set()


def main(argv=None):
    ap = argparse.ArgumentParser(prog=APP_NAME, description="Meshtastic USB node explorer")
    ap.add_argument("--port", type=int, default=0, help="HTTP port for the UI (default: random)")
    ap.add_argument("--no-window", action="store_true", help="Don't open a window; just print the URL")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.debug)
    log.info("%s %s starting", APP_NAME, __version__)

    state = MeshState(data_dir() / "meshscope-db.json")
    app = App(state, static_dir())
    srv = start_server(app, args.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/?t={app.token}"
    log.info("UI at %s", url.split("?")[0])

    if args.no_window:
        print(url, flush=True)
    else:
        open_window(url, app.quit_event, app)

    try:
        while not app.quit_event.wait(30):
            state.save()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down")
        try:
            app.radio.disconnect()
        except Exception:
            log.exception("disconnect on exit")
        state.save(force=True)
        srv.shutdown()


if __name__ == "__main__":
    main()
