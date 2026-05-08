# ESP-Agent 🤖

**One service that builds, flashes, and monitors your ESP-IDF project — from your browser or from Claude.**

[![Platform](https://img.shields.io/badge/platform-Windows-blue)]()
[![ESP-IDF](https://img.shields.io/badge/ESP--IDF-5.x+-orange)](https://github.com/espressif/esp-idf)
[![License](https://img.shields.io/badge/license-MIT-green)]()

## The Problem

Building an ESP-IDF project on Windows is surprisingly fragile:

```bash
# ❌ export.bat silently exits — MSYSTEM conflict with Git Bash
# ❌ activate.py can't find Python venv — version mismatch
# ❌ cmd.exe /c broken in Git Bash — interpreted as drive path
# ❌ idf.py not found — environment never set up correctly
# ❌ Serial port contention — flash needs the same COM as monitor
```

Every new clone, every environment reset, every teammate hits these.

## What ESP-Agent Does

A single HTTP+SSE server that handles **everything** — environment setup, build, flash, and serial monitor — all visible in your browser and controllable via REST API.

```
┌──────────────────────────────────────────────────┐
│              Your Browser (Web UI)                │
│         http://localhost:8099                     │
│     ┌──────────┬───────────┬──────────┐          │
│     │  Serial  │  Build    │  Flash   │          │
│     │  Monitor │  Log      │  Log     │          │
│     └────┬─────┴─────┬─────┴────┬────┘          │
│          │           │          │                 │
└──────────┼───────────┼──────────┼─────────────────┘
           │  SSE      │  SSE     │  SSE
           ▼           ▼          ▼
┌──────────────────────────────────────────────────┐
│              serial_server.py                     │
│                                                   │
│  ┌────────────┐  ┌────────────────────────┐      │
│  │ Serial     │  │ Build / Flash Engine   │      │
│  │ Monitor    │  │                        │      │
│  │ (pyserial) │  │ 1. set MSYSTEM=       │      │
│  │            │  │ 2. prepend venv PATH   │      │
│  │ ──→ SSE    │  │ 3. call export.bat    │      │
│  │    stream  │  │ 4. idf.py build/flash │      │
│  └────────────┘  └────────────────────────┘      │
│                         │                         │
│  ┌──────────────────────┘                         │
│  │  REST API: /api/build, /api/flash, ...         │
│  │  Claude (or any agent) calls these too         │
│  └────────────────────────────────────────────────┘
└──────────────────────────────────────────────────┘
                      │
                      ▼
              ESP32 Dev Board (COM port)
```

- **Web UI** — select COM port, see serial output live, build and flash with one click
- **REST API** — Claude calls the same endpoints; all output streams to both browser and API
- **Windows quirks handled automatically** — MSYSTEM, Python venv, export.bat, cmd.exe
- **No port contention** — server coordinates monitor and flash; flash auto-releases COM
- **Standalone** — works without Claude; configure IDF path and project dir from the UI

## Quick Start

```bash
# 1. Install dependency (once)
pip install pyserial

# 2. Start the server
python serial_server.py

# 3. Open http://localhost:8099 in your browser
```

Or double-click **`start_monitor.bat`**.

### From the Web UI

1. Set **IDF Path** (e.g. `D:\Programs\esp-idf\v5.5.3\esp-idf`) — or leave empty to auto-detect
2. Set **Project** (e.g. `D:\Projects\hello_world`)
3. Click **Build** → watch the compile log stream in real-time
4. Select **COM port** and click **Connect** → see device serial output
5. Click **Flash** → firmware uploads (monitor auto-disconnects, reconnects after)
6. Click **Reset** → hardware reset via DTR, watch the boot log

All of this works with zero AI involvement.

### From Claude (or any agent)

The server exposes a REST API that agents can call. Everything the agent does is visible in the browser:

```
POST /api/build          → build project (sync, returns result)
POST /api/flash          → flash firmware (sync, returns result)
POST /api/monitor/start  → open serial port
POST /api/monitor/stop   → close serial port
POST /api/send           → send data to serial
POST /api/reset          → hardware reset via DTR
GET  /api/ports          → list available COM ports
GET  /api/status         → query connection state
GET  /api/events         → SSE stream (build/flash/serial logs)
```

### CLI helper (legacy)

For quick one-off builds without the server:

```bash
python esp_agent.py path/to/esp-project
```

## Advanced

### Specifying IDF path

The server auto-detects IDF installations in common locations. To override:

```bash
python serial_server.py --idf-path D:\Programs\esp-idf\v5.5.3\esp-idf
```

Or set it from the Web UI config panel.

### Custom port / host

```bash
python serial_server.py --port 8080 --host 0.0.0.0
```

## Installation

### As a Claude Code skill

```bash
npx skills add Martlet-Tech/esp-agent -g -y
```

Then in any ESP-IDF project directory:

```
/esp-agent
```

Claude will ask for your IDF path and handle the rest.

### Standalone

```bash
git clone https://github.com/Martlet-Tech/esp-agent.git
cd esp-agent
pip install pyserial
python serial_server.py
```

## Roadmap

- [x] Serial monitor in browser (SSE live stream)
- [x] One-click build from browser
- [x] One-click flash with auto COM handover
- [x] Hardware reset (DTR)
- [x] Configurable IDF path and project dir
- [ ] Linux / macOS support

## Contributing

PRs welcome! Keep it simple — the whole point is avoiding complexity.

## Related

- [ESP-IDF Getting Started](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/)
- [Claude Code Skills](https://skills.sh/)
