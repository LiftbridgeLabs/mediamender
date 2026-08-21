import bcrypt
import hashlib
import os
import secrets
import threading
import time
from functools import wraps
from flask import request, session, redirect, url_for, jsonify
from src.branding import PRODUCT_SLUG, get_env

_BCRYPT_ROUNDS = 12


def hash_password(password: str) -> str:
    """Generate a bcrypt hash for storage in config.yml."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode()


def generate_api_token() -> str:
    """Generate a high-entropy bearer token that is independent of login auth."""
    return f"{PRODUCT_SLUG}_{secrets.token_urlsafe(32)}"


def hash_api_token(token: str) -> str:
    """Hash a random API token for storage and constant-time verification."""
    return hashlib.sha256(token.encode()).hexdigest()


def _environment_hash(password: str) -> str:
    """Deterministic SHA-256 hash used for environment login auth."""
    return hashlib.sha256(f"mediamender:{password}".encode()).hexdigest()


def _previous_environment_hash(password: str) -> str:
    """Hash namespace used by pre-mediaMender installations."""
    return hashlib.sha256(f"emptyarr:{password}".encode()).hexdigest()


def _verify_password(plain: str, stored: str) -> bool:
    """Verify plain password against stored hash.
    Supports bcrypt (new) and SHA-256 (legacy config.yml hashes).
    """
    if stored.startswith(("$2b$", "$2a$", "$2y$")):
        try:
            return bcrypt.checkpw(plain.encode(), stored.encode())
        except Exception:
            return False
    # SHA-256 fallback for environment auth and older config files.
    return secrets.compare_digest(_environment_hash(plain), stored) or secrets.compare_digest(
        _previous_environment_hash(plain), stored,
    )


def _get_credentials(config=None):
    env_user = get_env("MEDIAMENDER_USERNAME")
    env_pass = get_env("MEDIAMENDER_PASSWORD")
    if env_user and env_pass:
        return env_user, _environment_hash(env_pass)
    if config and getattr(config, "auth_username", "") and getattr(config, "auth_password_hash", ""):
        return config.auth_username, config.auth_password_hash
    return None, None


def auth_enabled(config=None) -> bool:
    u, _ = _get_credentials(config)
    return bool(u)


# ── Brute force protection ────────────────────────────────────────────────────
# Simple in-memory tracker: {ip: [timestamp, ...]}
_login_attempts: dict = {}
_locked_until: dict = {}
_attempt_lock = threading.Lock()
_MAX_ATTEMPTS  = 10    # max failures in window
_WINDOW_SECS   = 300   # 5 minute window
_LOCKOUT_SECS  = 600   # 10 minute lockout after max attempts


def _record_attempt(ip: str, success: bool):
    with _attempt_lock:
        now = time.time()
        attempts = _login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < _WINDOW_SECS]
        if not success:
            attempts.append(now)
            if len(attempts) >= _MAX_ATTEMPTS:
                _locked_until[ip] = now + _LOCKOUT_SECS
        else:
            attempts = []
            _locked_until.pop(ip, None)
        _login_attempts[ip] = attempts


def _is_locked_out(ip: str) -> bool:
    with _attempt_lock:
        now = time.time()
        until = _locked_until.get(ip, 0)
        if until <= now:
            _locked_until.pop(ip, None)
            return False
        return True


def check_credentials(username: str, password: str, config=None, ip: str = "") -> bool:
    u, ph = _get_credentials(config)
    if not u:
        return True
    if ip and _is_locked_out(ip):
        return False
    ok = secrets.compare_digest(username, u) and _verify_password(password, ph)
    if ip:
        _record_attempt(ip, ok)
    return ok


def is_locked_out(ip: str) -> bool:
    return _is_locked_out(ip)


def is_authenticated() -> bool:
    return session.get("authenticated") is True


def has_valid_api_token(config=None) -> bool:
    """Return whether this request carries the configured API token."""
    token = request.headers.get("X-API-Token", "")
    if not token:
        return False
    configured_token = get_env("MEDIAMENDER_API_TOKEN")
    expected_hash = (
        hash_api_token(configured_token)
        if configured_token
        else getattr(config, "auth_api_token_hash", "")
    )
    supplied_hash = hash_api_token(token)
    return bool(expected_hash and secrets.compare_digest(supplied_hash, expected_hash))


def require_auth(f):
    """
    Redirect to login for page requests, 401 for API requests.
    API requests can authenticate via session cookie OR X-API-Token header.
    API tokens are generated independently from the login password.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        from app import config as _config
        if not auth_enabled(_config):
            return f(*args, **kwargs)
        # Check session first
        if is_authenticated():
            return f(*args, **kwargs)
        # Check X-API-Token header for API requests
        if request.path.startswith("/api/"):
            if has_valid_api_token(_config):
                return f(*args, **kwargs)
            return jsonify({"error": "Unauthorized — set credentials or provide X-API-Token header"}), 401
        return redirect(url_for("login"))
    return decorated
