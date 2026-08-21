import logging
import logging.handlers
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from src.branding import LEGACY_SLUG, PRODUCT_SLUG


_LOG_NAME = re.compile(rf"^(?:{re.escape(PRODUCT_SLUG)}|{re.escape(LEGACY_SLUG)})(?:\.(\d+))?\.log$")
_LEGACY_LOG_NAME = re.compile(rf"^(?:{re.escape(PRODUCT_SLUG)}|{re.escape(LEGACY_SLUG)})\.log\.(\d+)$")


class RetentionRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Size-based rotation with age and total-storage retention."""

    def __init__(
        self,
        filename: str,
        max_file_size_mb: int = 5,
        max_total_size_mb: int = 50,
        retention_days: int = 14,
    ):
        self.max_total_bytes = 0
        self.retention_seconds = 0
        self._cleanup_lock = threading.Lock()
        self._last_cleanup = 0.0
        super().__init__(
            filename,
            maxBytes=1,
            backupCount=1,
            encoding="utf-8",
        )
        self.namer = self._rotation_name
        self.configure(max_file_size_mb, max_total_size_mb, retention_days)

    @staticmethod
    def _rotation_name(default_name: str) -> str:
        path = Path(default_name)
        match = re.match(r"^(.*)\.log\.(\d+)$", path.name)
        if not match:
            return default_name
        return str(path.with_name(f"{match.group(1)}.{match.group(2)}.log"))

    def configure(
        self,
        max_file_size_mb: int,
        max_total_size_mb: int,
        retention_days: int,
    ) -> None:
        max_bytes = int(max_file_size_mb) * 1024 * 1024
        total_bytes = int(max_total_size_mb) * 1024 * 1024
        self.maxBytes = max_bytes
        self.max_total_bytes = total_bytes
        self.retention_seconds = int(retention_days) * 86400
        self.backupCount = max(1, math.ceil(total_bytes / max_bytes) - 1)
        self.cleanup(force=True)

    def _managed_files(self) -> list[Path]:
        directory = Path(self.baseFilename).parent
        if not directory.exists():
            return []
        return [
            path for path in directory.iterdir()
            if path.is_file() and (
                _LOG_NAME.fullmatch(path.name)
                or _LEGACY_LOG_NAME.fullmatch(path.name)
            )
        ]

    def cleanup(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_cleanup < 300:
            return
        if not self._cleanup_lock.acquire(blocking=False):
            return
        try:
            self._last_cleanup = now
            active = Path(self.baseFilename)
            rotated = [path for path in self._managed_files() if path != active]

            for path in rotated:
                try:
                    if now - path.stat().st_mtime > self.retention_seconds:
                        path.unlink()
                except OSError:
                    continue

            files = self._managed_files()
            total = sum(_safe_size(path) for path in files)
            oldest_rotated = sorted(
                (path for path in files if path != active),
                key=lambda path: _safe_mtime(path),
            )
            for path in oldest_rotated:
                if total <= self.max_total_bytes:
                    break
                size = _safe_size(path)
                try:
                    path.unlink()
                    total -= size
                except OSError:
                    continue
        finally:
            self._cleanup_lock.release()

    def doRollover(self) -> None:
        super().doRollover()
        self.cleanup(force=True)

    def emit(self, record) -> None:
        super().emit(record)
        self.cleanup()


class LogManager:
    def __init__(
        self,
        log_dir: str,
        formatter: logging.Formatter,
        max_file_size_mb: int = 5,
        max_total_size_mb: int = 50,
        retention_days: int = 14,
    ):
        self.log_dir = Path(log_dir).resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.handler = RetentionRotatingFileHandler(
            str(self.log_dir / f"{PRODUCT_SLUG}.log"),
            max_file_size_mb=max_file_size_mb,
            max_total_size_mb=max_total_size_mb,
            retention_days=retention_days,
        )
        self.handler.setFormatter(formatter)

    def configure(
        self,
        max_file_size_mb: int,
        max_total_size_mb: int,
        retention_days: int,
    ) -> None:
        self.handler.acquire()
        try:
            self.handler.configure(
                max_file_size_mb,
                max_total_size_mb,
                retention_days,
            )
        finally:
            self.handler.release()

    def list_files(self) -> list[dict]:
        self.handler.flush()
        self.handler.cleanup(force=True)
        active = Path(self.handler.baseFilename)
        files = []
        for path in self.handler._managed_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append({
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(
                    stat.st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
                "active": path == active,
            })
        return sorted(
            files,
            key=lambda item: (
                not item["active"],
                -datetime.fromisoformat(item["modified"]).timestamp(),
            ),
        )

    def resolve_file(self, name: str) -> Path | None:
        if not (
            _LOG_NAME.fullmatch(name)
            or _LEGACY_LOG_NAME.fullmatch(name)
        ):
            return None
        candidate = (self.log_dir / name).resolve()
        if candidate.parent != self.log_dir or not candidate.is_file():
            return None
        return candidate

    def read_tail(self, name: str, max_bytes: int = 512 * 1024) -> dict | None:
        path = self.resolve_file(name)
        if path is None:
            return None
        self.handler.flush()
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with path.open("rb") as handle:
            handle.seek(start)
            content = handle.read(max_bytes)
        if start:
            first_newline = content.find(b"\n")
            if first_newline >= 0:
                content = content[first_newline + 1:]
        return {
            "name": path.name,
            "content": content.decode("utf-8", errors="replace"),
            "size_bytes": size,
            "truncated": start > 0,
            "active": path == Path(self.handler.baseFilename),
        }


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
