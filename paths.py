"""
Subs Rotator - shared path constants
Imported by both rotator.py and rotator_manager.py
"""

import glob
import os
import shutil
from pathlib import Path

# --- Code location ---------------------------------------------------------

APP_DIR = Path(__file__).parent

# --- Data location (home dir - never synced, never committed) --------------

DATA_DIR = Path.home() / ".subs-rotator"
SESSIONS_DIR = DATA_DIR / "sessions"
CONFIG_FILE = DATA_DIR / "rotator_config.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
LAUNCHERS_DIR = DATA_DIR

# --- Native tool session files (written temporarily when switching accounts) -

CODEX_AUTH = Path.home() / ".codex" / "auth.json"
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"

# --- Tool executables (auto-detected via PATH + common Windows installs) ----


def _expand_path(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def _first_existing_path(*candidates: str) -> str:
    for candidate in candidates:
        if not candidate:
            continue
        candidate = _expand_path(candidate)
        if Path(candidate).exists():
            return candidate
    return ""


def _first_glob_path(*patterns: str) -> str:
    for pattern in patterns:
        for match in glob.glob(_expand_path(pattern)):
            if Path(match).exists():
                return match
    return ""


def _find_cmd(name: str, *fallbacks: str) -> str:
    found = shutil.which(name)
    if found:
        return found

    direct = _first_existing_path(*fallbacks)
    if direct:
        return direct

    if name == "codex":
        return _first_glob_path(
            r"C:\Program Files\node-v*-win-x64\codex.cmd",
            r"C:\Program Files\node-v*-win-x64\codex.exe",
            r"C:\Program Files\nodejs\codex.cmd",
            r"C:\Program Files\nodejs\codex.exe",
            r"C:\Users\*\AppData\Roaming\npm\codex.cmd",
            r"C:\Users\*\AppData\Local\npm\codex.cmd",
            r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\resources\codex.exe",
            r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\resources\codex",
        ) or name

    return name  # last resort: hope it's on PATH at runtime


CODEX_CMD = _find_cmd(
    "codex",
    r"%ProgramFiles%\node-v24.14.0-win-x64\codex.cmd",
    r"C:\Program Files\nodejs\codex.cmd",
    r"C:\Program Files\node\codex.cmd",
)

CLAUDE_CMD = _find_cmd(
    "claude",
    str(Path.home() / ".paperclip" / "instances" / "paperclip" / "claude.cmd"),
    r"C:\Program Files\nodejs\claude.cmd",
)

GEMMA_CMD = _find_cmd(
    "ollama",
    r"C:\Program Files\Ollama\ollama.exe",
    str(Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"),
)
