# ESP-Agent 🤖

**Automatically build any ESP-IDF project on Windows — zero manual environment setup.**

[![Platform](https://img.shields.io/badge/platform-Windows-blue)]()
[![ESP-IDF](https://img.shields.io/badge/ESP--IDF-5.x+-orange)](https://github.com/espressif/esp-idf)
[![License](https://img.shields.io/badge/license-MIT-green)]()

## The Problem

Building an ESP-IDF project on Windows is surprisingly fragile:

```bash
# ❌ export.bat silently exits — MSYSTEM conflict
# ❌ activate.py can't find Python venv — version mismatch
# ❌ cmd.exe /c broken in Git Bash — interpreted as drive path
# ❌ idf.py not found — environment never set up correctly
```

Every new clone, every environment reset, every teammate hits these.

## What ESP-Agent Does

A single command that handles all the Windows quirks:

```bash
python esp_agent.py path/to/esp-project
```

The script automatically:
1. **Scans** for ESP-IDF installations (`v5.5.3`, `v5.4.1`, etc.)
2. **Detects** the matching Python virtual environment (handles 3.10 vs 3.14 mismatches)
3. **Clears** the `MSYSTEM` env var so `export.bat` doesn't bail early on Git Bash
4. **Runs** `idf.py build` with full output capture
5. **Reports** firmware size and flash instructions

## Quick Start

```bash
# Clone or cd to your ESP-IDF project
cd your-esp-project

# Run the agent
python path/to/esp-agent/esp_agent.py .
```

If you have multiple IDF versions, specify the one to use:

```bash
python esp_agent.py . --idf-path D:\Programs\esp-idf\v5.5.3\esp-idf
```

## Installation

### As a Claude Code skill (recommended)

```bash
npx skills add Martlet-Tech/esp-agent -g -y
```

Then in any ESP-IDF project:

```
/esp-agent
```

Claude will ask for your IDF path and run the build.

### Standalone

The Python script has **zero dependencies** — it only uses the standard library. Just clone and run:

```bash
git clone https://github.com/Martlet-Tech/esp-agent.git
python esp-agent/esp_agent.py your-esp-project
```

## Web Monitor — Watch Serial Output in Browser

Start the serial monitor server for real-time build, flash, and device logs:

```bash
# Install dependency (once)
pip install pyserial

# Start the server (uses system Python, not IDF venv)
python serial_server.py
```

Open **http://127.0.0.1:8099/** in your browser:

- Select COM port and click **Connect** — see device serial output live
- Click **Build** — compiles and streams log to browser
- Click **Flash** — flashes firmware (auto-disconnects serial, reconnects after)
- Claude (or any agent) talks to the same server via REST API:
  ```
  POST /api/build   → start build
  POST /api/flash   → start flash
  POST /api/monitor/start  → open serial port
  POST /api/send    → send data to serial
  GET  /api/ports   → list available ports
  GET  /api/events  → SSE stream (used by web UI)
  ```

Everything the agent does is visible in the browser — no more blind automation.

## How It Works

```
┌──────────────────────────────────────────────┐
│  esp_agent.py                                │
│                                              │
│  1. Detect IDF installations                 │
│     → D:\Programs\esp-idf\v5.5.3\esp-idf    │
│                                              │
│  2. Find matching Python venv                │
│     → idf5.5_py3.10_env                      │
│                                              │
│  3. Generate temp batch with fixes:          │
│     • set MSYSTEM=          ← Git Bash fix   │
│     • prepend venv to PATH  ← Python fix     │
│     • call export.bat                        │
│     • idf.py build                           │
│                                              │
│  4. Execute via cmd.exe (not Git Bash)       │
│  5. Report result                            │
└──────────────────────────────────────────────┘
```

## Roadmap

- [x] Web monitor UI (serial + build + flash in browser)
- [x] Flash helper — one-click from browser
- [ ] Linux / macOS support

## Contributing

PRs welcome! Keep it simple — the whole point is avoiding complexity.

## Related

- [ESP-IDF Getting Started](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/)
- [Claude Code Skills](https://skills.sh/)
