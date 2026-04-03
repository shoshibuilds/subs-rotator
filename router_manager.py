"""
Paperclip Router
Multi-account AI switcher for Paperclip — open source
https://github.com/shoshibuilds/paperclip-router
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
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from pathlib import Path
import base64
import uuid
import os
import tempfile
from datetime import datetime, timezone
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
    ROUTER_DIR, SESSIONS_DIR, CONFIG_FILE, LAUNCHERS_DIR,
    CODEX_AUTH, CLAUDE_CREDS,
    CODEX_CMD, CLAUDE_CMD,
)

ROUTER_PY = ROUTER_DIR / "router.py"

BAT_FILES = {
    "codex": LAUNCHERS_DIR / "router-codex.bat",
}

TYPE_LABELS = {
    "codex":         "Codex Subscription",
    "claude":        "Claude",
    "openai_api":    "OpenAI API",
    "anthropic_api": "Anthropic API",
}

# ─── Links ────────────────────────────────────────────────────────────────────

GITHUB_URL = "https://github.com/shoshibuilds/paperclip-router-codex"
BMC_URL    = "https://ko-fi.com/shoshibuilds"
VERSION    = "1.0.0"

# ─── Config helpers ───────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"accounts": []}
    result = load_encrypted_json(CONFIG_FILE)
    return result if result else {"accounts": []}

def save_config(config: dict):
    save_encrypted_json(CONFIG_FILE, config)

def get_accounts() -> list:
    return load_config().get("accounts", [])

def save_accounts(accounts: list):
    cfg = load_config()
    cfg["accounts"] = accounts
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
    return CODEX_CMD if tool == "codex" else CLAUDE_CMD

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

# ─── Auth helpers ─────────────────────────────────────────────────────────────

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
    """Mask email addresses — show first 2 + last 2 chars of each dot-separated word."""
    def _mask_word(w: str) -> str:
        if len(w) <= 3:
            return w[0] + "***"
        return w[:2] + "***" + w[-2:]

    def _mask_match(m: re.Match) -> str:
        local, domain = m.group(0).split("@", 1)
        masked_local = ".".join(_mask_word(p) for p in local.split("."))
        return f"{masked_local}@{domain}"

    return re.sub(r'[\w.+-]+@[\w.-]+\.\w+', _mask_match, text)


def account_status(account: dict) -> tuple[str, str]:
    typ = account.get("type", "")

    if typ in ("openai_api", "anthropic_api"):
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

    # Cooldown check — takes priority over session status
    remaining = get_cooldown_remaining(account.get("id", ""))
    if remaining > 0:
        h, m = remaining // 3600, (remaining % 3600) // 60
        return f"rate limited — {h}h {m:02d}m remaining", "#ff9900"

    try:
        data    = load_encrypted_session(full_path)
        tokens  = data.get("tokens", {})
        tok     = tokens.get("id_token") or tokens.get("access_token", "")
        payload = decode_jwt(tok)
        exp     = payload.get("exp", 0)
        now     = datetime.now(timezone.utc).timestamp()
        email   = payload.get("email", "?")
        lock    = " [enc]" if is_dpapi_available() else ""
        if exp and now > exp:
            if typ == "codex" and _codex_session_has_refresh(data):
                return f"logged in\n{_mask_email(email)}{lock}", BRAND
            return f"expired\n{_mask_email(email)}{lock}", "#ff6666"

        return f"logged in\n{_mask_email(email)}{lock}", BRAND
    except Exception:
        return "logged in", BRAND

# ─── BAT generator ────────────────────────────────────────────────────────────

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
    for mode, path in BAT_FILES.items():
        content = (
            "@echo off\r\n"
            "setlocal\r\n"
            f'python "{ROUTER_PY}" --mode {mode} %*\r\n'
            "endlocal\r\n"
        )
        path.write_text(content, encoding="utf-8")

# ─── Usage fetch ─────────────────────────────────────────────────────────────

def _http_get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))

def _http_post_form(url: str, data: dict, headers: dict | None = None) -> dict:
    encoded = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=encoded, headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))

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
        used  = (usage.get("total_usage") or 0) / 100   # cents → dollars
        pct   = round(used / limit * 100, 1) if limit else None
        ts    = sub.get("access_until")
        reset = (datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y")
                 if ts else None)
        return {"used": used, "limit": limit, "pct": pct, "reset_date": reset}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} — {e.reason}"}
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
        return {"error": f"HTTP {e.code} — {e.reason}"}
    except Exception as e:
        return {"error": str(e)}


_DASHBOARD_URLS = {
    "codex":  "https://chatgpt.com/#settings/usage",
    "claude": "https://claude.ai/settings/billing",
}

def _fetch_subscription_usage(account: dict, typ: str) -> dict:
    if typ == "codex":
        return _fetch_codex_subscription_usage(account)
    return _subscription_usage_snapshot(account, typ)


def _fetch_usage(account: dict) -> dict:
    typ = account.get("type", "")
    if typ == "openai_api":
        return _fetch_openai_usage(account.get("api_key", ""))
    if typ == "anthropic_api":
        return _fetch_anthropic_usage(account.get("api_key", ""))
    if typ in ("codex", "claude"):
        return _fetch_subscription_usage(account, typ)
    return {"error": "Unsupported account type"}


# ─── GUI ──────────────────────────────────────────────────────────────────────

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


class RouterManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Paperclip Router for Codex  v{VERSION}")
        self.geometry("980x1040")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.login_busy = False
        self._usage_fetch_inflight = set()
        self._last_accounts_snapshot = None
        self._last_seen_routing_id = None
        self._last_seen_routing_live = False
        cleanup_on_startup()
        generate_bats()
        self._build_ui()
        self.refresh_accounts()
        self._schedule_refresh()
        self._schedule_usage_refresh()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=18, pady=(14, 0))

        # Logo — full height of header text block (~80px: title + subtitle + version)
        logo_path = ROUTER_DIR / "logo.jpg"
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
        tk.Label(title_row, text="Paperclip Router for Codex",
                 font=("Segoe UI", 16, "bold"), bg=BG, fg=FG
                 ).pack(side="left")
        tk.Label(title_row, text="  by ShoshiBuilds",
                 font=("Segoe UI", 10), bg=BG, fg=FG_MUTE
                 ).pack(side="left", pady=(4, 0))
        tk.Label(left_head,
                 text="Multi-account AI switcher — automatically rotates accounts on rate limit.",
                 font=("Segoe UI", 9), bg=BG, fg=FG_MUTE
                 ).pack(anchor="w")
        enc_status = "Encryption: DPAPI (Windows)" if is_dpapi_available() else "Encryption: unavailable"
        enc_color  = "#00aa44" if is_dpapi_available() else "#ff6666"
        ver_row = tk.Frame(left_head, bg=BG)
        ver_row.pack(anchor="w", pady=(2, 0))
        tk.Label(ver_row, text=f"v{VERSION}",
                 font=("Segoe UI", 8, "bold"), bg=BG, fg=BRAND
                 ).pack(side="left")
        tk.Label(ver_row, text=f"  •  Open source  •  {enc_status}",
                 font=("Segoe UI", 8), bg=BG, fg=BRAND
                 ).pack(side="left")
        self.routing_status_var = tk.StringVar(value="Routing: idle")
        tk.Label(left_head,
                 textvariable=self.routing_status_var,
                 font=("Segoe UI", 9, "bold"), bg=BG, fg="#8fd3ff"
                 ).pack(anchor="w", pady=(4, 0))

        # ── Top-right links ───────────────────────────────────────────────────
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

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=18, pady=10)

        # ── Paperclip BAT section ─────────────────────────────────────────────
        bat_outer = tk.Frame(self, bg="#222222")
        bat_outer.pack(fill="x", padx=18, pady=(0, 12))

        tk.Label(bat_outer, text="  Paperclip — copy Command path for agents:",
                 font=("Segoe UI", 9, "bold"), bg="#222222", fg=FG
                 ).pack(anchor="w", pady=(8, 4), padx=4)

        for label, mode in [("Codex only", "codex")]:
            row = tk.Frame(bat_outer, bg="#222222")
            row.pack(fill="x", padx=8, pady=2)
            tk.Label(row, text=f"{label}:", width=14,
                     font=("Segoe UI", 8, "bold"), bg="#222222", fg=FG_MUTE, anchor="w"
                     ).pack(side="left")
            tk.Label(row, text=str(BAT_FILES[mode]),
                     font=("Consolas", 8), bg="#2a2a2a", fg=FG,
                     anchor="w", padx=6, pady=3
                     ).pack(side="left", fill="x", expand=True)
            tk.Button(row, text="Copy", font=("Segoe UI", 8),
                      bg=ACCENT, fg=FG, relief="flat", padx=8, pady=2,
                      cursor="hand2",
                      command=lambda m=mode: self._copy_bat(m)
                      ).pack(side="left", padx=(4, 0))

        tk.Label(bat_outer,
                 text="  Adapter: Codex (local)  |  Model: Default",
                 font=("Segoe UI", 8), bg="#222222", fg="#555555"
                 ).pack(anchor="w", padx=4, pady=(2, 6))

        tool_path_frame = tk.Frame(self, bg="#1a1a1a")
        tool_path_frame.pack(fill="x", padx=18, pady=(0, 10))

        tk.Label(tool_path_frame, text="  Executables:",
                 font=("Segoe UI", 8, "bold"), bg="#1a1a1a", fg=FG_MUTE
                 ).pack(anchor="w", padx=(4, 0), pady=(6, 4))

        self.codex_cmd_var = tk.StringVar(value=get_tool_cmd_path("codex"))

        self._build_tool_path_row(tool_path_frame, "Codex", self.codex_cmd_var, "codex")

        # ── Security settings ─────────────────────────────────────────────────
        sec_frame = tk.Frame(self, bg="#1a1a1a")
        sec_frame.pack(fill="x", padx=18, pady=(0, 10))

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
                 text="(~/.codex/auth.json)",
                 font=("Segoe UI", 8), bg="#1a1a1a", fg="#444455"
                 ).pack(side="left", padx=(8, 0))

        # ── Backup section ────────────────────────────────────────────────────
        backup_frame = tk.Frame(self, bg="#1a1a1a")
        backup_frame.pack(fill="x", padx=18, pady=(0, 10))

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
                 text="Password-encrypted — restores all accounts on another PC",
                 font=("Segoe UI", 8), bg="#1a1a1a", fg="#444455"
                 ).pack(side="left", padx=(10, 0))

        # ── Usage threshold ───────────────────────────────────────────────────
        usage_frame = tk.Frame(self, bg="#1a1a1a")
        usage_frame.pack(fill="x", padx=18, pady=(0, 10))

        tk.Label(usage_frame, text="  Auto-skip account when usage ≥",
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

        retry_frame = tk.Frame(self, bg="#1a1a1a")
        retry_frame.pack(fill="x", padx=18, pady=(0, 10))

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

        # ── Add account bar ───────────────────────────────────────────────────
        add_bar = tk.Frame(self, bg=BG)
        add_bar.pack(fill="x", padx=18, pady=(0, 6))

        tk.Label(add_bar, text="Accounts:", font=("Segoe UI", 10, "bold"),
                 bg=BG, fg=FG).pack(side="left")

        for label, typ in [
            ("+ Codex Subscription", "codex"),
            ("+ OpenAI API", "openai_api"),
            ("+ Anthropic API", "anthropic_api"),
        ]:
            tk.Button(add_bar, text=label, font=("Segoe UI", 8),
                      bg=ACCENT, fg=FG, relief="flat", padx=8, pady=3,
                      cursor="hand2",
                      command=lambda t=typ: self._add_account(t)
                      ).pack(side="right", padx=3)

        # ── Scrollable account list ───────────────────────────────────────────
        canvas_frame = tk.Frame(self, bg=BG)
        canvas_frame.pack(fill="both", expand=True, padx=18, pady=(0, 8))

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

        # ── Footer ────────────────────────────────────────────────────────────
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=18, pady=6)

        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=18, pady=(0, 4))
        tk.Label(footer,
                 text="Paperclip Router is free and open source. If it saves you time, consider supporting on Ko-fi.",
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

        # ── Log ───────────────────────────────────────────────────────────────
        tk.Label(self, text="Log:", font=("Segoe UI", 8, "bold"),
                 bg=BG, fg=FG_MUTE).pack(anchor="w", padx=18)
        self.log = scrolledtext.ScrolledText(self, height=8,
                                              font=("Consolas", 8),
                                              bg=BG3, fg=FG,
                                              insertbackground="white",
                                              relief="flat", state="disabled")
        self.log.pack(fill="x", padx=18, pady=(2, 14))

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

    # ── Account list ──────────────────────────────────────────────────────────

    def refresh_accounts(self):
        current_view = self.canvas.yview()
        accounts, changed = _normalize_accounts(get_accounts())
        accounts = [a for a in accounts if a.get("type") != "claude"]
        cooldowns_changed = _normalize_rate_limits(accounts)
        if changed:
            save_accounts(accounts)
            generate_bats()
        if cooldowns_changed:
            accounts = [a for a in get_accounts() if a.get("type") != "claude"]
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
            self._render_account_row(i, account, len(accounts))

        self._last_accounts_snapshot = self._make_accounts_snapshot(accounts)
        if current_view:
            self.after_idle(lambda v=current_view[0]: self.canvas.yview_moveto(v))

    def _make_accounts_snapshot(self, accounts: list[dict] | None = None):
        accounts = accounts if accounts is not None else get_accounts()
        accounts = [a for a in accounts if a.get("type") != "claude"]
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
            "anthropic_api": "#7b4a2c",
        }.get(typ, "#333333")
        status_text, status_color = account_status(account)
        routing_id, routing_live = get_recent_routing_state()
        is_active = account.get("id") == routing_id
        usage_cache = get_usage_from_cache(account.get("id", ""))
        exhausted_by_usage = False
        if typ == "codex" and usage_cache:
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

        # ── Line 1: badge | name | active | # | arrows | delete ──────────────
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
        tk.Button(line1, text="↑", font=("Segoe UI", 9),
                  bg=BG3, fg=FG, relief="flat", padx=5, pady=0,
                  cursor="hand2", state="normal" if idx > 0 else "disabled",
                  command=lambda aid=account["id"]: self._move(aid, -1)
                  ).pack(side="right", padx=2)

        tk.Button(line1, text="↓", font=("Segoe UI", 9),
                  bg=BG3, fg=FG, relief="flat", padx=5, pady=0,
                  cursor="hand2", state="normal" if idx < total - 1 else "disabled",
                  command=lambda aid=account["id"]: self._move(aid, 1)
                  ).pack(side="right", padx=2)

        tk.Button(line1, text="Delete", font=("Segoe UI", 8),
                  bg="#3a2020", fg=RED, relief="flat", padx=6, pady=0,
                  cursor="hand2",
                  command=lambda a=account: self._delete(a)
                  ).pack(side="right", padx=2)

        # ── Line 2: status | login buttons ────────────────────────────────────
        line2 = tk.Frame(row, bg=row_bg)
        line2.pack(fill="x", padx=8, pady=(2, 0))

        tk.Label(line2, text=status_text, font=("Segoe UI", 8),
                 bg=row_bg, fg=status_color, anchor="w", justify="left"
                 ).pack(side="left")

        can_prefetch_usage = typ in ("codex", "openai_api", "anthropic_api")
        if usage_cache is None and can_prefetch_usage:
            self._queue_usage_fetch(account)
        if usage_cache and (usage_cache.get("pct") is not None or usage_cache.get("primary_remaining") is not None):
            if typ == "codex" and usage_cache.get("primary_remaining") is not None:
                daily = int(usage_cache.get("primary_remaining", 0))
                weekly = int(usage_cache.get("secondary_remaining", 0) or 0)
                usage_btn_text = f"Remaining: {daily}% (5h) / {weekly}% (week)"
                usage_btn_fg = RED if daily <= 0 or weekly <= 0 else BRAND
            else:
                free_pct = 100 - float(usage_cache["pct"])
                usage_btn_text = f"Remaining: {free_pct:.0f}%"
                usage_btn_fg   = RED if free_pct <= 0 else BRAND
        else:
            usage_btn_text = "Loading usage..." if can_prefetch_usage else "Usage details"
            usage_btn_fg   = FG_MUTE
        tk.Button(line2, text=usage_btn_text,
                  font=("Segoe UI", 8), bg="#2a2a2a", fg=usage_btn_fg,
                  relief="flat", padx=6, pady=1, cursor="hand2",
                  command=lambda a=account: self._show_usage(a)
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
        else:
            tk.Button(line2, text="Set API key",
                      font=("Segoe UI", 8), bg=ACCENT, fg=FG,
                      relief="flat", padx=6, pady=1, cursor="hand2",
                      command=lambda a=account: self._set_api_key(a)
                      ).pack(side="right")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _show_help(self):
        win = tk.Toplevel(self)
        win.title("Paperclip Router — Help")
        win.geometry("640x620")
        win.configure(bg=BG)
        win.grab_set()

        tk.Label(win, text="How Paperclip Router for Codex works",
                 font=("Segoe UI", 13, "bold"), bg=BG, fg=FG
                 ).pack(pady=(18, 4), padx=24, anchor="w")

        sections = [
            ("What it does",
             "Paperclip Router for Codex manages multiple Codex Subscription accounts and API-key accounts.\n"
             "When one account hits a rate limit or approaches its usage limit, the router\n"
             "automatically switches to the next available account — so your agents keep running\n"
             "without interruption."),

            ("How to set up",
             "1. Add accounts using the buttons at the top (Codex Subscription, OpenAI API, Anthropic API).\n"
             "2. For Codex Subscription accounts: click 'Browser login' to authenticate.\n"
             "   For API key accounts: click 'Set API key' and paste your key.\n"
             "3. Use the ↑ ↓ arrows to set the priority order — the router tries accounts top to bottom.\n"
             "4. Copy the .bat file path and paste it into Paperclip → Agent → Command field.\n"
             "   Use the 'Codex only' bat for Codex agents."),

            ("Executable paths",
             "You can optionally set a custom executable path for Codex Subscription in the\n"
             "'Executables' section near the top of the main window.\n\n"
             "• If a custom path is set, the router always uses that file.\n"
             "• If the field is empty, the router falls back to auto-detect via PATH and common\n"
             "  install locations.\n"
             "• Use 'Browse' to pick an executable and 'Auto' to clear the override."),

            ("Rate limit & cooldown",
             "When a rate limit is detected in the agent output, the router records a cooldown\n"
             "timer for that account (typically 5 hours for OpenAI/Anthropic subscription plans)\n"
             "and immediately switches to the next account in the list.\n\n"
             "The account status shows the remaining cooldown time in orange.\n"
             "The list auto-refreshes every 60 seconds so the countdown stays current."),

            ("Usage tracking & auto-skip",
             "Usage is refreshed automatically every 10 minutes in the background.\n\n"
             "• The left status area shows login state, cooldown, and the masked email.\n"
             "• The button on the right shows remaining allowance.\n"
             "  Codex Subscription accounts show: 'Remaining: 92% (5h) / 67% (week)'.\n"
             "• Click the usage button to open the detailed usage dialog with a progress bar.\n"
             "• Set the threshold (default 90%) - accounts above that used amount are skipped\n"
             "  automatically before they hit the hard limit.\n\n"
             "Note: API-key accounts use provider billing endpoints.\n"
             "Codex Subscription accounts use the ChatGPT usage endpoint."),

            ("Active routing",
             "When the router is currently using an account, that row is highlighted in light blue\n"
             "and marked with a 'ROUTING' badge.\n"
             "The list refreshes every 5 seconds so active routing and cooldown state stay current."),

            ("Backup & restore",
             "Export your accounts and encrypted sessions to a .paperclip-backup file.\n"
             "The backup is protected with a password you choose — it works on any PC.\n\n"
             "• Export: Security → Backup → Export — enter password → save file.\n"
             "• Import: Security → Backup → Import — select file → enter password.\n\n"
             "Requires: pip install cryptography"),

            ("Security",
             "All sensitive data (session tokens, API keys) is encrypted using Windows DPAPI —\n"
             "the same system Chrome uses to protect saved passwords.\n\n"
             "• Encrypted files copied to another PC cannot be decrypted.\n"
             "• Other Windows users on the same PC cannot read your data.\n"
             "• Data is stored in C:\\Users\\<you>\\.paperclip-router\\ — outside OneDrive,\n"
             "  outside the program folder, never synced to the cloud.\n"
             "• Plaintext session files are automatically wiped 5 seconds after use.\n"
             "• Expired sessions are detected before routing — expired accounts are skipped.\n"
             "• Clipboard is cleared automatically after pasting a token.\n"
             "• Email addresses are masked in all logs: on***ej.so***ik@gmail.com\n\n"
             "Note: Any software running as your Windows user account can technically access\n"
             "DPAPI-protected data. Keep your PC and Windows account secure."),

            ("Account types",
             "Codex Subscription — OpenAI Codex subscription (browser login, no API key needed)\n"
             "Claude         — Anthropic Claude subscription (browser login, no API key needed)\n"
             "OpenAI API     — OpenAI API key (pay-per-use)\n"
             "Anthropic API  — Anthropic API key (pay-per-use)\n\n"
             "You can mix and match — e.g. 3 Codex Subscription accounts + 1 OpenAI API key\n"
             "as a final fallback. Order them by priority using the ↑ ↓ arrows."),
        ]

        sections = [
            (
                title,
                "\n".join(
                    line for line in body.splitlines()
                    if "Anthropic Claude subscription" not in line
                ),
            )
            for title, body in sections
        ]

        scroll = scrolledtext.ScrolledText(win, font=("Segoe UI", 9),
                                            bg=BG2, fg=FG, relief="flat",
                                            wrap="word", state="normal",
                                            padx=16, pady=8)
        scroll.pack(fill="both", expand=True, padx=18, pady=(4, 8))

        for title, body in sections:
            scroll.insert("end", f"{title}\n", "heading")
            scroll.insert("end", f"{body}\n\n", "body")

        scroll.tag_config("heading", font=("Segoe UI", 10, "bold"), foreground=BRAND)
        scroll.tag_config("body",    font=("Segoe UI", 9),           foreground=FG)
        scroll.configure(state="disabled")

        tk.Button(win, text="Close", font=("Segoe UI", 9),
                  bg=ACCENT, fg=FG, relief="flat", padx=14, pady=5,
                  cursor="hand2", command=win.destroy
                  ).pack(pady=(0, 16))

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
        if typ == "codex" and not account.get("auth_file"):
            return
        if typ in ("openai_api", "anthropic_api") and not account.get("api_key"):
            return
        if typ not in ("codex", "openai_api", "anthropic_api"):
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

    def _copy_bat(self, mode: str):
        self.clipboard_clear()
        self.clipboard_append(str(BAT_FILES[mode]))
        self._log(f"Copied: {BAT_FILES[mode].name} — paste into Paperclip Command field.")

    def _save_tool_path(self, tool: str, value: str):
        set_tool_cmd_path(tool, value)
        self._log(f"{tool.title()} executable: {'auto-detect' if not value.strip() else value.strip()}")

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
        var.set("")
        self._save_tool_path(tool, "")

    def _add_account(self, typ: str):
        accounts = get_accounts()
        label_default = {
            "codex":         "Codex Subscription 1",
            "claude":        "Claude account 1",
            "openai_api":    "OpenAI API",
            "anthropic_api": "Anthropic API",
        }.get(typ, typ)

        account = {
            "id":        str(uuid.uuid4()),
            "type":      typ,
            "label":     label_default,
            "enabled":   True,
            "order":     0,
            "auth_file": "",
            "api_key":   "",
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
        if account.get("type") != "codex":
            messagebox.showinfo("Unsupported account type", "This build currently supports Browser login only for Codex Subscription accounts.")
            return
        if self.login_busy:
            messagebox.showwarning("Login in progress", "Please wait for the current login to complete.")
            return

        # Warn user to log out first so they can log into a different account
        if not messagebox.askokcancel(
            "Browser login",
            "Before logging in, make sure you are logged OUT of any existing account in your browser "
            "(or use an incognito/private window).\n\n"
            "Otherwise the same account will be saved again.\n\n"
            "Continue?"
        ):
            return

        self.login_busy = True
        typ   = account["type"]
        label = account["label"]
        self._log(f"Starting browser login for '{label}'...")
        def _get_email_from_session(session: dict) -> str:
            try:
                tokens = session.get("tokens", {})
                tok    = tokens.get("id_token") or tokens.get("access_token", "")
                payload = decode_jwt(tok)
                return payload.get("email", "")
            except Exception:
                return ""


        def _launch_login_process(cmd: str):
            suffix = Path(cmd).suffix.lower()
            login_args = ["login"]
            if suffix in (".cmd", ".bat"):
                argv = ["cmd.exe", "/c", cmd, *login_args]
            else:
                argv = [cmd, *login_args]
            kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "cwd": str(ROUTER_DIR),
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            return subprocess.Popen(argv, **kwargs)

        def _probe_login_failure(cmd: str) -> str:
            try:
                probe_args = [cmd, "login"]
                probe = subprocess.run(
                    probe_args,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                )
                output = "\n".join(
                    p.strip() for p in [probe.stdout or "", probe.stderr or ""] if p.strip()
                )
                if not output:
                    return ""
                low = output.lower()
                if "hit your limit" in low or "rate limit" in low:
                    return output
                if "already logged in" in low:
                    return output
            except Exception:
                return ""
            return ""

        def worker():
            temp_restore = None
            try:
                cmd = get_tool_cmd("codex")
                source_auth = CODEX_AUTH

                # Force a fresh browser login instead of silently reusing an old plaintext session.
                if source_auth.exists():
                    fd, temp_name = tempfile.mkstemp(prefix=f"paperclip-{typ}-", suffix=".bak")
                    os.close(fd)
                    temp_restore = Path(temp_name)
                    source_auth.replace(temp_restore)

                proc = _launch_login_process(cmd)
                tool_name = Path(cmd).name
                self.after(0, lambda n=tool_name: self._log(
                    f"{n} login opened in a new terminal window. Finish the sign-in there."
                ))

                deadline = datetime.now(timezone.utc).timestamp() + 300
                while datetime.now(timezone.utc).timestamp() < deadline:
                    if source_auth.exists():
                        break
                    if proc.poll() is not None and not source_auth.exists():
                        # Give the CLI a short grace period to flush the credentials file
                        # after the OAuth callback completes.
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

                if typ == "codex" and CODEX_AUTH.exists():
                    session = json.loads(CODEX_AUTH.read_text(encoding="utf-8"))
                    new_email = _get_email_from_session(session)

                    # Check for duplicate email across other accounts
                    if new_email:
                        for a in get_accounts():
                            if a["id"] == account["id"] or not a.get("auth_file"):
                                continue
                            existing = load_encrypted_session(SESSIONS_DIR / a["auth_file"]) if (SESSIONS_DIR / a.get("auth_file","")).exists() else {}
                            if _get_email_from_session(existing) == new_email:
                                self.after(0, lambda n=new_email, ln=a["label"]: messagebox.showwarning(
                                    "Duplicate account",
                                    f"Warning: {n} is already saved as '{ln}'.\n"
                                    "You may have logged into the same account. "
                                    "Log out in your browser and try again with a different account."
                                ))

                    dest = get_codex_auth_file(account["id"])
                    save_encrypted_session(dest, session)
                    self._update_auth_file(account["id"], dest.name)
                    email_str = f" ({new_email})" if new_email else ""
                    self.after(0, lambda: self._log(f"'{label}' logged in{email_str}. Session encrypted."))
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
                    tool = Path(cmd).name
                    reason = (
                        f"{tool} login did not create an auth file. "
                        "Finish the sign-in in the terminal window and try again."
                    )
                    self.after(0, lambda r=reason: self._log(f"Login did not complete: {r}"))
                self.after(0, self.refresh_accounts)
            except Exception as e:
                if temp_restore and temp_restore.exists():
                    source_auth = CODEX_AUTH
                    if not source_auth.exists():
                        temp_restore.replace(source_auth)
                self.after(0, lambda: self._log(f"Error: {e}"))
            finally:
                self.login_busy = False

        threading.Thread(target=worker, daemon=True).start()

    def _manual_token(self, account: dict):
        win = tk.Toplevel(self)
        win.title(f"Paste token — {account['label']}")
        win.geometry("580x400")
        win.configure(bg=BG)
        win.grab_set()

        tk.Label(win, text=f"Paste auth.json contents for '{account['label']}':",
                 font=("Segoe UI", 10, "bold"), bg=BG, fg=FG
                 ).pack(pady=(16, 4), padx=16, anchor="w")
        tk.Label(win, text="Codex session saved to ~/.codex/",
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
                typ  = account["type"]
                if typ != "codex":
                    messagebox.showerror("Unsupported account type", "This build currently supports token paste only for Codex Subscription accounts.")
                    return
                dest = get_codex_auth_file(account["id"])
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
            f"API key — {account['label']}",
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
        win.title(f"Usage — {account['label']}")
        win.geometry("460x430")
        win.configure(bg=BG)
        win.grab_set()
        win.resizable(False, False)

        tk.Label(win, text=f"Usage — {account['label']}",
                 font=("Segoe UI", 11, "bold"), bg=BG, fg=FG
                 ).pack(pady=(16, 6), padx=20, anchor="w")

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
        dash_btn = tk.Button(btn_row, text="Open dashboard ↗",
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
            initialfile="paperclip-router-backup",
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


if __name__ == "__main__":
    app = RouterManager()
    app.mainloop()
