"""
Paperclip Router — shared path constants
Imported by both router.py and router_manager.py
"""

import shutil
from pathlib import Path

# ─── Code location ────────────────────────────────────────────────────────────

ROUTER_DIR = Path(__file__).parent

# ─── Data location (home dir — never synced, never committed) ─────────────────

DATA_DIR     = Path.home() / ".paperclip-router"
SESSIONS_DIR = DATA_DIR / "sessions"
CONFIG_FILE  = DATA_DIR / "router_config.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
LAUNCHERS_DIR = DATA_DIR

# ─── Native tool session files (written temporarily when switching accounts) ──

CODEX_AUTH   = Path.home() / ".codex" / "auth.json"
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"

# ─── Tool executables (auto-detected via PATH) ────────────────────────────────
# Fallbacks are common install locations — adjust if your setup differs.

def _find_cmd(name: str, *fallbacks: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for fb in fallbacks:
        if Path(fb).exists():
            return fb
    return name  # last resort: hope it's on PATH at runtime

CODEX_CMD = _find_cmd(
    "codex",
    r"C:\Program Files\nodejs\codex.cmd",
    r"C:\Program Files\node\codex.cmd",
)

CLAUDE_CMD = _find_cmd(
    "claude",
    str(Path.home() / ".paperclip" / "instances" / "paperclip" / "claude.cmd"),
    r"C:\Program Files\nodejs\claude.cmd",
)
