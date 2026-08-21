import hashlib
import hmac
import secrets
import threading
import time
from src.branding import PRODUCT_NAME


_WINDOW_SECONDS = 90


def signed_headers(secret: str, worker: str, method: str, path: str,
                   body: bytes = b"", now: int | None = None,
                   nonce: str | None = None) -> dict[str, str]:
    timestamp = str(int(now if now is not None else time.time()))
    request_nonce = nonce or secrets.token_urlsafe(18)
    digest = hashlib.sha256(body).hexdigest()
    canonical = "\n".join((timestamp, request_nonce, method.upper(), path, digest))
    signature = hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return {
        f"X-{PRODUCT_NAME}-Worker": worker,
        f"X-{PRODUCT_NAME}-Timestamp": timestamp,
        f"X-{PRODUCT_NAME}-Nonce": request_nonce,
        f"X-{PRODUCT_NAME}-Signature": signature,
    }


class SignatureVerifier:
    def __init__(self, window_seconds: int = _WINDOW_SECONDS):
        self.window_seconds = window_seconds
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()

    def verify(self, secret: str, worker: str, method: str, path: str,
               body: bytes, headers, now: int | None = None) -> tuple[bool, str]:
        current = int(now if now is not None else time.time())
        timestamp = str(headers.get(f"X-{PRODUCT_NAME}-Timestamp", ""))
        nonce = str(headers.get(f"X-{PRODUCT_NAME}-Nonce", ""))
        signature = str(headers.get(f"X-{PRODUCT_NAME}-Signature", ""))
        supplied_worker = str(headers.get(f"X-{PRODUCT_NAME}-Worker", ""))
        try:
            request_time = int(timestamp)
        except ValueError:
            return False, "Invalid worker timestamp"
        if supplied_worker != worker:
            return False, "Worker identity mismatch"
        if abs(current - request_time) > self.window_seconds:
            return False, "Worker request expired"
        if not nonce:
            return False, "Worker nonce is missing"
        expected = signed_headers(
            secret, worker, method, path, body, request_time, nonce,
        )[f"X-{PRODUCT_NAME}-Signature"]
        if not signature or not hmac.compare_digest(signature, expected):
            return False, "Invalid worker signature"
        with self._lock:
            if nonce in self._seen:
                return False, "Worker request was already used"
            self._seen = {
                key: seen_at for key, seen_at in self._seen.items()
                if current - seen_at <= self.window_seconds
            }
            self._seen[nonce] = current
        return True, ""
