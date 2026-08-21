import os
import requests
from datetime import datetime, timezone
from typing import Dict


PROVIDERS = {
    "realdebrid": {
        "url":     "https://api.real-debrid.com/rest/1.0/user",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
        "parse":   lambda d: {
            "username":   d.get("username", "?"),
            "expiration": str(d.get("expiration", ""))[:10],
            "days_left":  _days_left(d.get("expiration", "")),
            "type":       d.get("type", "?"),
        },
    },
    "alldebrid": {
        "url":     "https://api.alldebrid.com/v4/user?agent=mediamender",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
        "parse":   lambda d: {
            "username": d.get("data", {}).get("user", {}).get("username", "?"),
            "expiration": None,
            "days_left":  None,
        },
    },
    "torbox": {
        "url":     "https://api.torbox.app/v1/api/user/me",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
        "parse":   lambda d: {
            "username": d.get("data", {}).get("email", "?"),
            "expiration": None,
            "days_left":  None,
        },
    },
    "debridlink": {
        "url":     "https://debrid-link.com/api/v2/account/infos",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
        "parse":   lambda d: {
            "username": d.get("value", {}).get("username", "?"),
            "expiration": None,
            "days_left":  None,
        },
    },
}

_ENV_KEYS = {
    "realdebrid": "RD_API_KEY",
    "alldebrid":  "AD_API_KEY",
    "torbox":     "TB_API_KEY",
    "debridlink": "DL_API_KEY",
}


def _days_left(expiration_str: str) -> int | None:
    """Calculate days remaining from an ISO expiration string."""
    if not expiration_str:
        return None
    try:
        exp = datetime.fromisoformat(str(expiration_str).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return max(0, (exp - now).days)
    except Exception:
        return None


def get_api_key(provider_type: str, configured_key: str = "", config=None) -> str:
    """
    Resolve API key — priority order:
    1. Env var (RD_API_KEY etc)
    2. config.providers block (set via UI)
    3. configured_key (from path-level provider_checks)
    """
    env_var = _ENV_KEYS.get(provider_type.lower(), "")
    env_key = os.environ.get(env_var, "")
    if env_key:
        return env_key
    if config and hasattr(config, 'providers'):
        cfg_key = config.providers.get(provider_type.lower(), {}).get("api_key", "")
        if cfg_key:
            return cfg_key
    return configured_key or ""


def check_provider(provider_type: str, api_key: str, config=None) -> Dict:
    """
    Ping a debrid provider API to confirm connectivity and valid credentials.
    Returns {pass, detail}. Skipped if no api_key.
    """
    resolved_key = get_api_key(provider_type, api_key, config=config)
    if not resolved_key:
        return {"pass": True, "detail": f"{provider_type}: no API key — check skipped"}

    spec = PROVIDERS.get(provider_type.lower())
    if not spec:
        return {"pass": True, "detail": f"{provider_type}: unknown provider — check skipped"}

    try:
        r = requests.get(spec["url"], headers=spec["headers"](resolved_key), timeout=10)
        if r.status_code == 200:
            try:
                info   = spec["parse"](r.json())
                days   = info.get("days_left")
                detail = f"{provider_type}: {info['username']}"
                if days is not None:
                    detail += f" — {days}d remaining"
                    if days <= 7:
                        return {"pass": False, "detail": detail + " ⚠️ expiring soon"}
            except Exception:
                detail = f"{provider_type}: OK"
            return {"pass": True, "detail": detail}
        elif r.status_code in (401, 403):
            return {"pass": False, "detail": f"{provider_type}: invalid or expired API key"}
        else:
            return {"pass": False, "detail": f"{provider_type}: HTTP {r.status_code}"}
    except requests.exceptions.Timeout:
        return {"pass": False, "detail": f"{provider_type}: request timed out"}
    except Exception as e:
        return {"pass": False, "detail": f"{provider_type}: {e}"}


def get_account_status(provider_type: str, api_key: str = "") -> Dict:
    """
    Fetch full account info for display in the UI.
    Returns {ok, username, expiration, days_left, type, error}.
    """
    resolved_key = get_api_key(provider_type, api_key)
    if not resolved_key:
        return {"ok": False, "error": "No API key configured"}

    spec = PROVIDERS.get(provider_type.lower())
    if not spec:
        return {"ok": False, "error": f"Unknown provider: {provider_type}"}

    try:
        r = requests.get(spec["url"], headers=spec["headers"](resolved_key), timeout=10)
        if r.status_code == 200:
            info = spec["parse"](r.json())
            return {"ok": True, **info}
        elif r.status_code in (401, 403):
            return {"ok": False, "error": "Invalid or expired API key"}
        else:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "Request timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
