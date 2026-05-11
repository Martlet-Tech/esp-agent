---
name: esp-agent
description: HTTP+SSE server for ESP-IDF build/flash/serial-monitor on Windows. REST API returns structured error types so AI agents can act on failures instead of guessing.
license: MIT
metadata:
  author: https://github.com/Martlet-Tech
  version: "0.2.0"
  domain: specialized
  triggers: esp-idf, esp32, build, compile, firmware, serial, monitor
  role: assistant
  scope: implementation
---

# ESP-Agent Serial Server

HTTP + SSE server that bridges serial port and web browser for ESP-IDF development. The REST API is designed to be **agent-friendly**: all build/flash failures return structured `error_type`, `suggestion`, and `recoverable` fields so you can act decisively instead of retrying blindly.

## Quick Start

```bash
# Start the server
python esp-agent/serial_server.py
# Open http://localhost:8099 in browser
```

## API Reference (agent-friendly)

All POST endpoints accept JSON body with these fields:
- `project_dir` — path to ESP-IDF project (e.g. `D:/project/test26`)
- `idf_path` — ESP-IDF installation path
- `idf_tools_path` — ESP-IDF tools path

### Pre-flight: `POST /api/check-config`

**Always call this before build.** Validates all three paths, cascading through user → env var → auto-detect, and cross-checks version compatibility.

Response includes `resolved` block showing what was actually used:

```json
{
  "valid": true,
  "checks": [
    {"check": "project_dir", "status": "ok", "message": "..."),
    {"check": "idf_path", "status": "ok", "message": "IDF at ... (v5.5.3) [user]"},
    {"check": "version_match", "status": "warn", "message": "IDF '5.5.1' != tools '5.5.2'"}
  ],
  "error_type": "toolchain_version_mismatch",
  "suggestion": "IDF (5.5.1) and tools (5.5.2) don't match. Point both to same version.",
  "resolved": {
    "idf_path": "D:/.../v5.5.3/esp-idf",
    "idf_version": "v5.5.3",
    "idf_source": "user",
    "idf_tools_path": "D:/.../tools-5.5.2",
    "tools_source": "env"
  }
}
```

**Decision logic based on `valid`:**
- `true` → proceed to build
- `false` → show `suggestion` to user, don't build

### Build: `POST /api/build`

```json
// Request
{"project_dir": "...", "idf_path": "...", "idf_tools_path": "..."}

// Response on failure
{
  "status": "fail",
  "exit_code": 2,
  "output": [...],
  "error_type": "toolchain_version_mismatch",
  "suggestion": "Update IDF_TOOLS_PATH to a version matching...",
  "recoverable": false
}
```

### Flash: `POST /api/flash`

Same as build, with optional `"port"` field.

### Shutdown: `POST /api/shutdown`

Gracefully stops the server (closes serial, shuts down socket).

## Error Classification Reference

When build/flash fails, you get:

| `error_type` | Meaning | `recoverable` |
|---|---|---|
| `toolchain_version_mismatch` | GCC doesn't support target CPU extensions | false |
| `tool_version_mismatch` | CMake tool version check failed (stale build or wrong tools) | true |
| `compiler_not_found` | No GCC in IDF_TOOLS_PATH | true |
| `cmake_failed` | CMake configuration failed | true |
| `python_dependency` | Python venv missing or corrupted | true |
| `source_compile_error` | Source code compilation error | false |
| `build_failed` | Generic failure (last resort) | false |

**Recoverable = true** means you can fix the issue and retry. **Recoverable = false** means the user needs to intervene (wrong tools version, code bug).

## Agent Workflow

### Do this:

1. **Check config first**: `POST /api/check-config` → if `valid: false`, report `suggestion` to user and stop
2. **Build**: `POST /api/build` → check `error_type` in response
3. **If recoverable**: show `suggestion`, ask user if they want to fix and retry
4. **If not recoverable**: show `suggestion`, tell user what to fix
5. **Never retry blindly**: if `recoverable: false`, retrying will produce the same error
6. **Shutdown**: `POST /api/shutdown` when done

### Don't do this:

- Don't retry builds without checking `error_type` and `recoverable`
- Don't try to parse raw build `output` lines — use the structured fields
- Don't kill the server process — use `/api/shutdown`
- Don't guess IDF paths — let the server auto-detect or ask the user

## Resolution Order (IDF paths)

The server resolves each path in this cascade:
1. **User-provided** (in the JSON body)
2. **Environment variable** (`IDF_PATH`, `IDF_TOOLS_PATH`)
3. **Auto-detect** (common install locations)

The `resolved` block in `/api/check-config` tells you which source was used and the version detected.
