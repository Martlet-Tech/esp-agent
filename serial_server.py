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

HTTP_SERVER: HTTPServer | None = None      # set by main() for /api/shutdown


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


# ── error classification (agent-friendly) ──────────────────────────
BUILD_ERROR_TYPES = {
    "toolchain_version_mismatch": {
        "keywords": ["unsupported non-standard extension"],
        "suggestion": "Update IDF_TOOLS_PATH to a version matching the IDF version (same major.minor). Re-run the ESP-IDF installer or download matching tools.",
        "recoverable": False,
    },
    "tool_version_mismatch": {
        "keywords": ["Tool doesn't match supported version from list"],
        "suggestion": "The compiler found in IDF_TOOLS_PATH doesn't match the version required by this IDF version. This usually happens when IDF_TOOLS_PATH points to a different IDF version's tools. Verify IDF_TOOLS_PATH matches the IDF version and run 'idf.py fullclean'.",
        "recoverable": True,
    },
    "compiler_not_found": {
        "keywords": ["CMAKE_C_COMPILER", "not found in the PATH"],
        "suggestion": "IDF_TOOLS_PATH is missing the required toolchain binaries. Check that IDF_TOOLS_PATH points to a complete ESP-IDF tools installation.",
        "recoverable": True,
    },
    "cmake_failed": {
        "keywords": ["Configuring incomplete, errors occurred!"],
        "suggestion": "CMake configuration failed. Check the build log for details. Common causes: missing dependencies, stale build directory (run 'idf.py fullclean' or delete build/).",
        "recoverable": True,
    },
    "python_dependency": {
        "keywords": [["ModuleNotFoundError"], ["No module named"],
                      ["pip", "requirements"], ["Python virtual environment", "not found"]],
        "suggestion": "Python environment incomplete. The IDF Python virtual environment is missing or corrupted. Re-run the ESP-IDF installer or install script (install.bat / install.ps1).",
        "recoverable": True,
    },
    "source_compile_error": {
        "keywords": ["error:", "compilation terminated"],
        "suggestion": "Source code compilation error. Check the build log for the specific file and line number that failed.",
        "recoverable": False,
    },
}


def classify_build_error(output_text: str, target: str = "") -> dict:
    """
    Analyze build output and return a structured error classification.
    Keywords format:
      - [kw1, kw2]        → ALL must match (AND)
      - [[a, b], [c]]     → (a AND b) OR (c)
    """
    for err_type, info in BUILD_ERROR_TYPES.items():
        kws = info["keywords"]
        if kws and isinstance(kws[0], list):
            # OR-of-ANDs: any group where all keywords match
            if any(all(kw in output_text for kw in group) for group in kws):
                return {
                    "error_type": err_type,
                    "suggestion": info["suggestion"],
                    "recoverable": info["recoverable"],
                }
        else:
            # legacy AND: all keywords must match
            if all(kw in output_text for kw in kws):
                return {
                    "error_type": err_type,
                    "suggestion": info["suggestion"],
                    "recoverable": info["recoverable"],
                }
    return {
        "error_type": "build_failed",
        "suggestion": "Build failed. Check the log output above for error messages.",
        "recoverable": False,
    }


def _extract_version(path: Path) -> str:
    """Extract a version-ish string from a path name for matching."""
    for part in path.parts:
        name = part.lower()
        if name.startswith("v") and name[1:2].isdigit():
            return name.lstrip("v")   # "v5.5.3" -> "5.5.3"
        if name.startswith("tools-"):
            return name.replace("tools-", "")  # "tools-5.5.2" -> "5.5.2"
    return ""


