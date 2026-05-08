#!/usr/bin/env python3
"""
ESP-IDF Build Agent — Windows helper script.

Usage:
    python esp_agent.py <project_dir> [--idf-path <idf_path>]

Detects the ESP-IDF environment, sets up the Python venv, handles
MSYSTEM conflicts, and runs idf.py build — all in one shot.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# ── common IDF install locations ──────────────────────────────────
COMMON_IDF_ROOTS = [
    Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "esp-idf",
    Path("D:\\Programs\\esp-idf"),
    Path("C:\\esp"),
    Path("C:\\esp-idf"),
    Path.home() / "esp",
]


def find_idf_installations() -> list[tuple[Path, str]]:
    """Return list of (idf_path, version_str) for all detected IDF versions."""
    found = []
    for root in COMMON_IDF_ROOTS:
        if not root.exists():
            continue
        # Versioned subdirectories: v5.5.3, v5.4.1, etc.
        for subdir in sorted(root.iterdir(), reverse=True):
            if not subdir.name.startswith("v") or not subdir.is_dir():
                continue
            idf_dir = subdir / "esp-idf"
            tools_py = idf_dir / "tools" / "idf.py"
            if tools_py.exists():
                found.append((idf_dir.resolve(), subdir.name))
    return found


def get_python_venv(idf_version: str) -> Path | None:
    """
    Find an existing ESP-IDF Python venv for the given IDF version.
    Returns the Scripts directory path (so you can prepend to PATH), or None.
    """
    espressif_dir = Path.home() / ".espressif" / "python_env"
    if not espressif_dir.exists():
        return None
    # venv dirs use major.minor only (e.g. "idf5.5_py3.10_env"), so strip patch
    parts = idf_version.replace('v', '').split('.')
    major_minor = '.'.join(parts[:2])
    prefix = f"idf{major_minor}_py3"
    for venv_dir in espressif_dir.iterdir():
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


def build_batch(
    project_dir: Path,
    idf_path: Path,
    venv_path: Path | None,
) -> str:
    """Generate the build batch script as a string."""
    lines = ["@echo on", "set MSYSTEM="]

    # Prepend the matching Python venv to avoid version mismatch
    if venv_path is not None:
        lines.append(f'set PATH={venv_path};%PATH%')

    lines.append(f'cd /d {project_dir}')
    lines.append(f'set IDF_PATH={idf_path}')
    lines.append(f'call {idf_path}\\export.bat')
    lines.append("echo ======================================")
    lines.append(f'echo Building {project_dir.name} ({detect_target(project_dir)})')
    lines.append("echo ======================================")
    lines.append("idf.py build 2>&1")
    lines.append("exit /b %ERRORLEVEL%")

    return "\r\n".join(lines) + "\r\n"


def run_build(batch_content: str) -> subprocess.CompletedProcess:
    """Write a temp batch file and execute it via cmd.exe."""
    tmp = Path(tempfile.gettempdir()) / "esp-agent-build.bat"
    log = Path(tempfile.gettempdir()) / "esp-agent-build.log"
    tmp.write_text(batch_content, encoding="utf-8")
    # Use '//c' to prevent Git Bash from swallowing '/c'
    result = subprocess.run(
        ["cmd.exe", "/c", str(tmp)],
        capture_output=True,
    )
    log.write_bytes(result.stdout + result.stderr)
    # Print summary (last 20 lines)
    raw = log.read_bytes()
    # Try utf-8 first, fall back to system encoding (gbk on Chinese Windows)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")
    lines = text.splitlines()
    print(f"[esp-agent] Batch: {tmp}")
    print(f"[esp-agent] Log: {log}  ({len(lines)} lines)")
    print()
    for line in lines[-20:]:
        print(line)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="ESP-IDF Build Agent — build an ESP-IDF project on Windows."
    )
    parser.add_argument("project_dir", type=str, help="Path to the ESP-IDF project")
    parser.add_argument("--idf-path", type=str, default=None, help="ESP-IDF installation path (e.g. D:\\Programs\\esp-idf\\v5.5.3\\esp-idf)")

    args = parser.parse_args()
    project_dir = Path(args.project_dir).resolve()

    if not (project_dir / "sdkconfig").exists():
        print(f"[esp-agent] ERROR: no sdkconfig found in {project_dir}", file=sys.stderr)
        sys.exit(1)

    # ── determine IDF path ────────────────────────────────────────
    idf_path: Path | None = None
    if args.idf_path:
        candidate = Path(args.idf_path).resolve()
        if (candidate / "tools" / "idf.py").exists():
            idf_path = candidate
        else:
            print(f"[esp-agent] WARNING: --idf-path {args.idf_path} invalid, scanning...")

    if idf_path is None:
        installations = find_idf_installations()
        if not installations:
            print("[esp-agent] ERROR: no ESP-IDF installation found.", file=sys.stderr)
            print("  Scanned:", ", ".join(str(p) for p in COMMON_IDF_ROOTS), file=sys.stderr)
            sys.exit(1)
        idf_path, ver = installations[0]
        print(f"[esp-agent] Found {ver} at {idf_path}")

    # ── determine Python venv ─────────────────────────────────────
    # Extract version (e.g. "v5.5.3" from the directory name or from the path)
    # Try to get the version from the parent directory name
    parent_version = idf_path.parent.name if idf_path.parent else ""
    venv_path = get_python_venv(parent_version) if parent_version else None

    if venv_path is None:
        # Broader fallback: try any idf5.*_py3.*_env
        espressif_dir = Path.home() / ".espressif" / "python_env"
        if espressif_dir.exists():
            candidates = sorted(espressif_dir.iterdir(), reverse=True)
            for c in candidates:
                scripts = c / "Scripts"
                if (scripts / "python.exe").exists():
                    venv_path = scripts.resolve()
                    print(f"[esp-agent] Using fallback venv: {venv_path}")
                    break

    if venv_path is None:
        print("[esp-agent] WARNING: no Python venv found; export.bat may fail")

    # ── build ─────────────────────────────────────────────────────
    target = detect_target(project_dir)
    print(f"[esp-agent] Project: {project_dir.name}  Target: {target}")
    print(f"[esp-agent] IDF: {idf_path}")
    print(f"[esp-agent] Venv: {venv_path or 'none (letting export.bat decide)'}")
    print()

    batch = build_batch(project_dir, idf_path, venv_path)
    result = run_build(batch)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
