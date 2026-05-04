"""
Subs Rotator
Multi-account AI switcher for Paperclip - open source
https://github.com/shoshibuilds/subs-rotator
"""

import json
import re
import shutil
import urllib.request
import urllib.error
import urllib.parse
import subprocess
import sys
import threading
import queue
import tkinter as tk
import webbrowser
import ctypes
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from pathlib import Path
import base64
import uuid
import os
import tempfile
from datetime import datetime, timezone
from typing import Any
import time
try:
    from PIL import Image, ImageTk
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
try:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.fernet import Fernet
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False
sys.path.insert(0, str(Path(__file__).parent))
from crypto import (
    save_encrypted_json, load_encrypted_json,
    save_encrypted_session, load_encrypted_session,
    is_dpapi_available,
)
from paths import (
    APP_DIR, SESSIONS_DIR, CONFIG_FILE, LAUNCHERS_DIR,
    CODEX_AUTH, CLAUDE_CREDS,
    CODEX_CMD, CLAUDE_CMD, GEMMA_CMD,
)

ROTATOR_PY = APP_DIR / "rotator.py"

BAT_FILES = {
    "all": LAUNCHERS_DIR / "rotator-all.bat",
    "codex": LAUNCHERS_DIR / "rotator-codex.bat",
    "claude": LAUNCHERS_DIR / "rotator-claude.bat",
    "gemma": LAUNCHERS_DIR / "rotator-gemma.bat",
    "gemini": LAUNCHERS_DIR / "rotator-gemini.bat",
}
BAT_MODE_LABELS = {
    "all": "All accounts",
    "codex": "Codex only",
    "claude": "Claude only",
    "gemma": "Gemma 4 only",
    "gemini": "Gemma 4 via Gemini shim",
}

TYPE_LABELS = {
    "codex":         "Codex Subscription",
    "claude":        "Claude",
    "openai_api":    "OpenAI API",
    "openrouter_api":"OpenRouter API",
    "anthropic_api": "Anthropic API",
    "gemma":         "Gemma 4",
}

GEMMA_MODEL_DEFAULT = "gemma4:e4b"
GEMMA_MODELS = [
    "gemma4:e2b",
    "gemma4:e4b",
    "gemma4:26b",
    "gemma4:31b",
]
_GEMMA_RUNTIME_STATUS_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
SUPPORTED_ADAPTER_LANES = [
    ("Codex (local)", "ready now", True),
    ("Claude (local)", "ready now", True),
    ("Gemma 4 (local)", "ready now", True),
    ("Gemini CLI (local)", "Gemma 4 custom shim", True),
]
OLLAMA_INSTALL_URL = "https://ollama.com/install.ps1"

# â”€â”€â”€ Links â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

GITHUB_URL = "https://github.com/shoshibuilds/subs-rotator"
BMC_URL    = "https://ko-fi.com/shoshibuilds"
VERSION    = "1.2.1"

DEFAULT_CONFIG = {"accounts": [], "companies": [], "settings": {}}


def hide_console_window():
    """Hide the parent console window on Windows when launched via python.exe."""
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass

# â”€â”€â”€ Config helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    result = load_encrypted_json(CONFIG_FILE)
    if not result:
        return dict(DEFAULT_CONFIG)
    result.setdefault("accounts", [])
    result.setdefault("companies", [])
    result.setdefault("settings", {})
    return result

def save_config(config: dict):
    save_encrypted_json(CONFIG_FILE, config)

def get_accounts() -> list:
    return load_config().get("accounts", [])

def save_accounts(accounts: list):
    cfg = load_config()
    cfg["accounts"] = accounts
    save_config(cfg)

def get_companies() -> list:
    return load_config().get("companies", [])

def save_companies(companies: list):
    cfg = load_config()
    cfg["companies"] = companies
    save_config(cfg)

def get_setting(key: str, default=None):
    return load_config().get("settings", {}).get(key, default)

def save_setting(key: str, value):
    cfg = load_config()
    cfg.setdefault("settings", {})[key] = value
    save_config(cfg)

def get_tool_cmd_path(tool: str) -> str:
    return get_setting(f"{tool}_cmd_path", "") or ""

def set_tool_cmd_path(tool: str, value: str):
    save_setting(f"{tool}_cmd_path", value.strip())

def get_tool_cmd(tool: str) -> str:
    custom = get_tool_cmd_path(tool).strip()
    if custom:
        return custom
    return {
        "codex": CODEX_CMD,
        "claude": CLAUDE_CMD,
        "gemma": GEMMA_CMD,
    }.get(tool, tool)

