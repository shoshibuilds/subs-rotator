"""
Subs Rotator - encryption helpers
Uses Windows DPAPI: data encrypted per Windows user account.
A file copied to another machine cannot be decrypted.
"""

import json
from pathlib import Path

try:
    import win32crypt
    _DPAPI_AVAILABLE = True
except ImportError:
    _DPAPI_AVAILABLE = False


def encrypt_bytes(data: bytes) -> bytes:
    """Encrypt bytes with DPAPI. Falls back to plaintext if unavailable."""
    if _DPAPI_AVAILABLE:
        return win32crypt.CryptProtectData(data, "subs-rotator", None, None, None, 0)
    return data


def decrypt_bytes(data: bytes) -> bytes:
    """Decrypt DPAPI-encrypted bytes. Falls back to plaintext if unavailable."""
    if _DPAPI_AVAILABLE:
        _, decrypted = win32crypt.CryptUnprotectData(data, None, None, None, 0)
        return decrypted
    return data


def save_encrypted_json(path: Path, obj: dict):
    """Serialize obj to JSON and save encrypted."""
    raw = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypt_bytes(raw))


def load_encrypted_json(path: Path) -> dict:
    """Load and decrypt a JSON file saved by save_encrypted_json."""
    if not path.exists():
        return {}
    try:
        raw = decrypt_bytes(path.read_bytes())
        return json.loads(raw.decode("utf-8"))
    except Exception:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}


def save_encrypted_session(path: Path, session: dict):
    """Save a session dict (auth.json / credentials.json) encrypted."""
    save_encrypted_json(path, session)


def load_encrypted_session(path: Path) -> dict:
    """Load and decrypt a session file."""
    return load_encrypted_json(path)


def decrypt_session_to_file(encrypted_path: Path, target_path: Path):
    """Decrypt session and write plaintext to target (for Codex/Claude to read)."""
    session = load_encrypted_session(encrypted_path)
    if session:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(session, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )


def is_dpapi_available() -> bool:
    return _DPAPI_AVAILABLE

