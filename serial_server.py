#!/usr/bin/env python3
"""
ESP-Agent Serial Monitor Server.

HTTP + SSE server that bridges serial port and web browser.
Agent (Claude) uses the REST API; humans watch the Web UI.

Usage:
    python serial_server.py [--port 8099] [--host 127.0.0.1]
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import tempfile
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse

try:
    import serial  # noqa: F401
    import serial.tools.list_ports  # noqa: F401
except ImportError:
    print("pyserial not found.  Install it with system Python:")
    print("  pip install pyserial")
    print("Then run again with your system Python (not the ESP-IDF venv).")
    sys.exit(1)

# ── globals (shared across threads) ──────────────────────────────
SSE_CLIENTS: list["queue.Queue"] = []      # each SSE connection gets a queue
SSE_LOCK = threading.Lock()

SERIAL_PORT: serial.Serial | None = None
SERIAL_LOCK = threading.Lock()
MONITOR_THREAD: threading.Thread | None = None
MONITOR_RUNNING = False

BUILD_LOG = queue.Queue()                  # build/flash output for agent polling

# detected IDF environment (populated on first build/flash)
IDF_PATH: str | None = None
IDF_VENV: str | None = None


# ── broadcast to all SSE clients ─────────────────────────────────
def sse_broadcast(event: str, data: dict):
    """Push a JSON event to every connected SSE client."""
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with SSE_LOCK:
        dead = []
        for q in SSE_CLIENTS:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            SSE_CLIENTS.remove(q)


def sse_register() -> queue.Queue:
    q = queue.Queue(maxsize=256)
    with SSE_LOCK:
        SSE_CLIENTS.append(q)
    return q


def sse_unregister(q: queue.Queue):
    with SSE_LOCK:
        if q in SSE_CLIENTS:
            SSE_CLIENTS.remove(q)


# ── IDF environment detection ────────────────────────────────────
COMMON_IDF_ROOTS = [
    Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "esp-idf",
    Path("D:\\Programs\\esp-idf"),
    Path("C:\\esp"),
    Path("C:\\esp-idf"),
    Path.home() / "esp",
]


def find_idf() -> tuple[Path | None, str | None]:
    """Return (idf_path, version_str) for the first valid IDF installation."""
    for root in COMMON_IDF_ROOTS:
        if not root.exists():
            continue
        for subdir in sorted(root.iterdir(), reverse=True):
            if not subdir.name.startswith("v") or not subdir.is_dir():
                continue
            idf_dir = subdir / "esp-idf"
            if (idf_dir / "tools" / "idf.py").exists():
                return idf_dir.resolve(), subdir.name
    return None, None


def find_venv(idf_version: str) -> Path | None:
    """Find the matching Python venv Scripts dir for an IDF version."""
    espressif = Path.home() / ".espressif" / "python_env"
    if not espressif.exists():
        return None
    # venv dirs use major.minor only (e.g. "idf5.5_py3.10_env"), so strip patch
    parts = idf_version.replace('v', '').split('.')
    major_minor = '.'.join(parts[:2])
    prefix = f"idf{major_minor}_py3"
    for venv_dir in espressif.iterdir():
        if venv_dir.name.startswith(prefix) and venv_dir.is_dir():
            scripts = venv_dir / "Scripts"
            if (scripts / "python.exe").exists():
                return scripts.resolve()
    return None


def detect_target(project_dir: Path) -> str:
    """Read target chip from sdkconfig."""
    sdkconfig = project_dir / "sdkconfig"
    if not sdkconfig.exists():
        return "unknown"
    for line in sdkconfig.read_text(encoding="utf-8").splitlines():
        if line.startswith("CONFIG_IDF_TARGET="):
            return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


# ── serial monitor ───────────────────────────────────────────────
def _monitor_loop(port_name: str, baud: int):
    """Background thread — reads serial and broadcasts via SSE."""
    global SERIAL_PORT, MONITOR_RUNNING
    try:
        ser = serial.Serial(port_name, baud, timeout=0.1)
        with SERIAL_LOCK:
            SERIAL_PORT = ser
        MONITOR_RUNNING = True
        sse_broadcast("serial", {"status": "connected", "port": port_name})

        buf = b""
        while MONITOR_RUNNING:
            try:
                data = ser.read(1024)
                if data:
                    buf += data
                    # emit line-by-line if possible
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("utf-8", errors="replace").strip("\r")
                        if text:
                            sse_broadcast("serial", {"data": text})
                    # flush remaining partial line every 500ms
                    if buf and time.time() % 1 < 0.5:
                        text = buf.decode("utf-8", errors="replace").strip("\r")
                        if text:
                            sse_broadcast("serial", {"data": text})
                        buf = b""
            except serial.SerialException:
                break
    except Exception as e:
        sse_broadcast("serial", {"status": "error", "message": str(e)})
    finally:
        with SERIAL_LOCK:
            if SERIAL_PORT and SERIAL_PORT.is_open:
                try:
                    SERIAL_PORT.close()
                except Exception:
                    pass
            SERIAL_PORT = None
        MONITOR_RUNNING = False
        sse_broadcast("serial", {"status": "disconnected"})


def serial_open(port: str, baud: int = 115200) -> str:
    """Open serial port in a background monitor thread."""
    global MONITOR_THREAD, MONITOR_RUNNING
    if MONITOR_RUNNING:
        return "already connected"
    MONITOR_RUNNING = True
    t = threading.Thread(target=_monitor_loop, args=(port, baud), daemon=True)
    MONITOR_THREAD = t
    t.start()
    time.sleep(0.3)  # let it connect
    return "ok"


def serial_close():
    """Stop the monitor thread and close the port."""
    global MONITOR_RUNNING
    MONITOR_RUNNING = False
    time.sleep(0.2)


def serial_send(text: str):
    """Send data to the open serial port."""
    with SERIAL_LOCK:
        if SERIAL_PORT and SERIAL_PORT.is_open:
            SERIAL_PORT.write(text.encode("utf-8"))
            SERIAL_PORT.flush()
            return True
    return False


def serial_reset() -> bool:
    """
    Hardware reset without going into download mode.

    DTR → EN (inverted transistor): DTR=True → EN low → reset
    RTS → GPIO0 (inverted transistor): RTS=True → GPIO0 low → download
    """
    with SERIAL_LOCK:
        if SERIAL_PORT and SERIAL_PORT.is_open:
            try:
                # Idle: no reset, normal boot
                SERIAL_PORT.dtr = False   # EN high
                SERIAL_PORT.rts = False   # GPIO0 high
                time.sleep(0.05)
                # Pulse reset: EN low, GPIO0 stays high
                SERIAL_PORT.dtr = True    # EN low → reset
                time.sleep(0.15)
                # Release: EN high, GPIO0 still high → boot from flash
                SERIAL_PORT.dtr = False   # EN high
                time.sleep(0.05)
                return True
            except Exception:
                pass
    return False


# ── subprocess runner (build / flash) ────────────────────────────
def run_idf_command(cmd: list[str], label: str, project_dir: str) -> dict:
    """
    Run an idf.py subprocess with proper IDF environment (MSYSTEM fix,
    Python venv, export.bat).  Streams output via SSE in real-time.
    Returns {"status": "ok"|"fail", "exit_code": N, "output": [...]}.
    """
    global IDF_PATH, IDF_VENV
    sse_broadcast("system", {"status": f"{label}: starting"})

    # auto-detect IDF on first use
    if not IDF_PATH:
        found, ver = find_idf()
        if not found:
            msg = f"{label}: no ESP-IDF installation found"
            sse_broadcast("system", {"status": msg})
            return {"status": "error", "exit_code": -1, "output": [msg]}
        IDF_PATH = str(found)
        sse_broadcast("system", {"status": f"{label}: IDF at {IDF_PATH} ({ver})"})

    # auto-detect matching Python venv
    if not IDF_VENV:
        version = Path(IDF_PATH).parent.name
        v = find_venv(version)
        if v:
            IDF_VENV = str(v)

    project = Path(project_dir).resolve()
    target = detect_target(project)

    # build a temporary batch file with full IDF environment setup
    batch_lines = ["@echo on", "set MSYSTEM="]
    if IDF_VENV:
        # Prepend venv to PATH so python resolves to the right version;
        # also set IDF_PYTHON_ENV_PATH so idf_tools uses the existing venv
        # instead of checking for a non-existent one (e.g. py3.14).
        batch_lines.append(f"set PATH={IDF_VENV};%PATH%")
        idf_python_env = str(Path(IDF_VENV).parent)
        batch_lines.append(f"set IDF_PYTHON_ENV_PATH={idf_python_env}")
    batch_lines.append(f"cd /d {project}")
    batch_lines.append(f"set IDF_PATH={IDF_PATH}")
    batch_lines.append(f"call {IDF_PATH}\\export.bat")
    batch_lines.append("echo ======================================")
    batch_lines.append(f"echo {label} {project.name} ({target})")
    batch_lines.append("echo ======================================")
    batch_lines.append(" ".join(cmd) + " 2>&1")
    batch_lines.append("exit /b %ERRORLEVEL%")

    batch_content = "\r\n".join(batch_lines) + "\r\n"
    tmp = Path(tempfile.gettempdir()) / f"esp-agent-{label}.bat"
    tmp.write_text(batch_content, encoding="utf-8")
    sse_broadcast("system", {"data": f"[batch] {tmp}"})

    lines = []
    try:
        proc = subprocess.Popen(
            ["cmd.exe", "/c", str(tmp)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line:
                lines.append(line)
                sse_broadcast(label, {"data": line})

        proc.wait()
        ok = proc.returncode == 0
        status = "success" if ok else f"failed (exit {proc.returncode})"
        sse_broadcast("system", {"status": f"{label}: {status}"})
        return {"status": "ok" if ok else "fail", "exit_code": proc.returncode, "output": lines}
    except Exception as e:
        sse_broadcast("system", {"status": f"{label}: error — {e}"})
        return {"status": "error", "exit_code": -1, "output": lines}


# ── HTTP handler ─────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # quieter

    # ---- helpers --------------------------------------------------
    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _html(self, path: str):
        """Serve a static file relative to the script directory."""
        static_dir = Path(__file__).parent / "static"
        filepath = static_dir / path.lstrip("/")
        if not filepath.exists() or not filepath.is_file():
            self.send_response(404)
            self.end_headers()
            return
        # simple content-type map
        ext_map = {".html": "text/html", ".js": "application/javascript",
                   ".css": "text/css", ".png": "image/png", ".ico": "image/x-icon"}
        ctype = ext_map.get(filepath.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(filepath.read_bytes())

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return ""
        return self.rfile.read(length).decode("utf-8")

    # ---- routes ---------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/ports":
            ports = [
                {"device": p.device, "description": p.description}
                for p in serial.tools.list_ports.comports()
            ]
            return self._json(ports)

        if path == "/api/status":
            with SERIAL_LOCK:
                connected = SERIAL_PORT is not None and SERIAL_PORT.is_open
                port_name = SERIAL_PORT.port if connected else None
            return self._json({
                "monitor_running": MONITOR_RUNNING,
                "connected": connected,
                "port": port_name,
            })

        if path == "/api/events":
            return self._handle_sse()

        # serve the web UI
        if path == "/" or path == "":
            path = "/index.html"
        return self._html(path)

    def _handle_sse(self):
        """SSE endpoint — keeps connection open and streams events."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = sse_register()
        try:
            # send an initial connected event
            self.wfile.write(b"event: system\ndata: {\"status\": \"connected\"}\n\n")
            self.wfile.flush()

            while True:
                try:
                    payload = q.get(timeout=5)
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            sse_unregister(q)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = json.loads(self._read_body()) if self.headers.get("Content-Length", "0") != "0" else {}

        if path == "/api/monitor/start":
            port = body.get("port", "")
            baud = int(body.get("baud", 115200))
            if not port:
                return self._json({"error": "port required"}, 400)
            msg = serial_open(port, baud)
            return self._json({"status": msg})

        if path == "/api/monitor/stop":
            serial_close()
            return self._json({"status": "ok"})

        if path == "/api/send":
            data = body.get("data", "")
            ok = serial_send(data)
            return self._json({"sent": ok})

        if path == "/api/reset":
            ok = serial_reset()
            return self._json({"reset": ok})

        if path == "/api/build":
            project = body.get("project_dir", ".")
            result = run_idf_command(["idf.py", "build"], "build", project)
            return self._json(result)

        if path == "/api/flash":
            project = body.get("project_dir", ".")
            port = body.get("port", "")
            # close serial before flashing (release COM port)
            serial_close()
            time.sleep(0.3)
            cmd = ["idf.py", "-p", port, "flash"] if port else ["idf.py", "flash"]
            result = run_idf_command(cmd, "flash", project)
            return self._json(result)

        return self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server — handles each request in a new thread."""
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="ESP-Agent Serial Monitor Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8099, help="HTTP port (default 8099)")
    parser.add_argument("--idf-path", type=str, default=None,
                        help="ESP-IDF installation path (e.g. D:\\Programs\\esp-idf\\v5.5.3\\esp-idf)")
    args = parser.parse_args()

    global IDF_PATH
    if args.idf_path:
        p = Path(args.idf_path)
        if (p / "tools" / "idf.py").exists():
            IDF_PATH = str(p.resolve())
            print(f"  IDF: {IDF_PATH}")
        else:
            print(f"  WARNING: --idf-path {args.idf_path} invalid (no tools/idf.py)")

    server = ThreadedHTTPServer((args.host, args.port), Handler)
    print(f"\n  ESP-Agent Serial Server running at:")
    print(f"  http://{args.host}:{args.port}/")
    print(f"\n  Web UI  →  http://{args.host}:{args.port}/")
    print(f"  API     →  http://{args.host}:{args.port}/api/ports")
    print(f"\n  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        serial_close()
        server.shutdown()


if __name__ == "__main__":
    main()