def _is_resolved_tool_path(value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    if Path(value).exists():
        return True
    return bool(shutil.which(value))

def _tool_path_status(tool: str) -> tuple[str, str]:
    override = get_tool_cmd_path(tool).strip()
    source = str(get_setting(f"{tool}_cmd_source", "") or "").strip().lower()
    resolved = get_tool_cmd(tool).strip()
    if override:
        if source == "auto":
            return override, "found automatically"
        return override, "user set"
    if resolved and _is_resolved_tool_path(resolved):
        return resolved, "found automatically"
    return (resolved or ""), "not found"

def _gemma_runtime_status(cache_ttl_seconds: int = 30) -> dict[str, Any]:
    cmd = get_tool_cmd("gemma").strip()
    model = get_setting("gemma_model", GEMMA_MODEL_DEFAULT).strip() or GEMMA_MODEL_DEFAULT
    cache_key = (cmd, model)
    now = time.monotonic()
    cached = _GEMMA_RUNTIME_STATUS_CACHE.get(cache_key)
    if cached and now - cached[0] < cache_ttl_seconds:
        return dict(cached[1])

    result: dict[str, Any] = {
        "command": cmd,
        "model": model,
        "binary_found": False,
        "connected": False,
        "model_installed": False,
        "status": "missing",
        "detail": "Ollama not found.",
        "color": "#ff6666",
    }

    if not cmd:
        _GEMMA_RUNTIME_STATUS_CACHE[cache_key] = (now, result)
        return dict(result)

    cmd_path = Path(cmd)
    if cmd_path.exists():
        resolved_cmd = str(cmd_path)
        result["binary_found"] = True
    else:
        resolved_cmd = shutil.which(cmd) or ""
        if resolved_cmd:
            result["binary_found"] = True
        else:
            _GEMMA_RUNTIME_STATUS_CACHE[cache_key] = (now, result)
            return dict(result)

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            [resolved_cmd, "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=creationflags,
        )
    except Exception as exc:
        result.update({
            "status": "offline",
            "detail": f"Ollama found, but list check failed: {exc}",
            "color": "#ff9900",
        })
        _GEMMA_RUNTIME_STATUS_CACHE[cache_key] = (now, result)
        return dict(result)

    if proc.returncode != 0:
        err = "\n".join(part for part in [proc.stdout or "", proc.stderr or ""] if part).strip()
        result.update({
            "status": "offline",
            "detail": (err.splitlines()[-1] if err else "Ollama responded with an error."),
            "color": "#ff9900",
        })
        _GEMMA_RUNTIME_STATUS_CACHE[cache_key] = (now, result)
        return dict(result)

    installed_models: set[str] = set()
    for raw_line in (proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("name "):
            continue
        name = line.split()[0].strip()
        if name:
            installed_models.add(name)

    model_installed = model in installed_models
    result.update({
        "connected": True,
        "model_installed": model_installed,
        "status": "ready" if model_installed else "needs pull",
        "detail": f"Ollama connected. Model {'installed' if model_installed else 'missing'}: {model}",
        "color": BRAND if model_installed else "#ff9900",
    })
    _GEMMA_RUNTIME_STATUS_CACHE[cache_key] = (now, result)
    return dict(result)

def slugify_company_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "company"

def _make_unique_company_slug(slug: str, taken: set[str], current_id: str | None = None, companies: list[dict] | None = None) -> str:
    if companies is None:
        companies = []
    candidate = slug
    idx = 2
    while any(c.get("slug") == candidate and c.get("id") != current_id for c in companies) or candidate in taken:
        candidate = f"{slug}-{idx}"
        idx += 1
    return candidate

def _normalize_companies(companies: list[dict]) -> tuple[list[dict], bool]:
    changed = False
    normalized = [dict(c) for c in companies]
    seen_slugs: set[str] = set()
    for i, company in enumerate(normalized):
        if company.get("order") != i:
            company["order"] = i
            changed = True
        if "enabled" not in company:
            company["enabled"] = True
            changed = True
        name = (company.get("name") or "").strip() or f"Company {i + 1}"
        if company.get("name") != name:
            company["name"] = name
            changed = True
        desired_slug = slugify_company_name(name)
        unique_slug = _make_unique_company_slug(desired_slug, seen_slugs, company.get("id"), normalized)
        if company.get("slug") != unique_slug:
            company["slug"] = unique_slug
            changed = True
        seen_slugs.add(unique_slug)
    return normalized, changed

def _normalize_account_company_ids(accounts: list[dict], companies: list[dict]) -> tuple[list[dict], bool]:
    valid_company_ids = {c.get("id", "") for c in companies}
    changed = False
    normalized = [dict(a) for a in accounts]
    for account in normalized:
        company_ids = account.get("company_ids")
        if company_ids is None:
            account["company_ids"] = []
            changed = True
            continue
        cleaned = [cid for cid in company_ids if cid in valid_company_ids]
        if cleaned != company_ids:
            account["company_ids"] = cleaned
            changed = True
    return normalized, changed

def get_company_launchers(company: dict) -> dict[str, Path]:
    slug = company.get("slug") or slugify_company_name(company.get("name", "company"))
    base_dir = LAUNCHERS_DIR / "companies" / slug
    return {
        "all": base_dir / f"rotator-{slug}-all.bat",
        "codex": base_dir / f"rotator-{slug}-codex.bat",
        "claude": base_dir / f"rotator-{slug}-claude.bat",
        "gemma": base_dir / f"rotator-{slug}-gemma.bat",
        "gemini": base_dir / f"rotator-{slug}-gemini.bat",
    }

def save_usage_cache(account_id: str, data: dict):
    cfg = load_config()
    cfg.setdefault("usage_cache", {})[account_id] = {
        **{k: v for k, v in data.items() if not k.startswith("_")},
        "fetched_at": datetime.now(timezone.utc).timestamp(),
    }
    save_config(cfg)

def get_usage_from_cache(account_id: str, max_age_min: int = 20) -> dict | None:
    cache = load_config().get("usage_cache", {}).get(account_id)
    if not cache:
        return None
    age = datetime.now(timezone.utc).timestamp() - cache.get("fetched_at", 0)
    if age > max_age_min * 60:
        return None
    return cache

def get_active_account_id() -> str | None:
    return load_config().get("active_account_id")

def get_recent_routing_state(linger_seconds: int = 12) -> tuple[str | None, bool]:
    cfg = load_config()
    active_id = cfg.get("active_account_id")
    if active_id:
        return active_id, True
    last_id = cfg.get("last_active_account_id")
    finished_at = cfg.get("last_active_account_finished_at", 0)
    if last_id and finished_at:
        age = datetime.now(timezone.utc).timestamp() - finished_at
        if age <= linger_seconds:
            return last_id, False
    return None, False

def _is_default_account_label(account: dict) -> bool:
    typ = account.get("type", "")
    label = account.get("label", "")
    if typ == "codex":
        return bool(
            re.fullmatch(r"Codex account \d+", label)
            or re.fullmatch(r"Codex Subscription \d+", label)
        )
    if typ == "claude":
        return bool(re.fullmatch(r"Claude account \d+", label))
    return False

def _renumber_default_account_labels(accounts: list[dict]) -> list[dict]:
    counts = {"codex": 0, "claude": 0}
    for account in accounts:
        typ = account.get("type", "")
        if typ not in counts:
            continue
        counts[typ] += 1
        if _is_default_account_label(account):
            if typ == "codex":
                account["label"] = f"Codex Subscription {counts[typ]}"
            else:
                account["label"] = f"Claude account {counts[typ]}"
    return accounts

def _normalize_accounts(accounts: list[dict]) -> tuple[list[dict], bool]:
    changed = False
    normalized = [dict(a) for a in accounts]
    for i, account in enumerate(normalized):
        if account.get("order") != i:
            account["order"] = i
            changed = True
    before = [(a.get("id"), a.get("label")) for a in normalized]
    normalized = _renumber_default_account_labels(normalized)
    after = [(a.get("id"), a.get("label")) for a in normalized]
    if after != before:
        changed = True
    return normalized, changed

def _normalize_rate_limits(accounts: list[dict]) -> bool:
    cfg = load_config()
    rate_limits = cfg.get("rate_limits", {})
    changed = False
    now_ts = datetime.now(timezone.utc).timestamp()
    usage_cache = cfg.get("usage_cache", {})
    account_ids = {a.get("id", "") for a in accounts}

    # Drop rate limits for deleted accounts.
    for account_id in list(rate_limits.keys()):
        if account_id not in account_ids:
            rate_limits.pop(account_id, None)
            changed = True

    for account in accounts:
        account_id = account.get("id", "")
        until = rate_limits.get(account_id)
        if not until:
            continue
        remaining = max(0, int(until - now_ts))
        cache = usage_cache.get(account_id, {})
        primary_remaining = cache.get("primary_remaining")

        # Old fallback cooldowns were too aggressive. If usage still shows the account
        # has room left, keep only a short retry window instead of several hours.
        if primary_remaining is not None and primary_remaining > 1 and remaining > 5 * 60:
            rate_limits[account_id] = now_ts + 60
            changed = True

    if changed:
        cfg["rate_limits"] = rate_limits
        save_config(cfg)
    return changed

def cleanup_on_startup():
    """Delete plaintext session files if the user opted in."""
    if not get_setting("cleanup_on_startup", False):
        return
    for path in (CODEX_AUTH, CLAUDE_CREDS):
        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass

# â”€â”€â”€ Auth helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def decode_jwt(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = 4 - len(parts[1]) % 4
        return json.loads(base64.urlsafe_b64decode(parts[1] + "=" * pad))
    except Exception:
        return {}

def _codex_session_has_refresh(session: dict) -> bool:
    return bool(session.get("tokens", {}).get("refresh_token"))

def get_codex_auth_file(account_id: str) -> Path:
    return SESSIONS_DIR / f"codex_{account_id[:8]}.bin"

def get_claude_auth_file(account_id: str) -> Path:
    return SESSIONS_DIR / f"claude_{account_id[:8]}.bin"

def get_cooldown_remaining(account_id: str) -> int:
    until = load_config().get("rate_limits", {}).get(account_id, 0)
    return max(0, int(until - datetime.now(timezone.utc).timestamp()))

def _format_datetime_utc(value) -> str | None:
    """Format unix timestamp / ISO string into a short local datetime."""
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
            return dt.strftime("%b %d, %Y %H:%M")
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
            return dt.strftime("%b %d, %Y %H:%M")
    except Exception:
        return None
    return None

def _mask_email(text: str) -> str:
    """Mask email addresses - show first 2 + last 2 chars of each dot-separated word."""
    def _mask_word(w: str) -> str:
        if len(w) <= 3:
            return w[0] + "***"
        return w[:2] + "***" + w[-2:]

    def _mask_match(m: re.Match) -> str:
        local, domain = m.group(0).split("@", 1)
        masked_local = ".".join(_mask_word(p) for p in local.split("."))
        return f"{masked_local}@{domain}"

    return re.sub(r'[\w.+-]+@[\w.-]+\.\w+', _mask_match, text)


def _get_codex_email_from_session(session: dict) -> str:
    try:
        tokens = session.get("tokens", {})
        tok = tokens.get("id_token") or tokens.get("access_token", "")
        payload = decode_jwt(tok)
        return payload.get("email", "") or ""
    except Exception:
        return ""


def _get_claude_oauth(session: dict) -> dict:
    oauth = session.get("claudeAiOauth")
    if isinstance(oauth, dict):
        return oauth
    return session if isinstance(session, dict) else {}


def _get_claude_email_from_session(session: dict) -> str:
    oauth = _get_claude_oauth(session)
    token_account = oauth.get("tokenAccount") or {}
    profile = oauth.get("profile") or {}
    raw_profile = profile if isinstance(profile, dict) else {}
    for value in (
        token_account.get("emailAddress"),
        token_account.get("email_address"),
        raw_profile.get("emailAddress"),
        raw_profile.get("email_address"),
        raw_profile.get("email"),
    ):
        if value:
            return str(value)
    return ""


def account_status(account: dict) -> tuple[str, str]:
    typ = account.get("type", "")

    if typ == "gemma":
        runtime = _gemma_runtime_status()
        cmd_name = Path(runtime.get("command", "")).name or runtime.get("command", "ollama")
        status_line = {
            "ready": "connected",
            "needs pull": "connected",
            "offline": "found but offline",
            "missing": "not found",
        }.get(runtime.get("status", "missing"), "unknown")
        model_line = runtime.get("model", GEMMA_MODEL_DEFAULT)
        if runtime.get("status") == "ready":
            model_line = f"model installed - {model_line}"
        elif runtime.get("status") == "needs pull":
            model_line = f"model missing - {model_line}"
        elif runtime.get("status") == "offline":
            model_line = f"needs Ollama service - {model_line}"
        else:
            model_line = f"set up Gemma - {model_line}"
        return f"{status_line}\n{cmd_name} - {model_line}", runtime.get("color", BRAND)

    if typ in ("openai_api", "openrouter_api", "anthropic_api"):
        key = account.get("api_key", "")
        if not key:
            return "no key", "#666688"
        masked = key[:8] + "..." + key[-4:] if len(key) > 12 else key
        return f"key set\n{masked}", BRAND

    auth_file = account.get("auth_file", "")
    if not auth_file:
        return "not logged in", "#666688"

    full_path = SESSIONS_DIR / auth_file
    if not full_path.exists():
        return "auth file missing", "#ff6666"

    # Cooldown check - takes priority over session status
    remaining = get_cooldown_remaining(account.get("id", ""))
    if remaining > 0:
        h, m = remaining // 3600, (remaining % 3600) // 60
        return f"rate limited - {h}h {m:02d}m remaining", "#ff9900"

    try:
        data = load_encrypted_session(full_path)
        lock = " [enc]" if is_dpapi_available() else ""

        if typ == "claude":
            oauth = _get_claude_oauth(data)
            plan = oauth.get("subscriptionType")
            tier = oauth.get("rateLimitTier")
            saved_email = account.get("login_email", "") or _get_claude_email_from_session(data)
            expires_at = oauth.get("expiresAt")
            if expires_at:
                exp_value = float(expires_at)
                if exp_value < 10_000_000_000:
                    exp_value *= 1000
                if exp_value < datetime.now(timezone.utc).timestamp() * 1000:
                    extra = plan or tier or "Claude session"
                    if saved_email:
                        return f"expired\n{_mask_email(saved_email)}{lock}", "#ff6666"
                    return f"expired\n{extra}{lock}", "#ff6666"
            extra = _mask_email(saved_email) if saved_email else (plan or tier or "Claude session")
            return f"logged in\n{extra}{lock}", BRAND

        tokens = data.get("tokens", {})
        tok = tokens.get("id_token") or tokens.get("access_token", "")
        payload = decode_jwt(tok)
        exp = payload.get("exp", 0)
        now = datetime.now(timezone.utc).timestamp()
        email = payload.get("email", "?")
        if exp and now > exp:
            if typ == "codex" and _codex_session_has_refresh(data):
                return f"logged in\n{_mask_email(email)}{lock}", BRAND
            return f"expired\n{_mask_email(email)}{lock}", "#ff6666"

        return f"logged in\n{_mask_email(email)}{lock}", BRAND
    except Exception:
        return "logged in", BRAND

# â”€â”€â”€ BAT generator â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _subscription_usage_snapshot(account: dict, typ: str) -> dict:
    auth_file = account.get("auth_file", "")
    if not auth_file or not (SESSIONS_DIR / auth_file).exists():
        return {"error": "Not logged in", "_dashboard": _DASHBOARD_URLS.get(typ, "")}

    try:
        data = load_encrypted_session(SESSIONS_DIR / auth_file)
    except Exception as e:
        return {"error": f"Could not read saved session: {e}", "_dashboard": _DASHBOARD_URLS.get(typ, "")}

    lines = []
    cooldown = get_cooldown_remaining(account.get("id", ""))
    if cooldown > 0:
        h, m = cooldown // 3600, (cooldown % 3600) // 60
        lines.append(f"Router cooldown: {h}h {m:02d}m remaining")
    else:
        lines.append("Router status: available")

    if typ == "codex":
        tokens = data.get("tokens", {})
        payload = decode_jwt(tokens.get("id_token") or tokens.get("access_token", ""))
        auth = payload.get("https://api.openai.com/auth", {})
        email = payload.get("email")
        plan = auth.get("chatgpt_plan_type")
        active_until = _format_datetime_utc(auth.get("chatgpt_subscription_active_until"))
        token_exp = _format_datetime_utc(payload.get("exp"))
        if email:
            lines.append(f"Email: {_mask_email(email)}")
        if plan:
            lines.append(f"Plan: {plan}")
        if active_until:
            lines.append(f"Subscription active until: {active_until}")
        if token_exp:
            lines.append(f"Saved token valid until: {token_exp}")
        note = (
            "Live usage % is not available from the local Codex auth file. "
            "OpenAI's billing endpoint requires a browser session key, not the saved CLI OAuth token."
        )
    else:
        oauth = data.get("claudeAiOauth", data)
        plan = oauth.get("subscriptionType")
        tier = oauth.get("rateLimitTier")
        token_exp = _format_datetime_utc(oauth.get("expiresAt"))
        saved_email = account.get("login_email")
        if saved_email:
            lines.append(f"Email: {_mask_email(saved_email)}")
        if plan:
            lines.append(f"Plan: {plan}")
        if tier:
            lines.append(f"Rate limit tier: {tier}")
        if token_exp:
            lines.append(f"Saved token valid until: {token_exp}")
        note = (
            "Live usage % is not available from the saved Claude Code login alone. "
            "Anthropic exposes detailed usage through browser/admin surfaces rather than this local session file."
        )

    return {
        "detail_lines": lines,
        "note": note,
        "_dashboard": _DASHBOARD_URLS.get(typ, ""),
    }

def generate_bats():
    company_launcher_root = LAUNCHERS_DIR / "companies"
    if company_launcher_root.exists():
        shutil.rmtree(company_launcher_root, ignore_errors=True)
    for mode, path in BAT_FILES.items():
        content = (
            "@echo off\r\n"
            "setlocal\r\n"
            f'python "{ROTATOR_PY}" --mode {mode} %*\r\n'
            "endlocal\r\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for company in get_companies():
        if not company.get("enabled", True):
            continue
        for mode, path in get_company_launchers(company).items():
            content = (
                "@echo off\r\n"
                "setlocal\r\n"
                f'python "{ROTATOR_PY}" --company "{company.get("slug")}" --mode {mode} %*\r\n'
                "endlocal\r\n"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

# â”€â”€â”€ Usage fetch â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _http_get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))

def _http_post_form(url: str, data: dict, headers: dict | None = None) -> dict:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_post_json(url: str, data: dict, headers: dict | None = None):
    encoded = json.dumps(data).encode("utf-8")
    merged_headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=encoded, headers=merged_headers, method="POST")
    return urllib.request.urlopen(req, timeout=15)

def _codex_client_id_from_session(session: dict) -> str:
    tokens = session.get("tokens", {})
    payload = decode_jwt(tokens.get("id_token", ""))
    aud = payload.get("aud", [])
    if isinstance(aud, list) and aud:
        return aud[0]
    if isinstance(aud, str) and aud:
        return aud
    return "app_EMoamEEZ73f0CkXaXp7hrann"

def _refresh_codex_session(session_path: Path, session: dict) -> dict:
    refresh_token = session.get("tokens", {}).get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Saved session has no refresh token.")

    token_data = _http_post_form(
        "https://auth0.openai.com/oauth/token",
        {
            "grant_type": "refresh_token",
            "client_id": _codex_client_id_from_session(session),
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    tokens = session.setdefault("tokens", {})
    tokens["access_token"] = token_data["access_token"]
    if token_data.get("id_token"):
        tokens["id_token"] = token_data["id_token"]
    if token_data.get("refresh_token"):
        tokens["refresh_token"] = token_data["refresh_token"]
    session["last_refresh"] = datetime.now(timezone.utc).isoformat()
    save_encrypted_session(session_path, session)
    return session


def _refresh_claude_session(session_path: Path, session: dict) -> dict:
    oauth = _get_claude_oauth(session)
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise RuntimeError("Saved Claude session has no refresh token.")

    token_data = _http_post_json(
        "https://platform.claude.com/v1/oauth/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
            "scope": "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload",
        },
    )
    with token_data as r:
        payload = json.loads(r.read().decode("utf-8"))

    oauth["accessToken"] = payload["access_token"]
    oauth["refreshToken"] = payload.get("refresh_token") or refresh_token
    expires_in = payload.get("expires_in")
    if expires_in:
        oauth["expiresAt"] = int(datetime.now(timezone.utc).timestamp() * 1000 + int(expires_in) * 1000)
    if payload.get("scope"):
        oauth["scopes"] = [s for s in str(payload["scope"]).split(" ") if s]
    session["claudeAiOauth"] = oauth
    save_encrypted_session(session_path, session)
    return session

def _fetch_codex_subscription_usage(account: dict) -> dict:
    auth_file = account.get("auth_file", "")
    session_path = SESSIONS_DIR / auth_file if auth_file else None
    if not auth_file or not session_path or not session_path.exists():
        return {"error": "Not logged in", "_dashboard": _DASHBOARD_URLS["codex"]}

    try:
        session = load_encrypted_session(session_path)
    except Exception as e:
        return {"error": f"Could not read saved session: {e}", "_dashboard": _DASHBOARD_URLS["codex"]}

    def _request_usage(active_session: dict) -> dict:
        access_token = active_session.get("tokens", {}).get("access_token", "")
        if not access_token:
            raise RuntimeError("Saved session has no access token.")
        return _http_get(
            "https://chatgpt.com/backend-api/wham/usage",
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "User-Agent": "Mozilla/5.0",
            },
        )

    try:
        data = _request_usage(session)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return {"error": f"HTTP {e.code} - {e.reason}", "_dashboard": _DASHBOARD_URLS["codex"]}
        try:
            session = _refresh_codex_session(session_path, session)
            data = _request_usage(session)
        except Exception as refresh_error:
            return {
                "error": f"Could not refresh Codex session: {refresh_error}",
                "_dashboard": _DASHBOARD_URLS["codex"],
            }
    except Exception as e:
        return {"error": str(e), "_dashboard": _DASHBOARD_URLS["codex"]}

    rate_limit = data.get("rate_limit") or {}
    primary = rate_limit.get("primary_window") or {}
    secondary = rate_limit.get("secondary_window") or {}
    code_review = data.get("code_review_rate_limit") or {}
    code_review_primary = code_review.get("primary_window") or {}
    credits = data.get("credits") or {}

    lines = []
    email = data.get("email")
    plan = data.get("plan_type")
    if email:
        lines.append(f"Email: {_mask_email(email)}")
    if plan:
        lines.append(f"Plan: {plan}")

    cooldown = get_cooldown_remaining(account.get("id", ""))
    if cooldown > 0:
        h, m = cooldown // 3600, (cooldown % 3600) // 60
        lines.append(f"Router cooldown: {h}h {m:02d}m remaining")
    else:
        lines.append("Router status: available")

    if primary:
        remaining = max(0, 100 - int(primary.get("used_percent", 0)))
        reset_at = _format_datetime_utc(primary.get("reset_at"))
        line = f"5h limit: {remaining}% remaining"
        if reset_at:
            line += f" (resets {reset_at})"
        lines.append(line)
    if secondary:
        remaining = max(0, 100 - int(secondary.get("used_percent", 0)))
        reset_at = _format_datetime_utc(secondary.get("reset_at"))
        line = f"Weekly limit: {remaining}% remaining"
        if reset_at:
            line += f" (resets {reset_at})"
        lines.append(line)
    if code_review_primary:
        remaining = max(0, 100 - int(code_review_primary.get("used_percent", 0)))
        reset_at = _format_datetime_utc(code_review_primary.get("reset_at"))
        line = f"Code review weekly: {remaining}% remaining"
        if reset_at:
            line += f" (resets {reset_at})"
        lines.append(line)
    if credits.get("has_credits") and not credits.get("unlimited"):
        lines.append(f"Credits remaining: {credits.get('balance', '0')}")

    primary_used = primary.get("used_percent")
    primary_remaining = max(0, 100 - int(primary_used or 0)) if primary else None
    secondary_remaining = max(0, 100 - int(secondary.get("used_percent", 0))) if secondary else None
    summary = None
    if primary_remaining is not None and secondary_remaining is not None:
        summary = f"Remaining: {primary_remaining}% (5h) / {secondary_remaining}% (week)"
    primary_reset = _format_datetime_utc(primary.get("reset_at"))
    secondary_reset = _format_datetime_utc(secondary.get("reset_at"))
    return {
        "pct": primary_used,
        "free_pct": primary_remaining,
        "primary_remaining": primary_remaining,
        "secondary_remaining": secondary_remaining,
        "primary_reset_date": primary_reset,
        "secondary_reset_date": secondary_reset,
        "summary_text": summary,
        "detail_lines": lines,
        "note": None,
        "reset_date": primary_reset,
        "_dashboard": _DASHBOARD_URLS["codex"],
    }


def _fetch_claude_subscription_usage(account: dict) -> dict:
    auth_file = account.get("auth_file", "")
    session_path = SESSIONS_DIR / auth_file if auth_file else None
    if not auth_file or not session_path or not session_path.exists():
        return {"error": "Not logged in", "_dashboard": _DASHBOARD_URLS["claude"]}

    try:
        session = load_encrypted_session(session_path)
    except Exception as e:
        return {"error": f"Could not read saved session: {e}", "_dashboard": _DASHBOARD_URLS["claude"]}

    oauth = _get_claude_oauth(session)
    saved_email = account.get("login_email") or _get_claude_email_from_session(session)
    plan = oauth.get("subscriptionType")
    tier = oauth.get("rateLimitTier")
    token_exp = _format_datetime_utc(oauth.get("expiresAt"))

    def _request_limits(active_session: dict):
        current_oauth = _get_claude_oauth(active_session)
        access_token = current_oauth.get("accessToken")
        if not access_token:
            raise RuntimeError("Saved Claude session has no access token.")
        response = _http_post_json(
            "https://api.anthropic.com/v1/messages",
            {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "quota"}],
                "metadata": {"source": "quota_check"},
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "anthropic-version": "2023-06-01",
                "x-app": "cli",
                "User-Agent": "claude-cli",
                "Accept": "application/json",
            },
        )
        with response as r:
            body = r.read()
            return r.headers, body

    try:
        headers, _ = _request_limits(session)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            if e.code == 403:
                return {
                    "error": "Claude Pro or Max is required for Claude Code login and usage checks.",
                    "_dashboard": _DASHBOARD_URLS["claude"],
                }
            return {"error": f"HTTP {e.code} - {e.reason}", "_dashboard": _DASHBOARD_URLS["claude"]}
        try:
            session = _refresh_claude_session(session_path, session)
            headers, _ = _request_limits(session)
            oauth = _get_claude_oauth(session)
            token_exp = _format_datetime_utc(oauth.get("expiresAt"))
        except urllib.error.HTTPError as refresh_http_error:
            if refresh_http_error.code == 403:
                return {
                    "error": "Claude Pro or Max is required for Claude Code login and usage checks.",
                    "_dashboard": _DASHBOARD_URLS["claude"],
                }
            return {
                "error": f"Could not refresh Claude session: HTTP {refresh_http_error.code} - {refresh_http_error.reason}",
                "_dashboard": _DASHBOARD_URLS["claude"],
            }
        except Exception as refresh_error:
            return {
                "error": f"Could not refresh Claude session: {refresh_error}",
                "_dashboard": _DASHBOARD_URLS["claude"],
            }
    except Exception as e:
        return {"error": str(e), "_dashboard": _DASHBOARD_URLS["claude"]}

    def _header_float(name: str):
        value = headers.get(name)
        if value in (None, ""):
            return None
        try:
            return float(value)
        except Exception:
            return None

    def _header_int(name: str):
        value = headers.get(name)
        if value in (None, ""):
            return None
        try:
            return int(float(value))
        except Exception:
            return None

    five_util = _header_float("anthropic-ratelimit-unified-5h-utilization")
    five_reset_ts = _header_int("anthropic-ratelimit-unified-5h-reset")
    week_util = _header_float("anthropic-ratelimit-unified-7d-utilization")
    week_reset_ts = _header_int("anthropic-ratelimit-unified-7d-reset")
    unified_status = headers.get("anthropic-ratelimit-unified-status")
    representative_claim = headers.get("anthropic-ratelimit-unified-representative-claim")

    five_remaining = max(0, round((1 - five_util) * 100)) if five_util is not None else None
    week_remaining = max(0, round((1 - week_util) * 100)) if week_util is not None else None
    five_reset = _format_datetime_utc(five_reset_ts)
    week_reset = _format_datetime_utc(week_reset_ts)

    lines = []
    if saved_email:
        lines.append(f"Email: {_mask_email(saved_email)}")
    if plan:
        lines.append(f"Plan: {plan}")
    if tier:
        lines.append(f"Rate limit tier: {tier}")
    if token_exp:
        lines.append(f"Saved token valid until: {token_exp}")

    cooldown = get_cooldown_remaining(account.get("id", ""))
    if cooldown > 0:
        h, m = cooldown // 3600, (cooldown % 3600) // 60
        lines.append(f"Router cooldown: {h}h {m:02d}m remaining")
    else:
        lines.append("Router status: available")

    if five_remaining is not None:
        line = f"5h limit: {five_remaining}% remaining"
        if five_reset:
            line += f" (resets {five_reset})"
        lines.append(line)
    if week_remaining is not None:
        line = f"Weekly limit: {week_remaining}% remaining"
        if week_reset:
            line += f" (resets {week_reset})"
        lines.append(line)
    if unified_status:
        lines.append(f"Unified limit status: {unified_status}")
    if representative_claim and representative_claim not in ("five_hour", "seven_day"):
        lines.append(f"Representative claim: {representative_claim}")

    summary = None
    if five_remaining is not None and week_remaining is not None:
        summary = f"Remaining: {five_remaining}% (5h) / {week_remaining}% (week)"

    note = None
    if five_remaining is None and week_remaining is None:
        note = (
            "Claude did not return unified 5h / weekly headers for this session. "
            "Plan info is available, but live usage % could not be derived."
        )

    return {
        "primary_remaining": five_remaining,
        "secondary_remaining": week_remaining,
        "primary_reset_date": five_reset,
        "secondary_reset_date": week_reset,
        "summary_text": summary,
        "detail_lines": lines,
        "note": note,
        "reset_date": five_reset or week_reset,
        "_dashboard": _DASHBOARD_URLS["claude"],
    }


def _fetch_openai_usage(api_key: str) -> dict:
    if not api_key:
        return {"error": "No API key set"}
    try:
        today = datetime.now(timezone.utc)
        start = today.replace(day=1).strftime("%Y-%m-%d")
        end   = today.strftime("%Y-%m-%d")
        h     = {"Authorization": f"Bearer {api_key}"}
        sub   = _http_get("https://api.openai.com/dashboard/billing/subscription", h)
        usage = _http_get(
            f"https://api.openai.com/dashboard/billing/usage?start_date={start}&end_date={end}", h)
        limit = sub.get("hard_limit_usd") or sub.get("soft_limit_usd") or 0
        used  = (usage.get("total_usage") or 0) / 100   # cents -> dollars
        pct   = round(used / limit * 100, 1) if limit else None
        ts    = sub.get("access_until")
        reset = (datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y")
                 if ts else None)
        return {"used": used, "limit": limit, "pct": pct, "reset_date": reset}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} - {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


def _fetch_anthropic_usage(api_key: str) -> dict:
    if not api_key:
        return {"error": "No API key set"}
    try:
        h    = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        data = _http_get("https://api.anthropic.com/v1/usage", h)
        used  = data.get("total_cost_usd") or data.get("used_usd")
        limit = data.get("limit_usd") or data.get("hard_limit_usd")
        pct   = round(used / limit * 100, 1) if (used and limit) else None
        reset = data.get("reset_date") or data.get("next_reset_date")
        return {"used": used, "limit": limit, "pct": pct, "reset_date": reset, "_raw": data}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} - {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


_DASHBOARD_URLS = {
    "codex":  "https://chatgpt.com/#settings/usage",
    "claude": "https://claude.ai/settings/billing",
}

def _fetch_subscription_usage(account: dict, typ: str) -> dict:
    if typ == "codex":
        return _fetch_codex_subscription_usage(account)
    if typ == "claude":
        return _fetch_claude_subscription_usage(account)
    return _subscription_usage_snapshot(account, typ)


def _fetch_usage(account: dict) -> dict:
    typ = account.get("type", "")
    if typ == "openai_api":
        return _fetch_openai_usage(account.get("api_key", ""))
    if typ == "openrouter_api":
        return {
            "detail_lines": [
                "OpenRouter API account is connected via API key.",
                "Live 5h/weekly subscription usage is not available for this account type.",
                "Use OpenRouter dashboard for credit and usage details.",
            ],
            "note": "Routing works with OPENAI_API_KEY + OPENAI_BASE_URL.",
            "_dashboard": "https://openrouter.ai/settings/credits",
        }
    if typ == "anthropic_api":
        return _fetch_anthropic_usage(account.get("api_key", ""))
    if typ == "gemma":
        cmd = get_tool_cmd("gemma")
        return {
            "detail_lines": [
                "Gemma 4 is a local provider branch.",
                f"Command: {cmd}",
                f"Model: {get_setting('gemma_model', GEMMA_MODEL_DEFAULT)}",
                "No subscription usage is tracked for this account type.",
            ],
            "note": "Use this branch for local or self-hosted Gemma 4 execution.",
            "_dashboard": "",
        }
    if typ in ("codex", "claude"):
        return _fetch_subscription_usage(account, typ)
    return {"error": "Unsupported account type"}


# â”€â”€â”€ GUI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

BG      = "#1e1e1e"
BG2     = "#252525"
BG3     = "#181818"
BG_ACTIVE = "#23384a"
FG      = "#e0e0e0"
FG_MUTE = "#888899"
GREEN   = "#00cc66"
BLUE    = "#4db8ff"
RED     = "#ff6666"
ACCENT  = "#444444"
YELLOW  = "#f5a623"  # kept for reference
BRAND   = "#4db8ff"  # logo light blue


class RotatorManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Subs Rotator  v{VERSION}")
        self.geometry("1240x860")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.login_busy = False
        self.gemma_install_busy = False
        self._supported_adapters_open = False
        self._executables_open = False
        self._settings_open = False
        self._bat_group_state = {"__global__": False}
        self._tool_status_vars: dict[str, tk.StringVar] = {}
        self._usage_fetch_inflight = set()
        self._last_accounts_snapshot = None
        self._last_seen_routing_id = None
        self._last_seen_routing_live = False
        self.company_tree_meta = {}
        cleanup_on_startup()
        generate_bats()
        self._build_ui()
        self.after(80, self._maximize_window)
        self.refresh_accounts()
        self._schedule_refresh()
        self._schedule_usage_refresh()

    # â”€â”€ UI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _build_ui(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)

        self.page_canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        page_scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.page_canvas.yview)
        self.page_canvas.configure(yscrollcommand=page_scrollbar.set)
        page_scrollbar.pack(side="right", fill="y")
        self.page_canvas.pack(side="left", fill="both", expand=True)

        self.page_frame = tk.Frame(self.page_canvas, bg=BG)
        self.page_window = self.page_canvas.create_window((0, 0), window=self.page_frame, anchor="nw")
        self.page_frame.bind("<Configure>", self._on_page_frame_configure)
        self.page_canvas.bind("<Configure>", self._on_page_canvas_configure)
        self.page_canvas.bind_all("<MouseWheel>", self._on_global_mousewheel)
        root = self.page_frame
        # â”€â”€ Header â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=18, pady=(14, 0))

        # Logo â€” full height of header text block (~80px: title + subtitle + version)
        logo_path = APP_DIR / "logo.jpg"
        if _PIL_AVAILABLE and logo_path.exists():
            img = Image.open(logo_path)
            img.thumbnail((240, 80), Image.LANCZOS)
            self._logo_img = ImageTk.PhotoImage(img)
            tk.Label(header, image=self._logo_img, bg=BG
                     ).pack(side="left", padx=(0, 16))

        left_head = tk.Frame(header, bg=BG)
        left_head.pack(side="left", fill="x", expand=True)

        title_row = tk.Frame(left_head, bg=BG)
        title_row.pack(anchor="w")
        tk.Label(title_row, text="Subs Rotator",
                 font=("Segoe UI", 16, "bold"), bg=BG, fg=FG
                 ).pack(side="left")
        tk.Label(title_row, text="  by ShoshiBuilds",
                 font=("Segoe UI", 10), bg=BG, fg=FG_MUTE
                 ).pack(side="left", pady=(4, 0))
        tk.Label(left_head,
                 text="Run Paperclip for free with Gemma 4 via Ollama, plus Codex/Claude/API fallback rotation.",
                 font=("Segoe UI", 9), bg=BG, fg=FG_MUTE
                 ).pack(anchor="w")
        enc_status = "Encryption: DPAPI (Windows)" if is_dpapi_available() else "Encryption: unavailable"
        enc_color  = "#00aa44" if is_dpapi_available() else "#ff6666"
        ver_row = tk.Frame(left_head, bg=BG)
        ver_row.pack(anchor="w", pady=(2, 0))
        tk.Label(ver_row, text=f"v{VERSION}",
                 font=("Segoe UI", 8, "bold"), bg=BG, fg=BRAND
                 ).pack(side="left")
        tk.Label(ver_row, text=f"  -  Open source  -  {enc_status}",
                 font=("Segoe UI", 8), bg=BG, fg=BRAND
                 ).pack(side="left")
        self.routing_status_var = tk.StringVar(value="Routing: idle")
        tk.Label(left_head,
                 textvariable=self.routing_status_var,
                 font=("Segoe UI", 9, "bold"), bg=BG, fg="#8fd3ff"
                 ).pack(anchor="w", pady=(4, 0))

        # â”€â”€ Top-right links â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        right_head = tk.Frame(header, bg=BG)
        right_head.pack(side="right", anchor="n", pady=4)

        tk.Button(right_head,
                  text="Support on Ko-fi",
                  font=("Segoe UI", 8, "bold"),
                  bg=BRAND, fg="#0a0a1a",
                  relief="flat", padx=10, pady=4,
                  cursor="hand2",
                  command=lambda: webbrowser.open(BMC_URL)
                  ).pack(anchor="e", pady=(0, 4))

        tk.Button(right_head,
                  text="? Help",
                  font=("Segoe UI", 8),
                  bg="#2a2a3a", fg=FG,
                  relief="flat", padx=10, pady=4,
                  cursor="hand2",
                  command=self._show_help
                  ).pack(anchor="e", pady=(0, 4))

        tk.Button(right_head,
                  text="GitHub",
                  font=("Segoe UI", 8),
                  bg=ACCENT, fg=FG,
                  relief="flat", padx=10, pady=4,
                  cursor="hand2",
                  command=lambda: webbrowser.open(GITHUB_URL)
                  ).pack(anchor="e")

        ttk.Separator(root, orient="horizontal").pack(fill="x", padx=18, pady=10)

        # Launcher paths section
        bat_outer = tk.Frame(root, bg="#222222")
        bat_outer.pack(fill="x", padx=18, pady=(0, 12))

        header_row = tk.Frame(bat_outer, bg="#222222")
        header_row.pack(fill="x", padx=4, pady=(8, 2))

        tk.Label(header_row, text="  Launcher paths - copy Command path for integrations:",
                 font=("Segoe UI", 9, "bold"), bg="#222222", fg=FG
                 ).pack(side="left", anchor="w")
        tk.Label(header_row,
                text="  Live lanes: Codex / Claude / Gemma 4. OpenAI/OpenRouter/Anthropic API accounts run inside these lanes.",
                 font=("Segoe UI", 8), bg="#222222", fg="#555555"
                 ).pack(side="left", anchor="w", padx=(8, 0))
        tk.Button(
            header_row,
            text="Collapse all",
            font=("Segoe UI", 8),
            bg="#2a2a2a",
            fg=FG,
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
            command=lambda: self._set_all_bat_groups(False),
        ).pack(side="right", padx=(4, 0))
        tk.Button(
            header_row,
            text="Expand all",
            font=("Segoe UI", 8),
            bg=ACCENT,
            fg=FG,
            relief="flat",
            padx=8,
            pady=2,
            cursor="hand2",
            command=lambda: self._set_all_bat_groups(True),
        ).pack(side="right", padx=(4, 0))

        lanes_shell = tk.Frame(bat_outer, bg="#1c1c1c")
        lanes_shell.pack(fill="x", padx=4, pady=(0, 8))
        lanes_header = tk.Frame(lanes_shell, bg="#1c1c1c")
        lanes_header.pack(fill="x", padx=4, pady=(6, 2))
        tk.Button(
            lanes_header,
            text="v" if self._supported_adapters_open else ">",
            font=("Segoe UI", 8, "bold"),
            bg="#2a2a2a",
            fg=FG,
            relief="flat",
            padx=6,
            pady=1,
            cursor="hand2",
            command=self._toggle_supported_adapters,
        ).pack(side="left", padx=(0, 6))
        tk.Label(
            lanes_header,
            text="Adapter lanes",
            font=("Segoe UI", 8, "bold"),
            bg="#1c1c1c",
            fg=FG_MUTE,
        ).pack(side="left")
        tk.Label(
            lanes_header,
            text="(only fully wired lanes are shown)",
            font=("Segoe UI", 8),
            bg="#1c1c1c",
            fg="#555555",
        ).pack(side="left", padx=(8, 0))

        self.adapter_lanes_body = tk.Frame(lanes_shell, bg="#1c1c1c")
        if self._supported_adapters_open:
            self.adapter_lanes_body.pack(fill="x", padx=8, pady=(0, 8))
        self._render_supported_adapters()

        self.bat_rows_frame = tk.Frame(bat_outer, bg="#222222")
        self.bat_rows_frame.pack(fill="x", padx=8, pady=(0, 6))

        executables_outer = tk.Frame(root, bg="#1a1a1a")
        executables_outer.pack(fill="x", padx=18, pady=(0, 10))

        exec_header = tk.Frame(executables_outer, bg="#1a1a1a")
        exec_header.pack(fill="x", padx=4, pady=(6, 2))
        tk.Button(
            exec_header,
            text="v" if self._executables_open else ">",
            font=("Segoe UI", 8, "bold"),
            bg="#2a2a2a",
            fg=FG,
            relief="flat",
            padx=6,
            pady=1,
            cursor="hand2",
            command=self._toggle_executables_section,
        ).pack(side="left", padx=(0, 6))
        tk.Label(exec_header, text="Executables:",
                 font=("Segoe UI", 8, "bold"), bg="#1a1a1a", fg=FG_MUTE
                 ).pack(side="left", padx=(0, 8))
        tk.Label(exec_header,
                 text="Codex, Claude and Gemma 4 paths live here. Found automatically entries are marked inline.",
                 font=("Segoe UI", 8), bg="#1a1a1a", fg="#444455"
                 ).pack(side="left")

        self.executables_body = tk.Frame(executables_outer, bg="#1a1a1a")
        if self._executables_open:
            self.executables_body.pack(fill="x", pady=(0, 0))

        self.codex_cmd_var = tk.StringVar()
        self.claude_cmd_var = tk.StringVar()
        self.gemma_cmd_var = tk.StringVar()
        self.gemma_model_var = tk.StringVar(value=get_setting("gemma_model", GEMMA_MODEL_DEFAULT))
        self.gemma_install_status_var = tk.StringVar(value="Ready")

        self._build_tool_path_row(self.executables_body, "Codex", self.codex_cmd_var, "codex")
        self._build_tool_path_row(self.executables_body, "Claude", self.claude_cmd_var, "claude")
        self._build_tool_path_row(self.executables_body, "Gemma 4", self.gemma_cmd_var, "gemma")
        self._build_gemma_model_row(self.executables_body)

        # â”€â”€ Security settings â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        settings_outer = tk.Frame(root, bg="#1a1a1a")
        settings_outer.pack(fill="x", padx=18, pady=(0, 10))
        settings_header = tk.Frame(settings_outer, bg="#1a1a1a")
        settings_header.pack(fill="x", padx=4, pady=(6, 2))
        tk.Button(
            settings_header,
            text="?" if self._settings_open else "?",
            font=("Segoe UI", 8, "bold"),
            bg="#2a2a2a",
            fg=FG,
            relief="flat",
            padx=6,
            pady=1,
            cursor="hand2",
            command=self._toggle_settings_section,
        ).pack(side="left", padx=(0, 6))
        tk.Label(settings_header, text="Settings:",
                 font=("Segoe UI", 8, "bold"), bg="#1a1a1a", fg=FG_MUTE
                 ).pack(side="left", padx=(0, 8))
        tk.Label(settings_header,
                 text="Security, backup and routing preferences.",
                 font=("Segoe UI", 8), bg="#1a1a1a", fg="#444455"
                 ).pack(side="left")

        self.settings_body = tk.Frame(settings_outer, bg="#1a1a1a")
        if self._settings_open:
            self.settings_body.pack(fill="x", pady=(0, 0))

        sec_frame = tk.Frame(self.settings_body, bg="#1a1a1a")
        sec_frame.pack(fill="x", padx=0, pady=(0, 10))

        tk.Label(sec_frame, text="  Security:",
                 font=("Segoe UI", 8, "bold"), bg="#1a1a1a", fg=FG_MUTE
                 ).pack(side="left", padx=(4, 12), pady=6)

        self.cleanup_var = tk.BooleanVar(value=get_setting("cleanup_on_startup", False))
        tk.Checkbutton(
            sec_frame,
            text="Delete plaintext session files on next startup",
            variable=self.cleanup_var,
            bg="#1a1a1a", fg=FG, selectcolor="#1a1a1a", activebackground="#1a1a1a",
            font=("Segoe UI", 8),
            command=self._toggle_cleanup,
        ).pack(side="left")

        tk.Label(sec_frame,
                 text="(~/.codex/auth.json and ~/.claude/.credentials.json)",
                 font=("Segoe UI", 8), bg="#1a1a1a", fg="#444455"
                 ).pack(side="left", padx=(8, 0))

        backup_frame = tk.Frame(self.settings_body, bg="#1a1a1a")
        backup_frame.pack(fill="x", padx=0, pady=(0, 10))

        tk.Label(backup_frame, text="  Backup (cross-PC):",
                 font=("Segoe UI", 8, "bold"), bg="#1a1a1a", fg=FG_MUTE
                 ).pack(side="left", padx=(4, 8), pady=6)
        tk.Button(backup_frame, text="Export",
                  font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                  relief="flat", padx=8, pady=2, cursor="hand2",
                  command=self._export_backup
                  ).pack(side="left", padx=2)
        tk.Button(backup_frame, text="Import",
                  font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                  relief="flat", padx=8, pady=2, cursor="hand2",
                  command=self._import_backup
                  ).pack(side="left", padx=2)
        tk.Label(backup_frame,
                 text="Password-encrypted ? restores all accounts on another PC",
                 font=("Segoe UI", 8), bg="#1a1a1a", fg="#444455"
                 ).pack(side="left", padx=(10, 0))

        usage_frame = tk.Frame(self.settings_body, bg="#1a1a1a")
        usage_frame.pack(fill="x", padx=0, pady=(0, 10))

        tk.Label(usage_frame, text="  Auto-skip account when usage ?",
                 font=("Segoe UI", 8, "bold"), bg="#1a1a1a", fg=FG_MUTE
                 ).pack(side="left", padx=(4, 4), pady=6)
        self.threshold_var = tk.IntVar(value=get_setting("usage_limit_pct", 100))
        spn = tk.Spinbox(usage_frame, from_=50, to=100,
                         textvariable=self.threshold_var, width=4,
                         font=("Segoe UI", 8), bg="#2a2a2a", fg=FG,
                         buttonbackground=ACCENT, relief="flat",
                         command=self._save_threshold)
        spn.pack(side="left", padx=2)
        spn.bind("<Return>",   lambda _: self._save_threshold())
        spn.bind("<FocusOut>", lambda _: self._save_threshold())
        tk.Label(usage_frame,
                 text="%   (usage refreshed automatically every 10 min)",
                 font=("Segoe UI", 8), bg="#1a1a1a", fg="#444455"
                 ).pack(side="left", padx=(4, 0))

        retry_frame = tk.Frame(self.settings_body, bg="#1a1a1a")
        retry_frame.pack(fill="x", padx=0, pady=(0, 10))

        tk.Label(retry_frame, text="  Auto-retry after unclear limit =",
                 font=("Segoe UI", 8, "bold"), bg="#1a1a1a", fg=FG_MUTE
                 ).pack(side="left", padx=(4, 4), pady=6)
        self.retry_minutes_var = tk.IntVar(value=get_setting("fallback_retry_minutes", 1))
        retry_spn = tk.Spinbox(retry_frame, from_=1, to=60,
                               textvariable=self.retry_minutes_var, width=4,
                               font=("Segoe UI", 8), bg="#2a2a2a", fg=FG,
                               buttonbackground=ACCENT, relief="flat",
                               command=self._save_retry_minutes)
        retry_spn.pack(side="left", padx=2)
        retry_spn.bind("<Return>", lambda _: self._save_retry_minutes())
        retry_spn.bind("<FocusOut>", lambda _: self._save_retry_minutes())
        tk.Label(retry_frame,
                 text="min   (used only when the rate-limit reset time is unclear)",
                 font=("Segoe UI", 8), bg="#1a1a1a", fg="#444455"
                 ).pack(side="left", padx=(4, 0))

        add_bar = tk.Frame(root, bg=BG)
        add_bar.pack(fill="x", padx=18, pady=(0, 6))

        tk.Label(add_bar, text="Accounts:", font=("Segoe UI", 10, "bold"),
                 bg=BG, fg=FG).pack(side="left")

        for label, typ in [
            ("+ Anthropic API", "anthropic_api"),
            ("+ OpenRouter API", "openrouter_api"),
            ("+ OpenAI API", "openai_api"),
            ("+ Claude", "claude"),
            ("+ Gemma 4", "gemma"),
            ("+ Codex Subscription", "codex"),
        ]:
            tk.Button(add_bar, text=label, font=("Segoe UI", 8),
                      bg=ACCENT, fg=FG, relief="flat", padx=8, pady=3,
                      cursor="hand2",
                      command=lambda t=typ: self._add_account(t)
                      ).pack(side="right", padx=3)

        # â”€â”€ Scrollable account list â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        company_bar = tk.Frame(root, bg=BG)
        company_bar.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(company_bar, text="Companies:", font=("Segoe UI", 10, "bold"),
                 bg=BG, fg="#7fd3ff").pack(side="left")
        tk.Button(company_bar, text="+ Company",
                  font=("Segoe UI", 8, "bold"), bg="#24425a", fg="#aee4ff",
                  relief="flat", padx=8, pady=3,
                  cursor="hand2",
                  command=self._add_company
                  ).pack(side="right", padx=3)

        company_tree_wrap = tk.Frame(root, bg="#1f2833", highlightthickness=1, highlightbackground="#355a77")
        company_tree_wrap.pack(fill="x", padx=18, pady=(0, 10))

        tree_toolbar = tk.Frame(company_tree_wrap, bg="#1f2833")
        tree_toolbar.pack(fill="x", padx=8, pady=(8, 4))

        self.company_tree_status_var = tk.StringVar(
            value="Tree view shows each company with dedicated accounts plus shared fallback accounts."
        )
        tk.Label(tree_toolbar, textvariable=self.company_tree_status_var,
                 font=("Segoe UI", 8, "bold"), bg="#1f2833", fg="#8fd3ff"
                 ).pack(side="left")

        tk.Button(tree_toolbar, text="Copy Claude BAT",
                  font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                  relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda: self._copy_selected_company_bat("claude")
                  ).pack(side="right", padx=(4, 0))
        tk.Button(tree_toolbar, text="Copy Gemma BAT",
                  font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                  relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda: self._copy_selected_company_bat("gemma")
                  ).pack(side="right", padx=(4, 0))
        tk.Button(tree_toolbar, text="Copy Codex BAT",
                  font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                  relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda: self._copy_selected_company_bat("codex")
                  ).pack(side="right", padx=(4, 0))
        tk.Button(tree_toolbar, text="Rename",
                  font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                  relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=self._rename_selected_company
                  ).pack(side="right", padx=(4, 0))
        tk.Button(tree_toolbar, text="Enable/Disable",
                  font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                  relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=self._toggle_selected_company
                  ).pack(side="right", padx=(4, 0))
        tk.Button(tree_toolbar, text="Delete",
                  font=("Segoe UI", 8), bg="#3a2020", fg=RED,
                  relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=self._delete_selected_company
                  ).pack(side="right", padx=(4, 0))

        tree_frame = tk.Frame(company_tree_wrap, bg="#1f2833")
        tree_frame.pack(fill="x", padx=8, pady=(0, 8))

        tree_style = ttk.Style()
        try:
            tree_style.theme_use("clam")
        except Exception:
            pass
        tree_style.configure(
            "Rotator.Treeview",
            background=BG3,
            foreground=FG,
            fieldbackground=BG3,
            borderwidth=0,
            rowheight=24,
        )
        tree_style.configure(
            "Rotator.Treeview.Heading",
            background=BG2,
            foreground=FG_MUTE,
            relief="flat",
        )
        tree_style.map(
            "Rotator.Treeview",
            background=[("selected", BG_ACTIVE)],
            foreground=[("selected", FG)],
            fieldbackground=[("!disabled", BG3)],
        )
        tree_style.map(
            "Rotator.Treeview.Heading",
            background=[("active", BG2), ("!disabled", BG2)],
            foreground=[("active", FG), ("!disabled", FG_MUTE)],
        )

        self.company_tree = ttk.Treeview(
            tree_frame,
            columns=("kind", "scope"),
            show="tree headings",
            height=5,
            style="Rotator.Treeview",
        )
        self.company_tree.heading("#0", text="Company / Connection")
        self.company_tree.heading("kind", text="Type")
        self.company_tree.heading("scope", text="Scope")
        self.company_tree.column("#0", width=360, anchor="w")
        self.company_tree.column("kind", width=130, anchor="w")
        self.company_tree.column("scope", width=420, anchor="w")
        self.company_tree.pack(side="left", fill="x", expand=True)
        self.company_tree.bind("<<TreeviewSelect>>", self._on_company_tree_select)

        company_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.company_tree.yview)
        company_scroll.pack(side="right", fill="y")
        self.company_tree.configure(yscrollcommand=company_scroll.set)

        accounts_wrap = tk.Frame(root, bg=BG2)
        accounts_wrap.pack(fill="both", expand=True, padx=18, pady=(0, 8))

        accounts_header = tk.Frame(accounts_wrap, bg=BG2)
        accounts_header.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(accounts_header, text="Accounts",
                 font=("Segoe UI", 10, "bold"), bg=BG2, fg=FG
                 ).pack(side="left")
        tk.Label(accounts_header,
                 text="Assign accounts to companies from each account row. Companies stay managed in the block above.",
                 font=("Segoe UI", 8), bg=BG2, fg=FG_MUTE
                 ).pack(side="left", padx=(10, 0))

        canvas_frame = tk.Frame(accounts_wrap, bg=BG)
        canvas_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.accounts_frame = tk.Frame(self.canvas, bg=BG)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.accounts_frame, anchor="nw")
        self.accounts_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.accounts_frame.bind("<MouseWheel>", self._on_mousewheel)

        # â”€â”€ Footer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        ttk.Separator(root, orient="horizontal").pack(fill="x", padx=18, pady=6)

        footer = tk.Frame(root, bg=BG)
        footer.pack(fill="x", padx=18, pady=(0, 4))
        tk.Label(footer,
                 text="Subs Rotator is free and open source. If it saves you time, consider supporting on Ko-fi.",
                 font=("Segoe UI", 8), bg=BG, fg="#444455"
                 ).pack(side="left")
        tk.Button(footer,
                  text="ko-fi.com/shoshibuilds",
                  font=("Segoe UI", 8),
                  bg=BG, fg=BRAND,
                  relief="flat", padx=0, pady=0,
                  cursor="hand2", bd=0,
                  command=lambda: webbrowser.open(BMC_URL)
                  ).pack(side="left", padx=(4, 0))

        # â”€â”€ Log â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        tk.Label(root, text="Log:", font=("Segoe UI", 8, "bold"),
                 bg=BG, fg=FG_MUTE).pack(anchor="w", padx=18)
        self.log = scrolledtext.ScrolledText(root, height=4,
                                              font=("Consolas", 8),
                                              bg=BG3, fg=FG,
                                              insertbackground="white",
                                              relief="flat", state="disabled")
        self.log.pack(fill="x", padx=18, pady=(2, 14))

    def _maximize_window(self):
        try:
            self.state("zoomed")
        except Exception:
            try:
                self.attributes("-zoomed", True)
            except Exception:
                pass

    def _on_page_frame_configure(self, _):
        self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all"))

    def _on_page_canvas_configure(self, event):
        self.page_canvas.itemconfig(self.page_window, width=event.width)

    def _on_global_mousewheel(self, event):
        if getattr(self, "page_canvas", None) is None:
            return
        step = event.delta // 120
        if not step:
            return
        widget = self.winfo_containing(event.x_root, event.y_root)
        if widget is self.log or self._widget_is_descendant(widget, self.log):
            return
        self.page_canvas.yview_scroll(-1 * step, "units")
        return "break"

    def _widget_is_descendant(self, widget, ancestor):
        while widget is not None:
            if widget is ancestor:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _on_frame_configure(self, _):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        step = event.delta // 120
        if step:
            self.canvas.yview_scroll(-1 * step, "units")
        return "break"

    def _build_tool_path_row(self, parent, label: str, var: tk.StringVar, tool: str):
        row = tk.Frame(parent, bg="#1a1a1a")
        row.pack(fill="x", padx=8, pady=2)

        resolved_value, status_text = _tool_path_status(tool)
        if not var.get().strip():
            var.set(resolved_value)
        status_var = tk.StringVar(value=status_text)
        self._tool_status_vars[tool] = status_var

        tk.Label(row, text=f"{label} path:", width=14,
                 font=("Segoe UI", 8, "bold"), bg="#1a1a1a", fg=FG_MUTE, anchor="w"
                 ).pack(side="left")

        entry = tk.Entry(row, textvariable=var,
                         font=("Consolas", 8), bg="#2a2a2a", fg=FG,
                         insertbackground=FG, relief="flat")
        entry.pack(side="left", fill="x", expand=True, padx=(0, 4), ipady=3)
        entry.bind("<FocusOut>", lambda e, t=tool, v=var: self._save_tool_path(t, v.get()))
        entry.bind("<Return>", lambda e, t=tool, v=var: self._save_tool_path(t, v.get()))

        tk.Button(row, text="Browse", font=("Segoe UI", 8),
                  bg=ACCENT, fg=FG, relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda t=tool, v=var: self._browse_tool_path(t, v)
                  ).pack(side="left", padx=(0, 4))

        tk.Button(row, text="Auto", font=("Segoe UI", 8),
                  bg=ACCENT, fg=FG, relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=lambda t=tool, v=var: self._reset_tool_path(t, v)
                  ).pack(side="left")

        tk.Label(
            row,
            textvariable=status_var,
            font=("Segoe UI", 8, "italic"),
            bg="#1a1a1a",
            fg=BRAND if status_text == "found automatically" else (YELLOW if status_text == "user set" else "#ff9900"),
            anchor="w",
        ).pack(side="left", padx=(8, 0))

    def _build_gemma_model_row(self, parent):
        row = tk.Frame(parent, bg="#1a1a1a")
        row.pack(fill="x", padx=8, pady=(2, 8))

        tk.Label(row, text="Gemma model:",
                 width=14,
                 font=("Segoe UI", 8, "bold"), bg="#1a1a1a", fg=FG_MUTE, anchor="w"
                 ).pack(side="left")

        combo = ttk.Combobox(
            row,
            textvariable=self.gemma_model_var,
            values=GEMMA_MODELS,
            state="readonly",
            width=20,
            font=("Segoe UI", 8),
        )
        combo.pack(side="left", fill="x", expand=True, padx=(0, 4), ipady=1)
        combo.bind("<<ComboboxSelected>>", lambda e: self._save_gemma_model())

        tk.Button(row, text="Install / Pull", font=("Segoe UI", 8),
                  bg=ACCENT, fg=FG, relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=self._install_gemma_model
                  ).pack(side="left", padx=(0, 4))

        tk.Button(row, text="Save", font=("Segoe UI", 8),
                  bg=ACCENT, fg=FG, relief="flat", padx=8, pady=2,
                  cursor="hand2",
                  command=self._save_gemma_model
                  ).pack(side="left")
        tk.Label(row, textvariable=self.gemma_install_status_var,
                 font=("Segoe UI", 8), bg="#1a1a1a", fg="#555555",
                 anchor="w").pack(side="left", padx=(10, 0))

    def _render_supported_adapters(self):
        if not hasattr(self, "adapter_lanes_body"):
            return
        for w in self.adapter_lanes_body.winfo_children():
            w.destroy()

        lanes_grid = tk.Frame(self.adapter_lanes_body, bg="#1c1c1c")
        lanes_grid.pack(fill="x")
        for idx, (label, status, ready) in enumerate(SUPPORTED_ADAPTER_LANES):
            row = tk.Frame(lanes_grid, bg="#1c1c1c")
            row.grid(row=idx // 2, column=idx % 2, sticky="ew", padx=4, pady=2)
            lanes_grid.grid_columnconfigure(idx % 2, weight=1)
            tk.Label(
                row,
                text=f"{label} ({status})",
                font=("Segoe UI", 8, "bold"),
                bg="#1c1c1c",
                fg=FG,
                anchor="w",
                justify="left",
            ).pack(side="left")
            badge_bg = "#1f3a2a" if ready else "#3a2f1f"
            badge_fg = "#7dffb3" if ready else "#ffcf7d"
            tk.Label(
                row,
                text=("live now" if ready else "coming soon"),
                font=("Segoe UI", 8),
                bg=badge_bg,
                fg=badge_fg,
                padx=6,
                pady=1,
            ).pack(side="left", padx=(8, 0))

    def _toggle_supported_adapters(self):
        self._supported_adapters_open = not self._supported_adapters_open
        if hasattr(self, "adapter_lanes_body"):
            if self._supported_adapters_open:
                self.adapter_lanes_body.pack(fill="x", padx=8, pady=(0, 8))
            else:
                self.adapter_lanes_body.pack_forget()
        self._render_supported_adapters()

    def _toggle_executables_section(self):
        self._executables_open = not self._executables_open
        if hasattr(self, "executables_body"):
            if self._executables_open:
                self.executables_body.pack(fill="x", pady=(0, 0))
            else:
                self.executables_body.pack_forget()

    def _toggle_settings_section(self):
        self._settings_open = not self._settings_open
        if hasattr(self, "settings_body"):
            if self._settings_open:
                self.settings_body.pack(fill="x", pady=(0, 0))
            else:
                self.settings_body.pack_forget()

    def _set_all_bat_groups(self, open_state: bool):
        self._bat_group_state["__global__"] = open_state
        for company in get_companies():
            if company.get("enabled", True):
                self._bat_group_state[company.get("id", "")] = open_state
        self._render_bat_rows()

    def _render_bat_rows(self):
        for w in self.bat_rows_frame.winfo_children():
            w.destroy()

        rows = [("Global fallback", BAT_FILES, "__global__", False)]
        for company in get_companies():
            if company.get("enabled", True):
                company = dict(company)
                rows.append((f"Company: {company.get('name', company.get('slug', '?'))}", get_company_launchers(company), company.get("id", ""), False))

        for heading, launchers, key, default_open in rows:
            open_state = self._bat_group_state.get(key, default_open)
            self._bat_group_state[key] = open_state

            section = tk.Frame(self.bat_rows_frame, bg="#222222")
            section.pack(fill="x", padx=4, pady=(6, 2))

            header = tk.Frame(section, bg="#222222")
            header.pack(fill="x")

            toggle_btn = tk.Button(
                header,
                text="v" if open_state else ">",
                font=("Segoe UI", 8, "bold"),
                bg="#2a2a2a",
                fg=FG,
                relief="flat",
                padx=6,
                pady=1,
                cursor="hand2",
                command=lambda k=key: self._toggle_bat_group(k),
            )
            toggle_btn.pack(side="left", padx=(0, 6))

            launchers_count = len(launchers)
            tk.Label(header, text=f"{heading}  ({launchers_count} launcher{'s' if launchers_count != 1 else ''})",
                     font=("Segoe UI", 8, "bold"), bg="#222222", fg=BRAND
                     ).pack(side="left", anchor="w")
            tk.Label(
                header,
                text="Ready now: Codex / Claude / Gemma 4 (API accounts run via these lanes)",
                font=("Segoe UI", 8),
                bg="#222222",
                fg="#555555",
            ).pack(side="left", padx=(8, 0))

            body = tk.Frame(section, bg="#222222")
            if open_state:
                body.pack(fill="x", padx=(18, 0), pady=(4, 0))

                if key == "__global__":
                    mode_order = ("codex", "claude", "gemma", "all")
                else:
                    mode_order = ("codex", "claude", "gemma")

                for mode in mode_order:
                    if mode not in launchers:
                        continue
                    mode_label = BAT_MODE_LABELS.get(mode, mode)
                    row = tk.Frame(body, bg="#222222")
                    row.pack(fill="x", pady=2)
                    path = launchers[mode]
                    tk.Label(row, text=f"{mode_label}:",
                             width=14,
                             font=("Segoe UI", 8, "bold"), bg="#222222", fg=FG_MUTE, anchor="w"
                             ).pack(side="left")
                    tk.Label(row, text=str(path),
                             font=("Consolas", 8), bg="#2a2a2a", fg=FG,
                             anchor="w", padx=6, pady=3
                             ).pack(side="left", fill="x", expand=True)
                    tk.Button(row, text="Copy", font=("Segoe UI", 8),
                              bg=ACCENT, fg=FG, relief="flat", padx=8, pady=2,
                              cursor="hand2",
                              command=lambda p=path: self._copy_path(p)
                              ).pack(side="left", padx=(4, 0))

            section._toggle_btn = toggle_btn  # keep a reference for Tk
            section._body = body

    def _render_companies(self):
        companies = get_companies()
        self.company_tree_meta = {}
        for item in self.company_tree.get_children():
            self.company_tree.delete(item)
        if not companies:
            self.company_tree_status_var.set(
                "No companies yet. Add a company above, then assign compatible Codex/Claude/API accounts to it."
            )
            return

            self.company_tree_status_var.set(
                "Companies are managed here. Use each account row below to attach Codex/OpenAI, Claude/Anthropic, or Gemma 4 local branches to a company."
            )
        accounts = get_accounts()
        shared_accounts = [a for a in accounts if not a.get("company_ids", [])]

        for idx, company in enumerate(companies):
            dedicated = [a for a in accounts if company["id"] in a.get("company_ids", [])]
            company_item = self.company_tree.insert(
                "",
                "end",
                text=f"{company.get('name', company.get('slug', '?'))}  #{idx + 1}",
                values=(
                    "company",
                    "active" if company.get("enabled", True) else "disabled",
                ),
                open=True,
            )
            self.company_tree_meta[company_item] = {"kind": "company", "company": company}
            for account in dedicated:
                account_item = self.company_tree.insert(
                    company_item,
                    "end",
                    text=account.get("label", "?"),
                    values=(TYPE_LABELS.get(account.get("type", ""), account.get("type", "")), "dedicated"),
                )
                self.company_tree_meta[account_item] = {"kind": "account", "account": account, "company": company}

            if shared_accounts:
                fallback_item = self.company_tree.insert(
                    company_item,
                    "end",
                    text="Shared fallback accounts",
                    values=("shared", "available to all companies"),
                    open=False,
                )
                self.company_tree_meta[fallback_item] = {"kind": "shared-group", "company": company}
                for account in shared_accounts:
                    account_item = self.company_tree.insert(
                        fallback_item,
                        "end",
                        text=account.get("label", "?"),
                        values=(TYPE_LABELS.get(account.get("type", ""), account.get("type", "")), "shared"),
                    )
                    self.company_tree_meta[account_item] = {"kind": "account", "account": account, "company": company}

    def _company_assignment_summary(self, company_id: str) -> str:
        assigned = []
        shared = []
        for account in get_accounts():
            company_ids = account.get("company_ids", [])
            if company_id in company_ids:
                assigned.append(account.get("label", "?"))
            elif not company_ids:
                shared.append(account.get("label", "?"))
        parts = []
        if assigned:
            parts.append("Dedicated: " + ", ".join(assigned))
        if shared:
            parts.append("Shared/global fallback: " + ", ".join(shared))
        return "  |  ".join(parts) if parts else "No accounts assigned yet."

    def _account_company_summary(self, account: dict) -> str:
        company_ids = account.get("company_ids", [])
        if not company_ids:
            return "All companies"
        company_map = {c.get("id"): c.get("name", c.get("slug", "?")) for c in get_companies()}
        names = [company_map[cid] for cid in company_ids if cid in company_map]
        return ", ".join(names) if names else "All companies"

    def _on_company_tree_select(self, _event=None):
        item = self.company_tree.focus()
        meta = self.company_tree_meta.get(item)
        if not meta:
            self.company_tree_status_var.set(
                "Tree view shows each company with dedicated accounts, plus one shared fallback branch."
            )
            return
        if meta["kind"] == "company":
            company = meta["company"]
            self.company_tree_status_var.set(
                f"Selected company: {company.get('name', '?')} ({company.get('slug', '')})  |  Copy Codex BAT for Codex/OpenAI adapters, Claude BAT for Claude/Anthropic adapters, Gemma BAT for Gemma 4 local branches, Gemini shim BAT for Gemini-compatible Gemma 4 routing."
            )
        elif meta["kind"] == "account":
            account = meta["account"]
            self.company_tree_status_var.set(
                f"Selected connection: {account.get('label', '?')}  |  {self._account_company_summary(account)}"
            )
        else:
            self.company_tree_status_var.set("Shared fallback accounts are available to every company, and launcher copies are split by Codex, Claude, Gemma, and Gemini shim branches.")

    def _get_selected_company(self):
        item = self.company_tree.focus()
        meta = self.company_tree_meta.get(item)
        if not meta:
            return None
        if meta["kind"] == "company":
            return meta["company"]
        if meta["kind"] in {"account", "shared-group"}:
            return meta.get("company")
        return None

    def _copy_selected_company_bat(self, mode: str):
        company = self._get_selected_company()
        if not company:
            messagebox.showinfo("Select company", "Select a company in the tree first.")
            return
        if mode not in ("codex", "claude", "gemma", "gemini"):
            messagebox.showinfo("Unsupported launcher", "Choose Codex, Claude, Gemma, or Gemini shim.")
            return
        self._copy_path(get_company_launchers(company)[mode])

    def _toggle_bat_group(self, key: str):
        self._bat_group_state[key] = not self._bat_group_state.get(key, key == "__global__")
        self._render_bat_rows()

    def _rename_selected_company(self):
        company = self._get_selected_company()
        if not company:
            messagebox.showinfo("Select company", "Select a company in the tree first.")
            return
        new_name = simpledialog.askstring(
            "Rename company",
            "New company name:",
            initialvalue=company.get("name", ""),
            parent=self,
        )
        if new_name is None:
            return
        self._rename_company(company["id"], new_name)

    def _toggle_selected_company(self):
        company = self._get_selected_company()
        if not company:
            messagebox.showinfo("Select company", "Select a company in the tree first.")
            return
        self._toggle_company(company["id"], not company.get("enabled", True))

    def _delete_selected_company(self):
        company = self._get_selected_company()
        if not company:
            messagebox.showinfo("Select company", "Select a company in the tree first.")
            return
        self._delete_company(company)

    def _add_company(self):
        companies = get_companies()
        base_name = f"Company {len(companies) + 1}"
        company = {
            "id": str(uuid.uuid4()),
            "name": base_name,
            "slug": slugify_company_name(base_name),
            "enabled": True,
            "order": len(companies),
        }
        companies.append(company)
        companies, _ = _normalize_companies(companies)
        save_companies(companies)
        generate_bats()
        self.refresh_accounts()
        self._log(f"Added company: {company['name']}")

    def _rename_company(self, company_id: str, new_name: str):
        companies = get_companies()
        for company in companies:
            if company["id"] == company_id:
                company["name"] = new_name.strip() or company["name"]
                break
        companies, _ = _normalize_companies(companies)
        save_companies(companies)
        generate_bats()
        self.refresh_accounts()

    def _toggle_company(self, company_id: str, enabled: bool):
        companies = get_companies()
        for company in companies:
            if company["id"] == company_id:
                company["enabled"] = enabled
                break
        save_companies(companies)
        generate_bats()
        self.refresh_accounts()
        self._log(f"{'Enabled' if enabled else 'Disabled'} company: {company_id[:8]}")

    def _delete_company(self, company: dict):
        label = company.get("name", "?")
        if not messagebox.askyesno("Delete company", f"Delete '{label}' and remove its account assignments?"):
            return
        companies = [c for c in get_companies() if c["id"] != company["id"]]
        for i, c in enumerate(companies):
            c["order"] = i
        save_companies(companies)
        accounts = get_accounts()
        for account in accounts:
            account["company_ids"] = [cid for cid in account.get("company_ids", []) if cid != company["id"]]
        save_accounts(accounts)
        generate_bats()
        self.refresh_accounts()
        self._log(f"Deleted company: {label}")

    def _edit_account_companies(self, account: dict):
        companies = get_companies()
        if not companies:
            messagebox.showinfo("No companies", "Add a company first, then assign accounts to it.")
            return

        win = tk.Toplevel(self)
        win.title(f"Companies - {account.get('label', '?')}")
        win.geometry("440x420")
        win.configure(bg=BG)
        win.grab_set()

        tk.Label(win, text=f"Assign companies for '{account.get('label', '?')}'",
                 font=("Segoe UI", 11, "bold"), bg=BG, fg=FG
                 ).pack(anchor="w", padx=18, pady=(16, 6))
        tk.Label(win,
                 text="If no company is checked, this account acts as a global fallback for all companies.",
                 font=("Segoe UI", 8), bg=BG, fg=FG_MUTE, justify="left"
                 ).pack(anchor="w", padx=18, pady=(0, 10))

        content = tk.Frame(win, bg=BG)
        content.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        selected_ids = set(account.get("company_ids", []))
        vars_by_company: dict[str, tk.BooleanVar] = {}
        for company in companies:
            var = tk.BooleanVar(value=company["id"] in selected_ids)
            vars_by_company[company["id"]] = var
            tk.Checkbutton(
                content,
                text=f"{company.get('name', company.get('slug', '?'))}  ({company.get('slug', '')})",
                variable=var,
                bg=BG, fg=FG, selectcolor=BG, activebackground=BG,
                font=("Segoe UI", 9)
            ).pack(anchor="w", pady=2)

        btns = tk.Frame(win, bg=BG)
        btns.pack(fill="x", padx=18, pady=(0, 16))

        def save_assignment():
            accounts = get_accounts()
            selected = [cid for cid, var in vars_by_company.items() if var.get()]
            for item in accounts:
                if item["id"] == account["id"]:
                    item["company_ids"] = selected
                    break
            save_accounts(accounts)
            generate_bats()
            self.refresh_accounts()
            if selected:
                self._log(f"Updated companies for '{account.get('label', '?')}'.")
            else:
                self._log(f"'{account.get('label', '?')}' is now a global fallback for all companies.")
            win.destroy()

        tk.Button(btns, text="Save", font=("Segoe UI", 9, "bold"),
                  bg=ACCENT, fg=FG, relief="flat", padx=12, pady=5,
                  cursor="hand2", command=save_assignment
                  ).pack(side="right", padx=4)
        tk.Button(btns, text="Cancel", font=("Segoe UI", 9),
                  bg="#2a2a2a", fg=FG, relief="flat", padx=12, pady=5,
                  cursor="hand2", command=win.destroy
                  ).pack(side="right", padx=4)

    # â”€â”€ Account list â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def refresh_accounts(self):
        current_view = self.canvas.yview()
        companies, companies_changed = _normalize_companies(get_companies())
        if companies_changed:
            save_companies(companies)
        accounts, changed = _normalize_accounts(get_accounts())
        accounts, company_links_changed = _normalize_account_company_ids(accounts, companies)
        cooldowns_changed = _normalize_rate_limits(accounts)
        if changed or company_links_changed:
            save_accounts(accounts)
            generate_bats()
        if companies_changed and not (changed or company_links_changed):
            generate_bats()
        if cooldowns_changed:
            accounts = get_accounts()
        self._render_bat_rows()
        self._render_companies()
        routing_id, routing_live = get_recent_routing_state()
        routing_label = next((a.get("label", "?") for a in accounts if a.get("id") == routing_id), None)
        if routing_label:
            self.routing_status_var.set(
                f"Routing now: {routing_label}" if routing_live else f"Last routed: {routing_label}"
            )
        else:
            self.routing_status_var.set("Routing: idle")

        if routing_id != self._last_seen_routing_id or routing_live != self._last_seen_routing_live:
            if routing_label:
                msg = f"Now routing: {routing_label}" if routing_live else f"Finished routing: {routing_label}"
                self._log(msg)
            self._last_seen_routing_id = routing_id
            self._last_seen_routing_live = routing_live
        for w in self.accounts_frame.winfo_children():
            w.destroy()

        if not accounts:
            tk.Label(self.accounts_frame,
                     text="No accounts yet. Add an account using the buttons above.",
                     font=("Segoe UI", 10), bg=BG, fg=FG_MUTE
                     ).pack(pady=20)
            return

        for i, account in enumerate(accounts):
            self._render_account_row_compact(i, account, len(accounts))

        self._last_accounts_snapshot = self._make_accounts_snapshot(accounts)
        if current_view:
            self.after_idle(lambda v=current_view[0]: self.canvas.yview_moveto(v))

    def _make_accounts_snapshot(self, accounts: list[dict] | None = None):
        accounts = accounts if accounts is not None else get_accounts()
        routing_id, routing_live = get_recent_routing_state()
        snapshot = [routing_id, routing_live]
        for account in accounts:
            account_id = account.get("id", "")
            auth_file = account.get("auth_file", "")
            auth_path = SESSIONS_DIR / auth_file if auth_file else None
            auth_mtime = int(auth_path.stat().st_mtime) if auth_path and auth_path.exists() else 0
            usage_cache = get_usage_from_cache(account_id) or {}
            snapshot.append((
                account_id,
                account.get("type", ""),
                account.get("label", ""),
                bool(account.get("enabled", True)),
                int(account.get("order", 0)),
                auth_file,
                auth_mtime,
                bool(account.get("api_key", "")),
                tuple(sorted(account.get("company_ids", []))),
                get_cooldown_remaining(account_id) // 60,
                usage_cache.get("pct"),
                usage_cache.get("primary_remaining"),
                usage_cache.get("secondary_remaining"),
                int(usage_cache.get("fetched_at", 0) or 0),
            ))
        return tuple(snapshot)

    def _render_account_row(self, idx: int, account: dict, total: int):
        typ        = account.get("type", "codex")
        label      = account.get("label", typ)
        enabled    = account.get("enabled", True)
        type_label = TYPE_LABELS.get(typ, typ)
        badge_bg = {
            "codex": "#5b3fd1",
            "claude": "#c95a2e",
            "openai_api": "#2f6fdb",
            "openrouter_api": "#4c7bd9",
            "anthropic_api": "#7b4a2c",
            "gemma": "#3f8f6b",
        }.get(typ, "#333333")
        status_text, status_color = account_status(account)
        routing_id, routing_live = get_recent_routing_state()
        is_active = account.get("id") == routing_id
        usage_cache = get_usage_from_cache(account.get("id", ""))
        exhausted_by_usage = False
        if typ in ("codex", "claude") and usage_cache:
            try:
                daily_remaining = float(usage_cache.get("primary_remaining")) if usage_cache.get("primary_remaining") is not None else None
            except Exception:
                daily_remaining = None
            try:
                weekly_remaining = float(usage_cache.get("secondary_remaining")) if usage_cache.get("secondary_remaining") is not None else None
            except Exception:
                weekly_remaining = None
            exhausted_by_usage = (
                (daily_remaining is not None and daily_remaining <= 0)
                or (weekly_remaining is not None and weekly_remaining <= 0)
            )
        row_bg = BG_ACTIVE if is_active else ("#252525" if exhausted_by_usage else BG2)
        text_fg = FG_MUTE if exhausted_by_usage and not is_active else FG

        row = tk.Frame(self.accounts_frame, bg=row_bg, pady=4)
        row.pack(fill="x", pady=2, padx=2)

        # â”€â”€ Line 1: badge | name | active | # | arrows | delete â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        line1 = tk.Frame(row, bg=row_bg)
        line1.pack(fill="x", padx=8)

        if typ != "codex":
            tk.Label(line1, text=type_label, font=("Segoe UI", 8, "bold"),
                     bg=badge_bg, fg=FG, padx=6, pady=1
                     ).pack(side="left", padx=(0, 6))

        lbl_var = tk.StringVar(value=label)
        lbl_entry = tk.Entry(line1, textvariable=lbl_var,
                             font=("Segoe UI", 10, "bold"),
                             bg=row_bg, fg=text_fg, insertbackground=text_fg,
                             relief="flat", bd=0, width=24)
        lbl_entry.pack(side="left")
        lbl_entry.bind("<FocusOut>", lambda e, aid=account["id"]: self._rename(aid, lbl_var.get()))
        lbl_entry.bind("<Return>",   lambda e, aid=account["id"]: self._rename(aid, lbl_var.get()))

        if is_active:
            badge_text = "ROUTING" if routing_live else "JUST USED"
            tk.Label(line1, text=badge_text, font=("Segoe UI", 7, "bold"),
                     bg="#7fd3ff", fg="#0d2336", padx=6, pady=1
                     ).pack(side="left", padx=(6, 4))

        enabled_var = tk.BooleanVar(value=enabled)
        tk.Checkbutton(line1, text="active",
                       variable=enabled_var, bg=row_bg, fg=FG_MUTE,
                       selectcolor=row_bg, activebackground=row_bg,
                       font=("Segoe UI", 8),
                       command=lambda aid=account["id"], v=enabled_var: self._toggle(aid, v.get())
                       ).pack(side="left", padx=(6, 2))

        tk.Label(line1, text=f"#{idx+1}", font=("Consolas", 8),
                 bg=row_bg, fg=FG_MUTE).pack(side="left", padx=(0, 8))

        # Arrows + delete on the right of line 1
        tk.Button(line1, text="^", font=("Segoe UI", 9),
                  bg=BG3, fg=FG, relief="flat", padx=5, pady=0,
                  cursor="hand2", state="normal" if idx > 0 else "disabled",
                  command=lambda aid=account["id"]: self._move(aid, -1)
                  ).pack(side="right", padx=2)

        tk.Button(line1, text="v", font=("Segoe UI", 9),
                  bg=BG3, fg=FG, relief="flat", padx=5, pady=0,
                  cursor="hand2", state="normal" if idx < total - 1 else "disabled",
                  command=lambda aid=account["id"]: self._move(aid, 1)
                  ).pack(side="right", padx=2)

        tk.Button(line1, text="Delete", font=("Segoe UI", 8),
                  bg="#3a2020", fg=RED, relief="flat", padx=6, pady=0,
                  cursor="hand2",
                  command=lambda a=account: self._delete(a)
                  ).pack(side="right", padx=2)

        # â”€â”€ Line 2: status | login buttons â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        line2 = tk.Frame(row, bg=row_bg)
        line2.pack(fill="x", padx=8, pady=(2, 0))

        tk.Label(line2, text=status_text, font=("Segoe UI", 8),
                 bg=row_bg, fg=status_color, anchor="w", justify="left"
                 ).pack(side="left")

        tk.Label(line2, text=self._account_company_summary(account),
                 font=("Segoe UI", 8), bg=row_bg, fg=FG_MUTE,
                 anchor="w", justify="left"
                 ).pack(side="left", padx=(10, 0))

        can_prefetch_usage = typ in ("codex", "claude", "openai_api", "openrouter_api", "anthropic_api")
        if usage_cache is None and can_prefetch_usage:
            self._queue_usage_fetch(account)
        if usage_cache and (usage_cache.get("pct") is not None or usage_cache.get("primary_remaining") is not None):
            if typ in ("codex", "claude") and usage_cache.get("primary_remaining") is not None:
                daily = int(usage_cache.get("primary_remaining", 0))
                weekly = int(usage_cache.get("secondary_remaining", 0) or 0)
                usage_btn_text = f"Remaining: {daily}% (5h) / {weekly}% (week)"
                usage_btn_fg = RED if daily <= 0 or weekly <= 0 else BRAND
            else:
                free_pct = 100 - float(usage_cache["pct"])
                usage_btn_text = f"Remaining: {free_pct:.0f}%"
                usage_btn_fg   = RED if free_pct <= 0 else BRAND
        elif typ == "gemma":
            usage_btn_text = "Local model"
            usage_btn_fg = BRAND
        else:
            usage_btn_text = "Loading usage..." if can_prefetch_usage else ("Plan info" if typ == "claude" else "Usage details")
            usage_btn_fg   = FG_MUTE
        tk.Button(line2, text=usage_btn_text,
                  font=("Segoe UI", 8), bg="#2a2a2a", fg=usage_btn_fg,
                  relief="flat", padx=6, pady=1, cursor="hand2",
                  command=lambda a=account: self._show_usage(a)
                  ).pack(side="right", padx=(2, 0))

        if typ in ("codex", "claude", "openai_api", "openrouter_api", "anthropic_api", "gemma"):
            tk.Button(line2, text="Companies",
                      font=("Segoe UI", 8), bg="#2e2e2e", fg=FG,
                      relief="flat", padx=6, pady=1, cursor="hand2",
                      command=lambda a=account: self._edit_account_companies(a)
                      ).pack(side="right", padx=(2, 0))
        if typ in ("codex", "claude"):
            tk.Button(line2, text="Paste token",
                      font=("Segoe UI", 8), bg="#2e2e2e", fg=FG,
                      relief="flat", padx=6, pady=1, cursor="hand2",
                      command=lambda a=account: self._manual_token(a)
                      ).pack(side="right", padx=(2, 0))
            tk.Button(line2, text="Browser login",
                      font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                      relief="flat", padx=6, pady=1, cursor="hand2",
                      command=lambda a=account: self._browser_login(a)
                      ).pack(side="right", padx=2)
        elif typ in ("openai_api", "openrouter_api", "anthropic_api"):
            tk.Button(line2, text="Companies",
                      font=("Segoe UI", 8), bg="#2e2e2e", fg=FG,
                      relief="flat", padx=6, pady=1, cursor="hand2",
                      command=lambda a=account: self._edit_account_companies(a)
                      ).pack(side="right", padx=(2, 0))
            tk.Button(line2, text="Set API key",
                      font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                      relief="flat", padx=6, pady=1, cursor="hand2",
                      command=lambda a=account: self._set_api_key(a)
                      ).pack(side="right")

    def _render_account_row_compact(self, idx: int, account: dict, total: int):
        typ        = account.get("type", "codex")
        label      = account.get("label", typ)
        enabled    = account.get("enabled", True)
        type_label = TYPE_LABELS.get(typ, typ)
        badge_bg = {
            "codex": "#5b3fd1",
            "claude": "#c95a2e",
            "openai_api": "#2f6fdb",
            "openrouter_api": "#4c7bd9",
            "anthropic_api": "#7b4a2c",
            "gemma": "#3f8f6b",
        }.get(typ, "#333333")
        status_text, status_color = account_status(account)
        routing_id, routing_live = get_recent_routing_state()
        is_active = account.get("id") == routing_id
        usage_cache = get_usage_from_cache(account.get("id", ""))
        exhausted_by_usage = False
        if typ in ("codex", "claude") and usage_cache:
            try:
                daily_remaining = float(usage_cache.get("primary_remaining")) if usage_cache.get("primary_remaining") is not None else None
            except Exception:
                daily_remaining = None
            try:
                weekly_remaining = float(usage_cache.get("secondary_remaining")) if usage_cache.get("secondary_remaining") is not None else None
            except Exception:
                weekly_remaining = None
            exhausted_by_usage = (
                (daily_remaining is not None and daily_remaining <= 0)
                or (weekly_remaining is not None and weekly_remaining <= 0)
            )
        row_bg = BG_ACTIVE if is_active else ("#252525" if exhausted_by_usage else BG2)
        text_fg = FG_MUTE if exhausted_by_usage and not is_active else FG

        row = tk.Frame(self.accounts_frame, bg=row_bg, pady=2)
        row.pack(fill="x", pady=2, padx=2)

        line = tk.Frame(row, bg=row_bg)
        line.pack(fill="x", padx=8)

        tk.Label(line, text=type_label, font=("Segoe UI", 8, "bold"),
                 bg=badge_bg, fg=FG, padx=6, pady=1
                 ).pack(side="left", padx=(0, 6))

        lbl_var = tk.StringVar(value=label)
        lbl_entry = tk.Entry(line, textvariable=lbl_var,
                             font=("Segoe UI", 9, "bold"),
                             bg=row_bg, fg=text_fg, insertbackground=text_fg,
                             relief="flat", bd=0, width=18)
        lbl_entry.pack(side="left", padx=(0, 6))
        lbl_entry.bind("<FocusOut>", lambda e, aid=account["id"]: self._rename(aid, lbl_var.get()))
        lbl_entry.bind("<Return>",   lambda e, aid=account["id"]: self._rename(aid, lbl_var.get()))

        if is_active:
            badge_text = "ROUTING" if routing_live else "JUST USED"
            tk.Label(line, text=badge_text, font=("Segoe UI", 7, "bold"),
                     bg="#7fd3ff", fg="#0d2336", padx=6, pady=1
                     ).pack(side="left", padx=(0, 6))

        enabled_var = tk.BooleanVar(value=enabled)
        tk.Checkbutton(line, text="on",
                       variable=enabled_var, bg=row_bg, fg=FG_MUTE,
                       selectcolor=row_bg, activebackground=row_bg,
                       font=("Segoe UI", 8),
                       command=lambda aid=account["id"], v=enabled_var: self._toggle(aid, v.get())
                       ).pack(side="left", padx=(0, 4))

        tk.Label(line, text=f"#{idx+1}", font=("Consolas", 8),
                 bg=row_bg, fg=FG_MUTE).pack(side="left", padx=(0, 8))

        status_inline = " | ".join(part.strip() for part in status_text.splitlines() if part.strip())
        tk.Label(line, text=status_inline, font=("Segoe UI", 8),
                 bg=row_bg, fg=status_color, anchor="w"
                 ).pack(side="left", padx=(0, 8))

        tk.Label(line, text=self._account_company_summary(account),
                 font=("Segoe UI", 8), bg=row_bg, fg=FG_MUTE,
                 anchor="w"
                 ).pack(side="left", padx=(0, 8))

        can_prefetch_usage = typ in ("codex", "claude", "openai_api", "openrouter_api", "anthropic_api")
        if usage_cache is None and can_prefetch_usage:
            self._queue_usage_fetch(account)
        if usage_cache and (usage_cache.get("pct") is not None or usage_cache.get("primary_remaining") is not None):
            if typ in ("codex", "claude") and usage_cache.get("primary_remaining") is not None:
                daily = int(usage_cache.get("primary_remaining", 0))
                weekly = int(usage_cache.get("secondary_remaining", 0) or 0)
                usage_btn_text = f"{daily}%/5h  {weekly}%/wk"
                usage_btn_fg = RED if daily <= 0 or weekly <= 0 else BRAND
            else:
                free_pct = 100 - float(usage_cache["pct"])
                usage_btn_text = f"{free_pct:.0f}% left"
                usage_btn_fg   = RED if free_pct <= 0 else BRAND
        elif typ == "gemma":
            usage_btn_text = "Local model"
            usage_btn_fg = BRAND
        else:
            usage_btn_text = "Loading..." if can_prefetch_usage else ("Plan" if typ == "claude" else "Usage")
            usage_btn_fg   = FG_MUTE
        tk.Button(line, text=usage_btn_text,
                  font=("Segoe UI", 8), bg="#2a2a2a", fg=usage_btn_fg,
                  relief="flat", padx=6, pady=1, cursor="hand2",
                  command=lambda a=account: self._show_usage(a)
                  ).pack(side="right", padx=(2, 0))

        if typ in ("codex", "claude", "openai_api", "openrouter_api", "anthropic_api", "gemma"):
            tk.Button(line, text="Companies",
                      font=("Segoe UI", 8), bg="#2e2e2e", fg=FG,
                      relief="flat", padx=6, pady=1, cursor="hand2",
                      command=lambda a=account: self._edit_account_companies(a)
                      ).pack(side="right", padx=(2, 0))
        if typ in ("codex", "claude"):
            tk.Button(line, text="Token",
                      font=("Segoe UI", 8), bg="#2e2e2e", fg=FG,
                      relief="flat", padx=6, pady=1, cursor="hand2",
                      command=lambda a=account: self._manual_token(a)
                      ).pack(side="right", padx=(2, 0))
            tk.Button(line, text="Login",
                      font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                      relief="flat", padx=6, pady=1, cursor="hand2",
                      command=lambda a=account: self._browser_login(a)
                      ).pack(side="right", padx=2)
        elif typ in ("openai_api", "openrouter_api", "anthropic_api"):
            tk.Button(line, text="API key",
                      font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                      relief="flat", padx=6, pady=1, cursor="hand2",
                      command=lambda a=account: self._set_api_key(a)
                      ).pack(side="right")

        tk.Button(line, text="^", font=("Segoe UI", 8),
                  bg=BG3, fg=FG, relief="flat", padx=5, pady=0,
                  cursor="hand2", state="normal" if idx > 0 else "disabled",
                  command=lambda aid=account["id"]: self._move(aid, -1)
                  ).pack(side="right", padx=2)

        tk.Button(line, text="v", font=("Segoe UI", 8),
                  bg=BG3, fg=FG, relief="flat", padx=5, pady=0,
                  cursor="hand2", state="normal" if idx < total - 1 else "disabled",
                  command=lambda aid=account["id"]: self._move(aid, 1)
                  ).pack(side="right", padx=2)

        tk.Button(line, text="Del", font=("Segoe UI", 8),
                  bg="#3a2020", fg=RED, relief="flat", padx=6, pady=0,
                  cursor="hand2",
                  command=lambda a=account: self._delete(a)
                  ).pack(side="right", padx=2)

    # â”€â”€ Actions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _show_help(self):
        win = tk.Toplevel(self)
        win.title("Subs Rotator - Help")
        win.geometry("760x700")
        win.configure(bg=BG)
        win.grab_set()
        tk.Label(win, text="How Subs Rotator works",
                 font=("Segoe UI", 13, "bold"), bg=BG, fg=FG
                 ).pack(pady=(18, 4), padx=24, anchor="w")
        tabs = ttk.Notebook(win)
        tabs.pack(fill="both", expand=True, padx=18, pady=(4, 12))
        help_tab = tk.Frame(tabs, bg=BG2)
        versions_tab = tk.Frame(tabs, bg=BG2)
        tabs.add(help_tab, text="Help")
        tabs.add(versions_tab, text="Versions")
        sections = [
            ("What it does",
             "Subs Rotator was originally built for Paperclip and now works for broader automation flows.\n"
             "Use it with any target app that can run an external command.\n"
             "Gemma 4 runs locally through Ollama, so Paperclip can run at near-zero cost on VPS.\n"
             "then layer in Codex Subscription, Claude, OpenAI API, OpenRouter API, and Anthropic API as fallback chains.\n"
             "When one account is unavailable, rate-limited, or exhausted, the router switches to the next eligible account."),
            ("How to set up",
             "1. Add accounts using the top buttons (Codex Subscription, Claude, OpenAI API, OpenRouter API, Anthropic API, Gemma 4).\n"
             "2. For Codex Subscription or Claude accounts: click 'Browser login' to authenticate.\n"
             "   For API key accounts: click 'Set API key' and paste your key.\n"
             "3. Use the up/down arrows to set priority order - router tries accounts top to bottom.\n"
             "4. Copy a .bat launcher path into your target app command field.\n"
             "   Use 'Codex only' for Codex flows, 'Claude only' for Claude flows,\n"
             "   'Gemma 4 only' for Gemma local flows, or 'All accounts' for mixed fallback."),
            ("Beyond Paperclip",
             "Subs Rotator works outside Paperclip too.\n\n"
             "Examples:\n"
             "- VS Code tasks: run rotator-codex.bat instead of direct codex command.\n"
             "- Windows Task Scheduler: call rotator-all.bat for unattended jobs.\n"
             "- Custom Python/Node tools: execute rotator-*.bat as the provider command wrapper.\n\n"
             "Requirement: the target app must support running an external command."),
            ("Executable paths",
             "You can optionally set a custom executable path for Codex Subscription, Claude, and Gemma 4 in the\n"
             "'Executables' section near the top of the main window.\n\n"
             "- Gemma 4 uses Ollama (ollama.exe) as its runtime.\n"
             "- If a custom path is set, the router always uses that file.\n"
             "- If the field is empty, the router falls back to auto-detect via PATH and common install locations.\n"
             "- Use 'Browse' to pick an executable and 'Auto' to clear the override."),
            ("Rate limit & cooldown",
             "When a rate limit is detected in the agent output, the router records a cooldown timer for that account\n"
             "and immediately switches to the next account in the list.\n\n"
             "The account status shows the remaining cooldown time in orange.\n"
             "The list auto-refreshes so the countdown stays current."),
            ("Usage tracking & auto-skip",
             "Usage is refreshed automatically every 10 minutes in the background.\n\n"
             "- The left status area shows login state, cooldown, and masked email when available.\n"
             "- The button on the right shows remaining allowance.\n"
             "  Codex Subscription accounts show: 'Remaining: 92% (5h) / 67% (week)'.\n"
             "- Click the usage button to open the detailed usage dialog with progress bars.\n"
             "- Set the threshold in Settings - accounts above that used amount are skipped automatically.\n\n"
             "Note: API-key accounts use provider billing endpoints.\n"
             "Codex Subscription accounts use the ChatGPT usage endpoint.\n"
             "OpenRouter API accounts use OpenRouter base URL routing; usage details are shown via OpenRouter dashboard link.\n"
             "Claude usage may depend on provider headers. If unavailable, plan/tier info is shown."),
            ("Active routing",
             "When the router is currently using an account, that row is highlighted in light blue\n"
             "and marked with a 'ROUTING' badge.\n"
             "The list refreshes every 5 seconds so active routing and cooldown state stay current."),
            ("Companies and launcher scopes",
             "Use the Companies panel to map accounts to specific companies/workspaces.\n"
             "Accounts with no company assignment act as global fallback accounts.\n"
             "Each company gets scoped launchers for all/codex/claude/gemma/gemini routes."),
            ("Backup & restore",
             "Export your accounts and encrypted sessions to a .paperclip-backup file.\n"
             "The backup is protected with a password you choose and works on another PC.\n\n"
             "- Export: Security -> Backup -> Export -> enter password -> save file.\n"
             "- Import: Security -> Backup -> Import -> select file -> enter password.\n\n"
             "Requires: pip install cryptography"),
            ("Security",
             "All sensitive data (session tokens, API keys) is encrypted using Windows DPAPI -\n"
             "the same system Chrome uses to protect saved passwords.\n\n"
             "- Encrypted files copied to another PC cannot be decrypted.\n"
             "- Other Windows users on the same PC cannot read your data.\n"
             "- Data is stored in C:\\Users\\<you>\\.subs-rotator\\ outside OneDrive and outside the app folder.\n"
             "- Plaintext session files are automatically wiped 5 seconds after use.\n"
             "- Expired sessions are detected before routing.\n"
             "- Clipboard is cleared automatically after pasting a token.\n"
             "- Email addresses are masked in logs.\n\n"
             "Note: Any software running as your Windows user account can technically access DPAPI-protected data."),
            ("Account types",
             "Codex Subscription - OpenAI Codex subscription (browser login, no API key needed)\n"
             "Claude            - Anthropic Claude subscription (browser login, no API key needed)\n"
             "OpenAI API        - OpenAI API key (pay-per-use)\n"
             "OpenRouter API    - OpenRouter API key (OpenAI-compatible base URL)\n"
             "Anthropic API     - Anthropic API key (pay-per-use)\n"
             "Gemma 4           - local Ollama branch (model chosen in Executables, can run Paperclip for free)\n"
             "Gemini shim       - Gemma route for Paperclip setups that only expose Gemini local slot\n\n"
             "You can mix and match. Example: 3 Codex Subscription + 1 OpenRouter API + 1 Gemma 4 as fallback.\n"
             "Order them by priority with the up/down arrows."),
        ]
        help_scroll = scrolledtext.ScrolledText(help_tab, font=("Segoe UI", 9),
                                                bg=BG2, fg=FG, relief="flat",
                                                wrap="word", state="normal",
                                                padx=16, pady=8)
        help_scroll.pack(fill="both", expand=True, padx=12, pady=12)
        for title, body in sections:
            help_scroll.insert("end", f"{title}\n", "heading")
            help_scroll.insert("end", f"{body}\n\n", "body")
        help_scroll.tag_config("heading", font=("Segoe UI", 10, "bold"), foreground=BRAND)
        help_scroll.tag_config("body",    font=("Segoe UI", 9),           foreground=FG)
        help_scroll.configure(state="disabled")
        versions_text = (
            "v1.2.1\n"
            "- Added OpenRouter API as a first-class account type.\n"
            "- Added OpenRouter account button, labels, colors, and API-key flow in UI.\n"
            "- Added OpenRouter routing lane via OPENAI_BASE_URL=https://openrouter.ai/api/v1.\n"
            "- Added OpenRouter notes in Help and usage details dialog.\n\n"
            "v1.2.0\n"
            "- Rebranded UI/help for the multi-provider release.\n"
            "- Clarified setup for Codex, Claude, API keys, Gemma, and Gemini shim.\n"
            "- Added explicit Companies/scope guidance in Help.\n"
            "- Improved VPS onboarding guidance through bootstrap flow.\n\n"
            "v1.1.0\n"
            "- Added Gemma 4 as a first-class local lane.\n"
            "- Added a Gemini-compatible Gemma 4 shim for Paperclip setups that only expose Gemini local wiring.\n"
            "- Added one-click Ollama detection, install, and model pull flow.\n"
            "- Added visible model status so Gemma shows found / missing / ready instead of a blank row.\n"
            "- Added collapsible Executables, Settings, and launcher groups to save screen space.\n"
            "- Added visible auto-detected / user set / not found badges for executable paths.\n"
            "- Added launcher copies for company-specific Codex, Claude, Gemma, and Gemini-shim BATs.\n"
            "- Added persistent model selection for Gemma 4 variants.\n"
            "- Improved log visibility during Gemma installs and pulls.\n\n"
            "v1.0\n"
            "- Core Codex and Claude local routing.\n"
            "- Company-aware account assignments.\n"
            "- Global fallback accounts.\n"
            "- Account usage and cooldown tracking.\n"
            "- Basic executable path management.\n"
            "- Paperclip .bat launcher generation.\n"
        )
        versions_scroll = scrolledtext.ScrolledText(versions_tab, font=("Segoe UI", 9),
                                                    bg=BG2, fg=FG, relief="flat",
                                                    wrap="word", state="normal",
                                                    padx=16, pady=8)
        versions_scroll.pack(fill="both", expand=True, padx=12, pady=12)
        versions_scroll.insert("end", versions_text)
        versions_scroll.configure(state="disabled")
        tk.Button(win, text="Close", font=("Segoe UI", 9),
                  bg=ACCENT, fg=FG, relief="flat", padx=12, pady=5,
                  cursor="hand2", command=win.destroy
                  ).pack(pady=(0, 12))
    def _schedule_refresh(self):
        """Auto-refresh account list to update cooldowns and active routing state."""
        accounts = get_accounts()
        snapshot = self._make_accounts_snapshot(accounts)
        if snapshot != self._last_accounts_snapshot:
            self.refresh_accounts()
        self.after(3_000, self._schedule_refresh)

    def _refresh_all_usage_bg(self):
        """Fetch usage for all enabled accounts in background and cache results."""
        def _worker():
            updated = False
            for account in get_accounts():
                if not account.get("enabled", True):
                    continue
                result = _fetch_usage(account)
                if not result.get("error") and (
                    result.get("pct") is not None or result.get("primary_remaining") is not None
                ):
                    save_usage_cache(account["id"], result)
                    updated = True
            if updated:
                self.after(0, self.refresh_accounts)
        threading.Thread(target=_worker, daemon=True).start()

    def _schedule_usage_refresh(self):
        """Auto-fetch usage every 10 minutes."""
        self._refresh_all_usage_bg()
        self.after(10 * 60_000, self._schedule_usage_refresh)

    def _queue_usage_fetch(self, account: dict):
        account_id = account.get("id", "")
        if not account_id or account_id in self._usage_fetch_inflight:
            return

        typ = account.get("type", "")
        if typ == "gemma":
            return
        if typ in ("codex", "claude") and not account.get("auth_file"):
            return
        if typ in ("openai_api", "openrouter_api", "anthropic_api") and not account.get("api_key"):
            return
        if typ not in ("codex", "claude", "openai_api", "openrouter_api", "anthropic_api"):
            return

        self._usage_fetch_inflight.add(account_id)

        def _worker():
            updated = False
            try:
                result = _fetch_usage(account)
                if not result.get("error") and (
                    result.get("pct") is not None or result.get("primary_remaining") is not None
                ):
                    save_usage_cache(account_id, result)
                    updated = True
            finally:
                self._usage_fetch_inflight.discard(account_id)
                if updated:
                    self.after(0, self.refresh_accounts)

        threading.Thread(target=_worker, daemon=True).start()

    def _save_threshold(self):
        try:
            save_setting("usage_limit_pct", int(self.threshold_var.get()))
        except Exception:
            pass

    def _save_retry_minutes(self):
        try:
            value = max(1, int(self.retry_minutes_var.get()))
            self.retry_minutes_var.set(value)
            save_setting("fallback_retry_minutes", value)
        except Exception:
            pass

    def _save_gemma_model(self):
        model = self.gemma_model_var.get().strip() or GEMMA_MODEL_DEFAULT
        self.gemma_model_var.set(model)
        save_setting("gemma_model", model)
        self._log(f"Saved Gemma model: {model}")

    def _resolve_gemma_cmd(self) -> str | None:
        candidates: list[str] = []
        for value in (
            self.gemma_cmd_var.get().strip(),
            get_tool_cmd_path("gemma").strip(),
            get_tool_cmd("gemma").strip(),
            shutil.which("ollama") or "",
            shutil.which("ollama.exe") or "",
            r"C:\Program Files\Ollama\ollama.exe",
            str(Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe"),
        ):
            value = (value or "").strip()
            if value and value not in candidates:
                candidates.append(value)
        for cmd in candidates:
            if Path(cmd).exists():
                return cmd
            found = shutil.which(cmd)
            if found:
                return found
        return None

    def _run_ollama_windows_installer(self) -> subprocess.CompletedProcess:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell"
        cmd = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"irm {OLLAMA_INSTALL_URL} | iex",
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )

    def _wait_for_ollama_cmd(self, timeout_seconds: int = 300, poll_seconds: float = 2.0) -> str | None:
        deadline = datetime.now(timezone.utc).timestamp() + timeout_seconds
        while datetime.now(timezone.utc).timestamp() < deadline:
            cmd = self._resolve_gemma_cmd()
            if cmd:
                return cmd
            import time
            time.sleep(poll_seconds)
        return None

    def _format_process_output(self, result: subprocess.CompletedProcess) -> str:
        text = "\n".join(part for part in [result.stdout or "", result.stderr or ""] if part).strip()
        if not text:
            return "No output captured."
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        if len(lines) > 20:
            lines = lines[-20:]
        return "\n".join(lines)

    def _pull_gemma_model(self, ollama_cmd: str, model: str) -> subprocess.CompletedProcess:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(
            [ollama_cmd, "pull", model],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        output_parts: list[str] = []
        buffer: list[str] = []
        if proc.stdout is not None:
            while True:
                chunk = proc.stdout.read(1)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                output_parts.append(chunk)
                if chunk in ("\r", "\n"):
                    line = "".join(buffer).strip()
                    buffer = []
                    if line:
                        self.after(0, lambda line=line: self._log(f"Gemma pull: {line}"))
                        self.after(0, lambda line=line: self.gemma_install_status_var.set(f"Pulling {model}... {line[:48]}"))
                    continue
                buffer.append(chunk)

        returncode = proc.wait()
        if buffer:
            line = "".join(buffer).strip()
            if line:
                self.after(0, lambda line=line: self._log(f"Gemma pull: {line}"))
                self.after(0, lambda line=line: self.gemma_install_status_var.set(f"Pulling {model}... {line[:48]}"))
        return subprocess.CompletedProcess(
            args=[ollama_cmd, "pull", model],
            returncode=returncode,
            stdout="".join(output_parts),
            stderr="",
        )

    def _install_gemma_model(self):
        if self.gemma_install_busy:
            messagebox.showwarning("Gemma install in progress", "Please wait for the current Gemma install to finish.")
            return

        model = self.gemma_model_var.get().strip() or GEMMA_MODEL_DEFAULT
        self.gemma_model_var.set(model)
        save_setting("gemma_model", model)
        self.gemma_install_busy = True
        self.gemma_install_status_var.set("Starting...")
        self._log(f"Gemma install requested: {model}")
        messagebox.showinfo("Gemma install", f"Starting Gemma setup for {model}. Watch the log for progress.")

        def _worker():
            try:
                ollama_cmd = self._resolve_gemma_cmd()
                if not ollama_cmd:
                    self.after(0, lambda: self._log("Ollama not found. Starting one-click Windows install..."))
                    self.after(0, lambda: self.gemma_install_status_var.set("Installing Ollama..."))
                    install_result = self._run_ollama_windows_installer()
                    if install_result.returncode != 0:
                        raise RuntimeError(
                            "Ollama installation failed.\n\n"
                            f"{self._format_process_output(install_result)}"
                        )
                    self.after(0, lambda: self._log("Ollama installer finished. Waiting for ollama.exe..."))
                    ollama_cmd = self._wait_for_ollama_cmd()
                    if not ollama_cmd:
                        raise RuntimeError(
                            "Ollama installed, but the executable was not found yet.\n"
                            "Restart the router once and try Install / Pull again, or set the Gemma 4 executable path manually."
                        )

                self.after(0, lambda: self.gemma_install_status_var.set(f"Pulling {model}..."))
                self.after(0, lambda path=ollama_cmd: self.gemma_cmd_var.set(path))
                self.after(0, lambda path=ollama_cmd: self._save_tool_path("gemma", path, source="auto"))
                self.after(0, lambda: self._log(f"Starting Gemma model pull: {ollama_cmd} pull {model}"))
                pull_result = self._pull_gemma_model(ollama_cmd, model)
                if pull_result.returncode != 0:
                    raise RuntimeError(
                        "Gemma model pull failed.\n\n"
                        f"{self._format_process_output(pull_result)}"
                    )
                self.after(0, lambda: self._log(f"Gemma model ready: {model}"))
                self.after(0, lambda: self.gemma_install_status_var.set(f"Ready: {model}"))
                self.after(0, lambda: messagebox.showinfo("Gemma ready", f"{model} is installed and ready to use."))
            except Exception as e:
                self.after(0, lambda: self.gemma_install_status_var.set("Failed"))
                self.after(0, lambda msg=str(e): messagebox.showerror("Gemma install failed", msg))
            finally:
                self.gemma_install_busy = False

        threading.Thread(target=_worker, daemon=True).start()

    def _toggle_cleanup(self):
        value = self.cleanup_var.get()
        save_setting("cleanup_on_startup", value)
        self._log(f"Cleanup on startup: {'enabled' if value else 'disabled'}.")

    def _log(self, msg: str):
        self.log.configure(state="normal")
        ts = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {_mask_email(msg)}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_tool_status(self, tool: str, var: tk.StringVar):
        resolved_value, status_text = _tool_path_status(tool)
        if not get_tool_cmd_path(tool).strip() and not var.get().strip():
            var.set(resolved_value)
        status_var = self._tool_status_vars.get(tool)
        if status_var is not None:
            status_var.set(status_text)

    def _copy_bat(self, mode: str):
        self.clipboard_clear()
        self.clipboard_append(str(BAT_FILES[mode]))
        self._log(f"Copied: {BAT_FILES[mode].name} - paste into Paperclip Command field.")

    def _save_tool_path(self, tool: str, value: str, source: str = "manual"):
        set_tool_cmd_path(tool, value)
        save_setting(f"{tool}_cmd_source", source if value.strip() or source == "auto" else "auto")
        self._refresh_tool_status(tool, self.codex_cmd_var if tool == "codex" else self.claude_cmd_var if tool == "claude" else self.gemma_cmd_var)
        self._log(f"{tool.title()} executable: {'auto-detect' if not value.strip() else value.strip()}")

    def _copy_path(self, path: Path):
        self.clipboard_clear()
        self.clipboard_append(str(path))
        self._log(f"Copied: {path.name} - paste into Paperclip Command field.")

    def _browse_tool_path(self, tool: str, var: tk.StringVar):
        selected = filedialog.askopenfilename(
            title=f"Select {tool.title()} executable",
            filetypes=[("Executable files", "*.exe;*.cmd;*.bat"), ("All files", "*.*")],
            parent=self,
        )
        if not selected:
            return
        var.set(selected)
        self._save_tool_path(tool, selected)

    def _reset_tool_path(self, tool: str, var: tk.StringVar):
        var.set(_tool_path_status(tool)[0])
        self._save_tool_path(tool, "", source="auto")

    def _add_account(self, typ: str):
        accounts = get_accounts()
        label_default = {
            "codex":         "Codex Subscription 1",
            "claude":        "Claude account 1",
            "openai_api":    "OpenAI API",
            "openrouter_api":"OpenRouter API",
            "anthropic_api": "Anthropic API",
            "gemma":         "Gemma 4 1",
        }.get(typ, typ)

        account = {
            "id":        str(uuid.uuid4()),
            "type":      typ,
            "label":     label_default,
            "enabled":   True,
            "order":     0,
            "company_ids": [],
            "auth_file": "",
            "api_key":   "",
            "login_email": "",
        }
        accounts.insert(0, account)
        for i, a in enumerate(accounts):
            a["order"] = i
        accounts = _renumber_default_account_labels(accounts)
        save_accounts(accounts)
        generate_bats()
        self.refresh_accounts()
        self.after_idle(lambda: self.canvas.yview_moveto(0))
        self._log(f"Added account: {account['label']}")

    def _rename(self, account_id: str, new_label: str):
        accounts = get_accounts()
        for a in accounts:
            if a["id"] == account_id:
                a["label"] = new_label.strip() or a["label"]
                break
        save_accounts(accounts)

    def _toggle(self, account_id: str, enabled: bool):
        accounts = get_accounts()
        for a in accounts:
            if a["id"] == account_id:
                a["enabled"] = enabled
                break
        save_accounts(accounts)
        generate_bats()
        self._log(f"{'Enabled' if enabled else 'Disabled'}: {account_id[:8]}")

    def _move(self, account_id: str, direction: int):
        accounts = get_accounts()
        idx = next((i for i, a in enumerate(accounts) if a["id"] == account_id), None)
        if idx is None:
            return
        new_idx = idx + direction
        if 0 <= new_idx < len(accounts):
            accounts[idx], accounts[new_idx] = accounts[new_idx], accounts[idx]
            for i, a in enumerate(accounts):
                a["order"] = i
            accounts = _renumber_default_account_labels(accounts)
        save_accounts(accounts)
        generate_bats()
        self.refresh_accounts()

    def _delete(self, account: dict):
        label = account.get("label", "?")
        if not messagebox.askyesno("Delete account", f"Delete '{label}'?"):
            return
        accounts = [a for a in get_accounts() if a["id"] != account["id"]]
        for i, a in enumerate(accounts):
            a["order"] = i
        accounts = _renumber_default_account_labels(accounts)
        save_accounts(accounts)
        generate_bats()
        self.refresh_accounts()
        self._log(f"Deleted: {label}")

    def _browser_login(self, account: dict):
        typ = account.get("type")
        if typ not in ("codex", "claude"):
            messagebox.showinfo("Unsupported account type", "Browser login is only available for Codex Subscription and Claude accounts.")
            return
        if self.login_busy:
            messagebox.showwarning("Login in progress", "Please wait for the current login to complete.")
            return

        if typ == "claude":
            if not messagebox.askokcancel(
                "Claude browser login",
                "Claude works best through a private browser window and the current auth link.\n\n"
                "Recommended flow:\n"
                "1. Open the current Claude auth link in a private/incognito browser tab.\n"
                "2. If Claude is already logged in there, log out first at https://claude.ai/.\n"
                "3. Log in again under the account you want to save.\n"
                "4. Complete the Claude login from the browser/console flow.\n"
                "5. If Claude shows an Authentication Code page, paste that code into the interactive console window opened by Claude.\n\n"
                "Continue?"
            ):
                return
        else:
            if not messagebox.askokcancel(
                "Codex browser login",
                "Before logging in, make sure you are logged OUT of any existing account in your browser "
                "(or use an incognito/private window).\n\n"
                "Otherwise the same account will be saved again.\n\n"
                "Continue?"
            ):
                return

        self.login_busy = True
        label = account["label"]
        self._log(f"Starting browser login for '{label}'...")

        def _launch_login_process(cmd: str, login_args: list[str]):
            suffix = Path(cmd).suffix.lower()
            if suffix in (".cmd", ".bat"):
                argv = ["cmd.exe", "/c", cmd, *login_args]
            else:
                argv = [cmd, *login_args]
            kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "cwd": str(APP_DIR),
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            return subprocess.Popen(argv, **kwargs)

        def worker():
            temp_restore = None
            source_auth = None
            proc = None
            try:
                cmd = get_tool_cmd(typ)
                source_auth = CODEX_AUTH if typ == "codex" else CLAUDE_CREDS
                login_args = ["login"] if typ == "codex" else ["auth", "login", "--claudeai"]

                if source_auth.exists():
                    fd, temp_name = tempfile.mkstemp(prefix=f"paperclip-{typ}-", suffix=".bak")
                    os.close(fd)
                    temp_restore = Path(temp_name)
                    source_auth.replace(temp_restore)

                proc = _launch_login_process(cmd, login_args)
                tool_name = Path(cmd).name
                if typ == "claude":
                    self.after(0, lambda n=tool_name: self._log(
                        f"{n} Claude login opened in a new terminal window. Finish the sign-in there."
                    ))
                    self.after(0, lambda: self._log(
                        "Open the current Claude auth link in a private/incognito tab. If the wrong account is already signed in, "
                        "log out first at https://claude.ai/ and then log in again with the account you want to save."
                    ))
                else:
                    self.after(0, lambda n=tool_name: self._log(
                        f"{n} login opened in a new terminal window. Finish the sign-in there."
                    ))

                deadline = datetime.now(timezone.utc).timestamp() + 600
                while datetime.now(timezone.utc).timestamp() < deadline:
                    if source_auth.exists():
                        break
                    if proc.poll() is not None and not source_auth.exists():
                        import time
                        time.sleep(3)
                        break
                    import time
                    time.sleep(0.5)

                if proc.poll() is None and source_auth.exists():
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass

                if source_auth.exists():
                    session = json.loads(source_auth.read_text(encoding="utf-8"))
                    if typ == "codex":
                        new_email = _get_codex_email_from_session(session)
                        if new_email:
                            for a in get_accounts():
                                if a["id"] == account["id"] or not a.get("auth_file"):
                                    continue
                                existing_path = SESSIONS_DIR / a.get("auth_file", "")
                                if not existing_path.exists():
                                    continue
                                existing = load_encrypted_session(existing_path)
                                if _get_codex_email_from_session(existing) == new_email:
                                    self.after(0, lambda n=new_email, ln=a["label"]: messagebox.showwarning(
                                        "Duplicate account",
                                        f"Warning: {n} is already saved as '{ln}'.\n"
                                        "You may have logged into the same account. "
                                        "Log out in your browser and try again with a different account."
                                    ))
                        dest = get_codex_auth_file(account["id"])
                        save_encrypted_session(dest, session)
                        email_str = f" ({new_email})" if new_email else ""
                    else:
                        dest = get_claude_auth_file(account["id"])
                        save_encrypted_session(dest, session)
                        oauth = _get_claude_oauth(session)
                        plan = oauth.get("subscriptionType") or oauth.get("rateLimitTier") or "Claude"
                        session_email = _get_claude_email_from_session(session)
                        email_str = f" ({plan})"
                    self._update_auth_file(account["id"], dest.name)
                    if typ == "claude" and session_email:
                        self._update_login_email(account["id"], session_email)
                    self.after(0, lambda txt=email_str: self._log(f"'{label}' logged in{txt}. Session encrypted."))
                    if temp_restore and temp_restore.exists():
                        temp_restore.unlink(missing_ok=True)
                else:
                    if temp_restore and temp_restore.exists() and not source_auth.exists():
                        temp_restore.replace(source_auth)
                        temp_restore = None
                    if proc is not None and proc.poll() is None:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    if typ == "claude":
                        reason = (
                            "Claude login did not create a credentials file. "
                            "This often means the opened account does not have Claude Pro or Max, "
                            "or the browser reused the wrong Claude session. "
                            "Open the current auth link again in a private/incognito tab, log out first at https://claude.ai/ "
                            "if needed, and try the correct account again. "
                            "If Claude showed an Authentication Code page, paste that code into the interactive Claude console window."
                        )
                    else:
                        tool = Path(cmd).name
                        reason = (
                            f"{tool} login did not create an auth file. "
                            "Finish the sign-in in the terminal window and try again."
                        )
                    self.after(0, lambda r=reason: self._log(f"Login did not complete: {r}"))
                self.after(0, self.refresh_accounts)
            except Exception as e:
                if temp_restore and temp_restore.exists() and source_auth is not None:
                    if not source_auth.exists():
                        temp_restore.replace(source_auth)
                self.after(0, lambda msg=f"Error: {e}": self._log(msg))
            finally:
                self.login_busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _manual_token(self, account: dict):
        typ = account["type"]
        win = tk.Toplevel(self)
        win.title(f"Paste token - {account['label']}")
        win.geometry("580x400")
        win.configure(bg=BG)
        win.grab_set()

        tk.Label(win, text=f"Paste {'auth.json' if typ == 'codex' else '.credentials.json'} contents for '{account['label']}':",
                 font=("Segoe UI", 10, "bold"), bg=BG, fg=FG
                 ).pack(pady=(16, 4), padx=16, anchor="w")
        tk.Label(win, text="Codex session saved to ~/.codex/   |   Claude session saved to ~/.claude/",
                 font=("Consolas", 8), bg=BG, fg=FG_MUTE
                 ).pack(padx=16, anchor="w", pady=(0, 8))

        text = scrolledtext.ScrolledText(win, height=14, font=("Consolas", 8),
                                          bg=BG3, fg=FG,
                                          insertbackground="white", relief="flat")
        text.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        def save():
            content = text.get("1.0", "end").strip()
            if not content:
                messagebox.showerror("Empty input", "Please paste JSON content.")
                return
            try:
                parsed = json.loads(content)
                if typ not in ("codex", "claude"):
                    messagebox.showerror("Unsupported account type", "Token paste is only available for Codex Subscription and Claude accounts.")
                    return
                dest = get_codex_auth_file(account["id"]) if typ == "codex" else get_claude_auth_file(account["id"])
                save_encrypted_session(dest, parsed)
                self._update_auth_file(account["id"], dest.name)
                self._log(f"'{account['label']}' saved and encrypted.")
                self.refresh_accounts()
                win.destroy()
                self.clipboard_clear()  # wipe token from clipboard
            except json.JSONDecodeError as e:
                messagebox.showerror("JSON error", str(e))

        tk.Button(win, text="Save", font=("Segoe UI", 10, "bold"),
                  bg=ACCENT, fg=FG, relief="flat", padx=14, pady=6,
                  cursor="hand2", command=save).pack(pady=(0, 14))

    def _set_api_key(self, account: dict):
        key = simpledialog.askstring(
            f"API key - {account['label']}",
            "Enter API key:",
            show="*",
            parent=self
        )
        if not key:
            return
        accounts = get_accounts()
        for a in accounts:
            if a["id"] == account["id"]:
                a["api_key"] = key.strip()
                break
        save_accounts(accounts)
        generate_bats()
        self.refresh_accounts()
        self._log(f"API key saved for '{account['label']}'.")

    def _show_usage(self, account: dict):
        win = tk.Toplevel(self)
        win.title(f"Usage - {account['label']}")
        win.geometry("460x430")
        win.configure(bg=BG)
        win.grab_set()
        win.resizable(False, False)

        tk.Label(win, text=f"Usage - {account['label']}",
                 font=("Segoe UI", 11, "bold"), bg=BG, fg=FG
                 ).pack(pady=(16, 6), padx=20, anchor="w")

        if account.get("type") == "gemma":
            runtime = _gemma_runtime_status()
            detail_lbl = tk.Label(
                win,
                text=(
                    "Gemma 4 is a local branch.\n"
                    f"Command: {runtime.get('command')}\n"
                    f"Model: {runtime.get('model')}\n"
                    f"Status: {runtime.get('detail')}\n\n"
                    "No subscription usage is tracked for this account type."
                ),
                font=("Segoe UI", 9),
                bg=BG,
                fg=FG,
                justify="left",
                anchor="w",
                wraplength=420,
            )
            detail_lbl.pack(padx=20, anchor="w")

            btn_row = tk.Frame(win, bg=BG)
            btn_row.pack(side="bottom", pady=(22, 18))
            tk.Button(btn_row, text="Close", font=("Segoe UI", 9),
                      bg=ACCENT, fg=FG, relief="flat", padx=14, pady=5,
                      cursor="hand2", command=win.destroy
                      ).pack(side="left", padx=4)
            return

        detail_lbl = tk.Label(win, text="Fetching...",
                              font=("Segoe UI", 9), bg=BG, fg=FG_MUTE,
                              justify="left", anchor="w", wraplength=420)
        detail_lbl.pack(padx=20, anchor="w")

        bars_frame = tk.Frame(win, bg=BG)

        def _make_bar(parent):
            canvas = tk.Canvas(
                parent,
                width=420,
                height=22,
                bg="#d8d8d8",
                highlightthickness=0,
                bd=0,
            )
            fill = canvas.create_rectangle(0, 0, 0, 22, fill="#7fd3ff", outline="")
            return canvas, fill

        primary_title = tk.Label(bars_frame, text="5h limit", font=("Segoe UI", 8, "bold"), bg=BG, fg=FG)
        primary_bar, primary_fill = _make_bar(bars_frame)
        primary_lbl = tk.Label(bars_frame, text="", font=("Segoe UI", 12, "bold"), bg=BG, fg=BRAND)
        primary_reset_lbl = tk.Label(bars_frame, text="", font=("Segoe UI", 8), bg=BG, fg=FG_MUTE)

        weekly_title = tk.Label(bars_frame, text="Weekly limit", font=("Segoe UI", 8, "bold"), bg=BG, fg=FG)
        weekly_bar, weekly_fill = _make_bar(bars_frame)
        weekly_lbl = tk.Label(bars_frame, text="", font=("Segoe UI", 12, "bold"), bg=BG, fg=BRAND)
        weekly_reset_lbl = tk.Label(bars_frame, text="", font=("Segoe UI", 8), bg=BG, fg=FG_MUTE)

        # btn_row always visible at the bottom
        btn_row = tk.Frame(win, bg=BG)
        btn_row.pack(side="bottom", pady=(22, 18))
        tk.Button(btn_row, text="Close", font=("Segoe UI", 9),
                  bg=ACCENT, fg=FG, relief="flat", padx=14, pady=5,
                  cursor="hand2", command=win.destroy
                  ).pack(side="left", padx=4)
        dash_btn = tk.Button(btn_row, text="Open dashboard",
                             font=("Segoe UI", 9), bg="#2a2a2a", fg=BRAND,
                             relief="flat", padx=10, pady=5, cursor="hand2")

        def _fetch():
            result = _fetch_usage(account)
            def _update():
                if not win.winfo_exists():
                    return
                dashboard = result.get("_dashboard", "")
                show_dashboard = bool(dashboard) and (bool(result.get("error")) or account.get("type") != "codex")
                if show_dashboard:
                    dash_btn.config(command=lambda u=dashboard: webbrowser.open(u))
                    dash_btn.pack(side="left", padx=4)

                detail_lines = result.get("detail_lines", [])
                note = result.get("note", "")
                error = result.get("error")
                if error and not detail_lines:
                    tip = result.get("_tip", "")
                    parts = [error]
                    if tip:
                        parts.append(tip)
                    detail_lbl.config(text="\n\n".join(parts), fg="#cc8800")
                    # hide empty bar/labels for cleaner look
                    bars_frame.pack_forget()
                    return

                used  = result.get("used")
                limit = result.get("limit")
                pct   = result.get("pct")
                free_pct = result.get("free_pct")
                reset = result.get("reset_date", "")
                primary_remaining = result.get("primary_remaining")
                secondary_remaining = result.get("secondary_remaining")
                primary_reset = result.get("primary_reset_date", "")
                secondary_reset = result.get("secondary_reset_date", "")
                summary_text = result.get("summary_text")
                parts = []
                if used is not None or limit is not None:
                    used_str = f"${used:.2f}" if used is not None else "?"
                    limit_str = f"${limit:.2f}" if limit is not None else "?"
                    parts.append(f"{used_str} / {limit_str} included usage")
                if summary_text:
                    parts.append(summary_text)
                parts.extend(detail_lines)
                if error:
                    parts.append(error)
                if note:
                    parts.append(note)
                detail_lbl.config(text="\n".join(parts) if parts else "No usage details available.", fg=FG)

                display_free = free_pct if free_pct is not None else (100 - pct if pct is not None else None)
                if display_free is None and primary_remaining is None and secondary_remaining is None:
                    bars_frame.pack_forget()
                    return

                primary_value = primary_remaining if primary_remaining is not None else display_free

                bars_frame.pack(fill="x", padx=20, pady=(10, 2))

                primary_title.pack(anchor="w")
                primary_bar.pack(fill="x", pady=(2, 0))
                primary_lbl.pack(anchor="center", pady=(2, 0))
                primary_reset_lbl.pack(anchor="center", pady=(0, 10))

                if primary_value is not None:
                    primary_width = int(420 * min(max(primary_value, 0), 100) / 100)
                    primary_bar.coords(primary_fill, 0, 0, primary_width, 22)
                    primary_color = RED if primary_value <= 1 else BRAND
                    primary_lbl.config(text=f"{primary_value:.1f}% free", fg=primary_color)
                    primary_reset_lbl.config(text=f"Resets {primary_reset or reset}")

                weekly_title.pack(anchor="w")
                weekly_bar.pack(fill="x", pady=(2, 0))
                weekly_lbl.pack(anchor="center", pady=(2, 0))
                weekly_reset_lbl.pack(anchor="center")

                if secondary_remaining is not None:
                    weekly_width = int(420 * min(max(secondary_remaining, 0), 100) / 100)
                    weekly_bar.coords(weekly_fill, 0, 0, weekly_width, 22)
                    weekly_color = RED if secondary_remaining <= 1 else BRAND
                    weekly_lbl.config(text=f"{secondary_remaining:.1f}% free", fg=weekly_color)
                    weekly_reset_lbl.config(text=f"Resets {secondary_reset}")
                else:
                    weekly_bar.coords(weekly_fill, 0, 0, 0, 22)
                    weekly_lbl.config(text="No weekly data", fg=FG_MUTE)
                    weekly_reset_lbl.config(text="")
            win.after(0, _update)

        threading.Thread(target=_fetch, daemon=True).start()

    def _export_backup(self):
        if not _CRYPTO_AVAILABLE:
            messagebox.showerror("Missing package",
                                 "Install the cryptography package first:\n  pip install cryptography")
            return
        password = simpledialog.askstring(
            "Export backup", "Enter a password to encrypt the backup.\n"
            "You will need this password to import it on another PC.",
            show="*", parent=self)
        if not password:
            return
        confirm = simpledialog.askstring("Confirm password", "Confirm password:",
                                         show="*", parent=self)
        if password != confirm:
            messagebox.showerror("Password mismatch", "Passwords do not match.")
            return
        save_path = filedialog.asksaveasfilename(
            defaultextension=".paperclip-backup",
            filetypes=[("Paperclip backup", "*.paperclip-backup"), ("All files", "*.*")],
            initialfile="subs-rotator-backup",
            parent=self)
        if not save_path:
            return
        try:
            sessions = {}
            for f in SESSIONS_DIR.iterdir():
                if f.suffix == ".bin":
                    sessions[f.name] = load_encrypted_session(f)
            payload = json.dumps(
                {"version": 1, "config": load_config(), "sessions": sessions},
                ensure_ascii=False
            ).encode("utf-8")
            salt = os.urandom(16)
            kdf  = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                               salt=salt, iterations=480_000)
            key  = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
            encrypted = Fernet(key).encrypt(payload)
            Path(save_path).write_bytes(salt + encrypted)
            self._log(f"Backup exported: {Path(save_path).name}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    def _import_backup(self):
        if not _CRYPTO_AVAILABLE:
            messagebox.showerror("Missing package",
                                 "Install the cryptography package first:\n  pip install cryptography")
            return
        load_path = filedialog.askopenfilename(
            filetypes=[("Paperclip backup", "*.paperclip-backup"), ("All files", "*.*")],
            parent=self)
        if not load_path:
            return
        password = simpledialog.askstring("Import backup", "Enter backup password:",
                                          show="*", parent=self)
        if not password:
            return
        try:
            data      = Path(load_path).read_bytes()
            salt      = data[:16]
            encrypted = data[16:]
            kdf       = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                                    salt=salt, iterations=480_000)
            key       = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
            try:
                payload = Fernet(key).decrypt(encrypted)
            except Exception:
                messagebox.showerror("Import failed", "Wrong password or corrupted backup.")
                return
            backup = json.loads(payload.decode("utf-8"))
            if backup.get("version") != 1:
                messagebox.showerror("Import failed", "Unsupported backup format.")
                return
            if not messagebox.askyesno("Import backup",
                                       "This will overwrite your current accounts and sessions.\nAre you sure?"):
                return
            for fname, session in backup.get("sessions", {}).items():
                save_encrypted_session(SESSIONS_DIR / fname, session)
            save_config(backup["config"])
            self.refresh_accounts()
            self._log(f"Backup imported from {Path(load_path).name}.")
        except Exception as e:
            messagebox.showerror("Import failed", str(e))

    def _update_auth_file(self, account_id: str, filename: str):
        accounts = get_accounts()
        for a in accounts:
            if a["id"] == account_id:
                a["auth_file"] = filename
                break
        save_accounts(accounts)
        generate_bats()

    def _update_login_email(self, account_id: str, login_email: str):
        accounts = get_accounts()
        for a in accounts:
            if a["id"] == account_id:
                a["login_email"] = login_email.strip()
                break
        save_accounts(accounts)
        generate_bats()


if __name__ == "__main__":
    hide_console_window()
    app = RotatorManager()
    app.mainloop()