def check_build_config(project_dir: str, idf_path: str, idf_tools_path: str) -> dict:
    """
    Pre-flight check: validate paths and detect issues before building.
    Resolution order: user-provided → system env → auto-detect.
    Returns resolved paths + validation results.
    """
    checks = []
    has_error = False
    error_type = None
    suggestion = None

    # ── 1. project_dir ──────────────────────────────────────────────
    proj = Path(project_dir) if project_dir else None
    cmake = proj / "CMakeLists.txt" if proj else None
    if not proj or not proj.exists() or not proj.is_dir():
        checks.append({"check": "project_dir", "status": "fail", "message": f"Project directory not found: {project_dir}"})
        has_error = True; error_type = "config_invalid"; suggestion = "Check the Project Dir path in the web UI."
    elif not cmake.exists():
        checks.append({"check": "project_dir", "status": "fail", "message": f"No CMakeLists.txt found in {project_dir}"})
        has_error = True; error_type = "config_invalid"; suggestion = "The project directory does not appear to be an ESP-IDF project."
    else:
        checks.append({"check": "project_dir", "status": "ok", "message": f"Project at {proj.resolve()}"})

    proj_target = detect_target(proj) if proj and cmake and cmake.exists() else "unknown"

    # ── 2. IDF_PATH: user → env → auto-detect ─────────────────────
    resolved_idf = None
    idf_version = None
    idf_source = "none"

    if idf_path:
        p = Path(idf_path)
        if p.exists() and (p / "tools" / "idf.py").exists():
            resolved_idf, idf_version = p.resolve(), p.parent.name
            idf_source = "user"
            checks.append({"check": "idf_path", "status": "ok", "message": f"IDF at {resolved_idf} ({idf_version}) [user]"})
        else:
            checks.append({"check": "idf_path", "status": "fail", "message": f"User IDF path invalid: {idf_path}"})

    if not resolved_idf:
        env_idf = os.environ.get("IDF_PATH", "")
        if env_idf:
            ep = Path(env_idf)
            if ep.exists() and (ep / "tools" / "idf.py").exists():
                resolved_idf, idf_version = ep.resolve(), ep.parent.name
                idf_source = "env"
                checks.append({"check": "idf_path", "status": "ok", "message": f"IDF from env IDF_PATH={resolved_idf} ({idf_version})"})

    if not resolved_idf:
        found, ver = find_idf()
        if found:
            resolved_idf, idf_version = found, ver
            idf_source = "auto"
            checks.append({"check": "idf_path", "status": "ok", "message": f"IDF auto-detected at {resolved_idf} ({idf_version})"})

    if not resolved_idf:
        checks.append({"check": "idf_path", "status": "fail", "message": "No IDF_PATH found (user / env / auto-detect all failed)"})
        has_error = True; error_type = "config_invalid"; suggestion = "Install ESP-IDF or provide the path in the web UI."

    # ── 3. IDF_TOOLS_PATH: user → env → match IDF version → auto ──
    resolved_tools = None
    tools_version = None
    tools_source = "none"

    if idf_tools_path:
        tp = Path(idf_tools_path)
        if tp.exists() and (tp / "tools").is_dir():
            resolved_tools, tools_version = tp.resolve(), tp.name
            tools_source = "user"
            checks.append({"check": "tools_path", "status": "ok", "message": f"Tools at {resolved_tools.name} [user]"})
        else:
            checks.append({"check": "tools_path", "status": "fail", "message": f"User tools path invalid: {idf_tools_path}"})

    if not resolved_tools:
        env_tools = os.environ.get("IDF_TOOLS_PATH", "")
        if env_tools:
            etp = Path(env_tools)
            if etp.exists() and (etp / "tools").is_dir():
                resolved_tools, tools_version = etp.resolve(), etp.name
                tools_source = "env"
                checks.append({"check": "tools_path", "status": "ok", "message": f"Tools from env IDF_TOOLS_PATH={resolved_tools.name}"})

    # ── 4. Cross-validate: IDF version vs tools version ───────────
    if resolved_idf and resolved_tools:
        idf_ver = _extract_version(resolved_idf)
        tools_ver = _extract_version(resolved_tools)
        if idf_ver and tools_ver and idf_ver != tools_ver:
            checks.append({"check": "version_match", "status": "warn",
                "message": f"IDF version '{idf_ver}' != tools version '{tools_ver}' — may cause incompatibility."})
            if not error_type:
                error_type = "toolchain_version_mismatch"
                suggestion = f"IDF ({idf_ver}) and tools ({tools_ver}) versions don't match. Point both to same version."

    # ── 5. Compiler check ─────────────────────────────────────────
    target_to_prefix = {
        "esp32": "xtensa-esp-elf", "esp32s2": "xtensa-esp-elf", "esp32s3": "xtensa-esp-elf",
        "esp32c2": "riscv32-esp-elf", "esp32c3": "riscv32-esp-elf", "esp32c5": "riscv32-esp-elf",
        "esp32c6": "riscv32-esp-elf", "esp32h2": "riscv32-esp-elf", "esp32p4": "riscv32-esp-elf",
    }
    prefix = target_to_prefix.get(proj_target, "riscv32-esp-elf")

    if resolved_tools:
        tools_dir = resolved_tools / "tools"
        found_compiler = False
        for tc_dir in tools_dir.iterdir():
            if tc_dir.is_dir():
                for bin_dir in tc_dir.rglob("bin"):
                    if bin_dir.is_dir():
                        gcc = bin_dir / f"{prefix}-gcc.exe"
                        if gcc.exists():
                            found_compiler = True
                            checks.append({"check": "compiler", "status": "ok",
                                "message": f"Found {gcc.name} ({gcc.parent.parent.name})"})
                            break
                if found_compiler:
                    break
        if not found_compiler:
            any_gcc = list(tools_dir.rglob("*gcc.exe"))
            if any_gcc:
                checks.append({"check": "compiler", "status": "warn",
                    "message": f"No '{prefix}-gcc' for target '{proj_target}' (other GCC exists — version mismatch?)"})
                if not error_type:
                    error_type = "toolchain_version_mismatch"
                    suggestion = f"Tools version doesn't support target '{proj_target}'. Update to matching tools."
            else:
                checks.append({"check": "compiler", "status": "fail", "message": "No GCC compiler found in IDF_TOOLS_PATH."})
                has_error = True
                if not error_type:
                    error_type = "compiler_not_found"
                    suggestion = "IDF_TOOLS_PATH has no toolchain binaries. Reinstall ESP-IDF tools."

    return {
        "valid": not has_error,
        "checks": checks,
        "error_type": error_type,
        "suggestion": suggestion,
        "target": proj_target,
        "resolved": {
            "idf_path": str(resolved_idf) if resolved_idf else None,
            "idf_version": idf_version,
            "idf_source": idf_source,
            "idf_tools_path": str(resolved_tools) if resolved_tools else None,
            "tools_version": tools_version,
            "tools_source": tools_source,
        }
    }


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
    DTR -> EN (inverted transistor): DTR=True -> EN low -> reset
    RTS -> GPIO0 (inverted transistor): RTS=True -> GPIO0 low -> download
    """
    with SERIAL_LOCK:
        if SERIAL_PORT and SERIAL_PORT.is_open:
            try:
                SERIAL_PORT.dtr = False
                SERIAL_PORT.rts = False
                time.sleep(0.05)
                SERIAL_PORT.dtr = True
                time.sleep(0.15)
                SERIAL_PORT.dtr = False
                time.sleep(0.05)
                return True
            except Exception:
                pass
    return False


# ── subprocess runner (build / flash) ────────────────────────────
def run_idf_command(cmd: list[str], label: str, project_dir: str,
                    idf_tools_path: str | None = None) -> dict:
    """
    Run an idf.py subprocess with proper IDF environment (MSYSTEM fix,
    Python venv, export.bat).  Streams output via SSE in real-time.
    Returns agent-friendly dict with error classification.
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
        batch_lines.append(f"set PATH={IDF_VENV};%PATH%")
        idf_python_env = str(Path(IDF_VENV).parent)
        batch_lines.append(f"set IDF_PYTHON_ENV_PATH={idf_python_env}")
    if idf_tools_path:
        batch_lines.append(f"set IDF_TOOLS_PATH={idf_tools_path}")
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

        # classify errors for agent-friendly response
        full_output = "\n".join(lines)
        error_info = classify_build_error(full_output, target) if not ok else {}
        if error_info.get("error_type"):
            diag = f"[{error_info['error_type']}] {error_info['suggestion']}"
            sse_broadcast("system", {"status": f"[diagnostic] {diag}"})
            lines.append(f"[diagnostic] {diag}")

        status = "success" if ok else f"failed (exit {proc.returncode})"
        sse_broadcast("system", {"status": f"{label}: {status}"})
        return {"status": "ok" if ok else "fail", "exit_code": proc.returncode,
                "output": lines, "error_type": error_info.get("error_type"),
                "suggestion": error_info.get("suggestion"),
                "recoverable": error_info.get("recoverable")}
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
        global IDF_PATH, IDF_VENV
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

        if path == "/api/check-config":
            project = body.get("project_dir", ".")
            idf_path = body.get("idf_path", "")
            idf_tools_path = body.get("idf_tools_path", "")
            result = check_build_config(project, idf_path, idf_tools_path)
            return self._json(result)

        if path == "/api/shutdown":
            serial_close()
            threading.Thread(target=lambda: HTTP_SERVER.shutdown(), daemon=True).start()
            return self._json({"status": "shutting down"})

        if path == "/api/build":
            project = body.get("project_dir", ".")
            idf_path = body.get("idf_path", "")
            idf_tools_path = body.get("idf_tools_path", "")
            if idf_path:
                p = Path(idf_path)
                if (p / "tools" / "idf.py").exists():
                    IDF_PATH = str(p.resolve())
                    IDF_VENV = None
            result = run_idf_command(["idf.py", "build"], "build", project,
                                     idf_tools_path=idf_tools_path or None)
            return self._json(result)

        if path == "/api/flash":
            project = body.get("project_dir", ".")
            port = body.get("port", "")
            idf_path = body.get("idf_path", "")
            idf_tools_path = body.get("idf_tools_path", "")
            if idf_path:
                p = Path(idf_path)
                if (p / "tools" / "idf.py").exists():
                    IDF_PATH = str(p.resolve())
                    IDF_VENV = None
            serial_close()
            time.sleep(0.3)
            cmd = ["idf.py", "-p", port, "flash"] if port else ["idf.py", "flash"]
            result = run_idf_command(cmd, "flash", project,
                                     idf_tools_path=idf_tools_path or None)
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
    global HTTP_SERVER
    HTTP_SERVER = server
    print(f"\n  ESP-Agent Serial Server running at:")
    print(f"  http://{args.host}:{args.port}/")
    print(f"\n  Web UI  ->  http://{args.host}:{args.port}/")
    print(f"  API     ->  http://{args.host}:{args.port}/api/ports")
    print(f"\n  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        serial_close()
        server.shutdown()


if __name__ == "__main__":
    main()
