---
name: esp-agent
description: Automatically build ESP-IDF projects on Windows. Detects your IDF installation, sets up the environment (handles MSYSTEM/Python venv mismatches), and runs idf.py build — no manual config needed.
license: MIT
metadata:
  author: https://github.com/Martlet-Tech
  version: "0.1.0"
  domain: specialized
  triggers: esp-idf, esp32, build, compile, firmware
  role: assistant
  scope: implementation
---

# ESP-IDF Build Agent

Automates the ESP-IDF build process on Windows. Handles all the environment quirks: MSYSTEM conflicts, Python venv version mismatches, toolchain PATH setup.

## Files

| File | Purpose |
|------|---------|
| `esp_agent.py` | Python CLI script — does the actual detection and build |
| `SKILL.md` | Skill definition for Claude |

## Workflow

### Step 1: Ask for IDF path (if not already known)

Ask the user: **"Where is ESP-IDF installed?"**

If the user doesn't know, scan common locations:
- `D:\Programs\esp-idf\`
- `C:\esp\`
- `C:\esp-idf\`
- `%USERPROFILE%\esp\`

Look for subdirectories matching `v*.*.*` pattern (e.g. `v5.5.3`, `v5.4.1`).

### Step 2: Detect IDF environment

For each detected IDF version, check:
- `{idf_path}\tools\idf.py` exists (validates installation)
- Available Python venvs in `~/.espressif/python_env/idf{version}_py3.*_env\`
- Target chip in project's `sdkconfig` (`CONFIG_IDF_TARGET`)

Select the best matching Python venv (match IDF version, prefer any Python 3.x).

### Step 3: Build the project

Run the Python helper script:

```bash
python esp_agent.py <project_dir> [--idf-path <idf_path>]
```

The script handles:
1. **MSYSTEM** — clears the env var so `export.bat` doesn't bail early
2. **Python venv** — finds existing venv and prepends to PATH before `export.bat`
3. **Batch execution** — writes a temp `.bat` and invokes it via `cmd.exe '//c'`
4. **Build** — runs `idf.py build` with full output streaming

### Step 4: Report result

Print:
- Build **success** or **failure** with exit code
- Firmware path and size (if success)
- Flash command copied from build output

## Environment Compatibility

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| `export.bat` exits silently | `MSYSTEM=MINGW64` from Git Bash | `set MSYSTEM=` before calling |
| `activate.py` can't find venv | System Python version doesn't match venv | Prepend existing venv to PATH first |
| `cmd.exe /c` not working | Git Bash interprets `/c` as path | Use `//c` instead |
| `idf.py: command not found` | Not in PATH after export failure | Set up venv PATH manually |

## Constraints

### MUST DO
- Ask the user for IDF path first (don't assume)
- Use `cmd.exe '//c'` syntax when calling from Git Bash
- Handle the MSYSTEM env var issue
- Report build success/failure clearly
- Preserve the build log for debugging

### MUST NOT DO
- Modify any project source files
- Touch git state
- Assume common paths without asking
- Hardcode Python version numbers
