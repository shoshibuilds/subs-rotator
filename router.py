"""
Universal AI Router
Called from .bat files: python router.py --mode [codex|claude|all] [args...]
"""

import sys
import re
import os
import base64
import json
import subprocess
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from crypto import decrypt_session_to_file, load_encrypted_json, save_encrypted_json
from paths import (
    SESSIONS_DIR, CONFIG_FILE,
    CODEX_AUTH, CLAUDE_CREDS,
    CODEX_CMD, CLAUDE_CMD,
)

# ─── Rate limit signals ───────────────────────────────────────────────────────

RATE_LIMIT_SIGNALS = [
    "rate limit", "rate_limit", "429", "too many requests",
    "usage limit", "quota exceeded", "you've hit your limit",
    "overloaded", "insufficient_quota",
]

DEFAULT_COOLDOWN_SECONDS = 60  # 1 minute fallback when we cannot parse a real reset time
UNSUPPORTED_CHATGPT_CODEX_MODELS = {"gpt-5-mini"}

def parse_retry_seconds(line: str, fallback_seconds: int = DEFAULT_COOLDOWN_SECONDS) -> tuple[int, bool]:
    """Extract cooldown duration in seconds from a rate-limit message.
    Returns (seconds, parsed_exactly).
    """
    low = line.lower()

    # "5h 0m" / "5 hours 30 min"
    m = re.search(r"(\d+)\s*h(?:ours?)?[\s,]*(\d+)?\s*m(?:in)?", low)
    if m:
        return int(m.group(1)) * 3600 + (int(m.group(2)) if m.group(2) else 0) * 60, True

    # "5 hours"
    m = re.search(r"(\d+)\s*hour", low)
    if m:
        return int(m.group(1)) * 3600, True

    # "10 minutes" / "15 mins" / "1 minute"
    m = re.search(r"(\d+)\s*m(?:in(?:ute)?s?)\b", low)
    if m:
        return int(m.group(1)) * 60, True

    # "retry after / try again in / wait N seconds"
    m = re.search(r"(?:retry.{0,20}?|try again in\s*|wait\s*)(\d+)\s*second", low)
    if m:
        return int(m.group(1)), True

    # "retry after / try again in / wait N minutes"
    m = re.search(r"(?:retry.{0,20}?|try again in\s*|wait\s*)(\d+)\s*m(?:in(?:ute)?s?)\b", low)
    if m:
        return int(m.group(1)) * 60, True

    # "resets 1pm" / "resets 4:30 pm"
    m = re.search(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", low)
    if m:
        hour = int(m.group(1)) % 12
        minute = int(m.group(2) or 0)
        meridiem = m.group(3)
        if meridiem == "pm":
            hour += 12
        now = datetime.now(timezone.utc).astimezone()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target = target + timedelta(days=1)
        return max(60, int((target - now).total_seconds())), True

    return fallback_seconds, False

# ─── Config helpers ───────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"accounts": []}
    return load_encrypted_json(CONFIG_FILE) or {"accounts": []}

def save_config(cfg: dict):
    save_encrypted_json(CONFIG_FILE, cfg)

def set_active_account(account_id: str | None):
    cfg = load_config()
    if account_id:
        cfg["active_account_id"] = account_id
        cfg["active_account_started_at"] = datetime.now(timezone.utc).timestamp()
    else:
        previous = cfg.get("active_account_id")
        if previous:
            cfg["last_active_account_id"] = previous
            cfg["last_active_account_finished_at"] = datetime.now(timezone.utc).timestamp()
        cfg.pop("active_account_id", None)
        cfg.pop("active_account_started_at", None)
    save_config(cfg)

def set_rate_limited(account_id: str, cooldown_seconds: int):
    cfg = load_config()
    cfg.setdefault("rate_limits", {})[account_id] = (
        datetime.now(timezone.utc).timestamp() + cooldown_seconds
    )
    save_config(cfg)

def get_cooldown_remaining(account_id: str, cfg: dict) -> int:
    until = cfg.get("rate_limits", {}).get(account_id, 0)
    return max(0, int(until - datetime.now(timezone.utc).timestamp()))

def get_cached_usage_pct(account_id: str, cfg: dict, max_age_min: int = 20) -> float | None:
    """Return cached usage % if fresh, else None."""
    cache = cfg.get("usage_cache", {}).get(account_id)
    if not cache or cache.get("pct") is None:
        return None
    age = datetime.now(timezone.utc).timestamp() - cache.get("fetched_at", 0)
    if age > max_age_min * 60:
        return None
    return cache["pct"]


def get_cached_usage_limits(account_id: str, cfg: dict, max_age_min: int = 20) -> tuple[float | None, float | None]:
    """Return fresh cached (5h remaining %, weekly remaining %) if available."""
    cache = cfg.get("usage_cache", {}).get(account_id)
    if not cache:
        return None, None
    age = datetime.now(timezone.utc).timestamp() - cache.get("fetched_at", 0)
    if age > max_age_min * 60:
        return None, None
    primary = cache.get("primary_remaining")
    secondary = cache.get("secondary_remaining")
    try:
        primary = float(primary) if primary is not None else None
    except Exception:
        primary = None
    try:
        secondary = float(secondary) if secondary is not None else None
    except Exception:
        secondary = None
    return primary, secondary

def get_tool_cmd(tool: str, cfg: dict) -> str:
    settings = cfg.get("settings", {})
    custom = settings.get(f"{tool}_cmd_path", "").strip()
    if custom:
        return custom
    return CODEX_CMD if tool == "codex" else CLAUDE_CMD

def _extract_model_arg(args: list[str]) -> tuple[str | None, int | None, int | None]:
    for i, arg in enumerate(args):
        if arg == "--model" and i + 1 < len(args):
            return args[i + 1], i, i + 1
        if arg.startswith("--model="):
            return arg.split("=", 1)[1], i, None
        if arg == "-m" and i + 1 < len(args):
            return args[i + 1], i, i + 1
    return None, None, None

def _normalize_args_for_account(account: dict, args: list[str]) -> list[str]:
    normalized = list(args)
    model, idx, value_idx = _extract_model_arg(normalized)
    if not model:
        return normalized

    typ = account.get("type", "")
    if typ == "codex" and model in UNSUPPORTED_CHATGPT_CODEX_MODELS:
        if value_idx is None and idx is not None:
            normalized.pop(idx)
        elif idx is not None and value_idx is not None:
            del normalized[idx:value_idx + 1]
        print(
            f"[router] {account.get('label')}: model '{model}' is not supported for Codex ChatGPT accounts, using CLI default instead.",
            flush=True,
        )
    return normalized

def cleanup_on_startup():
    """Delete plaintext session files if the user opted in."""
    cfg = load_config()
    if not cfg.get("settings", {}).get("cleanup_on_startup", False):
        return
    for path in (CODEX_AUTH, CLAUDE_CREDS):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

# ─── Process runner ───────────────────────────────────────────────────────────

def run_process(cmd: list, env: dict | None = None, fallback_cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS) -> tuple[bool, int]:
    """
    Stream process output.
    Returns (rate_limited, cooldown_seconds).
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        for line in proc.stdout:
            try:
                sys.stdout.write(line)
            except UnicodeEncodeError:
                data = line.encode(sys.stdout.encoding or "utf-8", errors="replace")
                if hasattr(sys.stdout, "buffer"):
                    sys.stdout.buffer.write(data)
                else:
                    sys.stdout.write(data.decode(sys.stdout.encoding or "utf-8", errors="replace"))
            sys.stdout.flush()
            if any(s in line.lower() for s in RATE_LIMIT_SIGNALS):
                cooldown, parsed_exactly = parse_retry_seconds(line, fallback_cooldown_seconds)
                h, m = cooldown // 3600, (cooldown % 3600) // 60
                if parsed_exactly:
                    print(f"[router] Rate limit — cooldown {h}h {m}m. Switching...", flush=True)
                else:
                    print(f"[router] Rate limit detected, but reset time was unclear. Using a short {cooldown}s retry window.", flush=True)
                proc.terminate()
                proc.wait()
                return True, cooldown
        proc.wait()
        return False, 0
    except FileNotFoundError:
        print(f"[router] Command not found: {cmd[0]}", flush=True)
        return False, 0

# ─── Security helpers ────────────────────────────────────────────────────────

def _schedule_wipe(*paths: Path, delay: int = 5):
    """Delete plaintext session files after a short delay (tool reads at startup then keeps in memory)."""
    def _run():
        import time
        time.sleep(delay)
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    threading.Thread(target=_run, daemon=True).start()


def _is_session_valid(session: dict) -> bool:
    """Return False only if we can confirm the JWT is expired; True otherwise (benefit of the doubt)."""
    try:
        tokens  = session.get("tokens", {})
        tok     = tokens.get("id_token") or tokens.get("access_token", "")
        if not tok:
            return True
        parts = tok.split(".")
        if len(parts) < 2:
            return True
        pad     = 4 - len(parts[1]) % 4
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * pad))
        exp     = payload.get("exp", 0)
        return not (exp and datetime.now(timezone.utc).timestamp() > exp)
    except Exception:
        return True  # can't decode → assume valid


def _codex_session_has_refresh(session: dict) -> bool:
    return bool(session.get("tokens", {}).get("refresh_token"))


def _codex_client_id_from_session(session: dict) -> str:
    try:
        tokens = session.get("tokens", {})
        tok = tokens.get("id_token") or tokens.get("access_token", "")
        parts = tok.split(".")
        if len(parts) < 2:
            return "app_EMoamEEZ73f0CkXaXp7hrann"
        pad = 4 - len(parts[1]) % 4
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * pad))
        aud = payload.get("aud", [])
        if isinstance(aud, list) and aud:
            return aud[0]
        if isinstance(aud, str) and aud:
            return aud
    except Exception:
        pass
    return "app_EMoamEEZ73f0CkXaXp7hrann"


def _refresh_codex_session(session_path: Path, session: dict) -> dict:
    refresh_token = session.get("tokens", {}).get("refresh_token")
    if not refresh_token:
        raise RuntimeError("Saved session has no refresh token.")

    encoded = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": _codex_client_id_from_session(session),
        "refresh_token": refresh_token,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://auth0.openai.com/oauth/token",
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        token_data = json.loads(r.read().decode("utf-8"))

    tokens = session.setdefault("tokens", {})
    tokens["access_token"] = token_data["access_token"]
    if token_data.get("id_token"):
        tokens["id_token"] = token_data["id_token"]
    if token_data.get("refresh_token"):
        tokens["refresh_token"] = token_data["refresh_token"]
    session["last_refresh"] = datetime.now(timezone.utc).isoformat()
    save_encrypted_json(session_path, session)
    return session


# ─── Account preparation ──────────────────────────────────────────────────────

def prepare_account(account: dict) -> tuple[list, dict | None] | None:
    typ   = account.get("type", "")
    label = account.get("label", typ)
    cfg   = load_config()

    if typ == "codex":
        auth_file = account.get("auth_file", "")
        if not auth_file or not (SESSIONS_DIR / auth_file).exists():
            print(f"[router] {label}: auth file not found, skipping.", flush=True)
            return None
        session_path = SESSIONS_DIR / auth_file
        session = load_encrypted_json(session_path)
        if not _is_session_valid(session):
            if _codex_session_has_refresh(session):
                try:
                    session = _refresh_codex_session(session_path, session)
                    print(f"[router] {label}: session refreshed.", flush=True)
                except Exception as e:
                    print(f"[router] {label}: session refresh failed ({e}), skipping.", flush=True)
                    return None
            else:
                print(f"[router] {label}: session expired, skipping.", flush=True)
                return None
        decrypt_session_to_file(session_path, CODEX_AUTH)
        _schedule_wipe(CODEX_AUTH)
        return [get_tool_cmd("codex", cfg)], None

    elif typ == "claude":
        env = os.environ.copy()
        if account.get("api_key"):
            env["ANTHROPIC_API_KEY"] = account["api_key"]
        if account.get("auth_file") and (SESSIONS_DIR / account["auth_file"]).exists():
            session = load_encrypted_json(SESSIONS_DIR / account["auth_file"])
            if not _is_session_valid(session):
                print(f"[router] {label}: session expired, skipping.", flush=True)
                return None
            decrypt_session_to_file(SESSIONS_DIR / account["auth_file"], CLAUDE_CREDS)
            _schedule_wipe(CLAUDE_CREDS)
        return [get_tool_cmd("claude", cfg)], env

    elif typ == "openai_api":
        if not account.get("api_key"):
            print(f"[router] {label}: API key missing, skipping.", flush=True)
            return None
        env = os.environ.copy()
        env["OPENAI_API_KEY"] = account["api_key"]
        return [get_tool_cmd("codex", cfg)], env

    elif typ == "anthropic_api":
        if not account.get("api_key"):
            print(f"[router] {label}: API key missing, skipping.", flush=True)
            return None
        env = os.environ.copy()
        env["ANTHROPIC_API_KEY"] = account["api_key"]
        return [get_tool_cmd("claude", cfg)], env

    print(f"[router] {label}: unknown type '{typ}', skipping.", flush=True)
    return None

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    cleanup_on_startup()
    set_active_account(None)
    args = sys.argv[1:]
    mode = "all"

    if "--mode" in args:
        idx  = args.index("--mode")
        mode = args[idx + 1] if idx + 1 < len(args) else "all"
        args = args[:idx] + args[idx + 2:]

    config   = load_config()
    accounts = config.get("accounts", [])

    if mode == "codex":
        accounts = [a for a in accounts if a.get("type") in ("codex", "openai_api")]
    elif mode == "claude":
        accounts = [a for a in accounts if a.get("type") in ("claude", "anthropic_api")]

    accounts = sorted(
        [a for a in accounts if a.get("enabled", True)],
        key=lambda a: a.get("order", 99),
    )

    if not accounts:
        print(f"[router] No accounts configured for mode '{mode}'.", flush=True)
        sys.exit(1)

    usage_threshold = config.get("settings", {}).get("usage_limit_pct", 100)
    fallback_retry_minutes = config.get("settings", {}).get("fallback_retry_minutes", 1)
    fallback_cooldown_seconds = max(1, int(fallback_retry_minutes)) * 60
    print(f"[router] Mode: {mode} | Accounts: {len(accounts)} | Usage threshold: {usage_threshold}%", flush=True)

    for account in accounts:
        aid       = account["id"]
        remaining = get_cooldown_remaining(aid, config)
        if remaining > 0:
            h, m = remaining // 3600, (remaining % 3600) // 60
            print(f"[router] {account.get('label')}: cooldown {h}h {m}m remaining, skipping.", flush=True)
            continue

        usage_pct = get_cached_usage_pct(aid, config)
        if usage_pct is not None and usage_pct >= usage_threshold:
            print(f"[router] {account.get('label')}: usage {usage_pct:.1f}% >= {usage_threshold}%, skipping.", flush=True)
            continue

        primary_remaining, secondary_remaining = get_cached_usage_limits(aid, config)
        if primary_remaining is not None and primary_remaining <= 0:
            print(f"[router] {account.get('label')}: 5h remaining {primary_remaining:.0f}% = 0%, skipping.", flush=True)
            continue
        if secondary_remaining is not None and secondary_remaining <= 0:
            print(f"[router] {account.get('label')}: weekly remaining {secondary_remaining:.0f}% = 0%, skipping.", flush=True)
            continue

        result = prepare_account(account)
        if result is None:
            continue

        cmd, env = result
        set_active_account(aid)
        print(f"[router] Trying: {account.get('label')}", flush=True)

        account_args = _normalize_args_for_account(account, args)
        hit_limit, cooldown = run_process(
            cmd + account_args,
            env=env,
            fallback_cooldown_seconds=fallback_cooldown_seconds,
        )

        if not hit_limit:
            set_active_account(None)
            print(f"[router] Done: {account.get('label')}", flush=True)
            sys.exit(0)

        set_active_account(None)
        set_rate_limited(aid, cooldown)
        h, m = cooldown // 3600, (cooldown % 3600) // 60
        print(f"[router] {account.get('label')} rate limited for {h}h {m}m.", flush=True)

    set_active_account(None)
    print("[router] All accounts exhausted or on cooldown.", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
