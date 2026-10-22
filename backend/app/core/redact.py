"""Secret redaction for logs, errors and anything that leaves the server.

Every secret value loaded from ``.env`` is registered here; ``redact`` replaces any
occurrence with ``***``. A logging filter applies it to all log records.
"""
from __future__ import annotations

import logging
import re
import threading

_lock = threading.Lock()
_secrets: set[str] = set()
_BEARER = re.compile(r"(?i)(bearer\s+|x-api-key[:=]\s*|x-goog-api-key[:=]\s*|key=)([A-Za-z0-9_\-\.]{8,})")
_SK = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|sk-ant-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{20,})")


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        with _lock:
            _secrets.add(value)


def unregister_secret(value: str | None) -> None:
    if value:
        with _lock:
            _secrets.discard(value)


def redact(text: object) -> str:
    s = str(text)
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for sec in secrets:
        if sec in s:
            s = s.replace(sec, "***")
    s = _BEARER.sub(lambda m: m.group(1) + "***", s)
    s = _SK.sub("***", s)
    return s


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        # Redact message and string args in place, keeping the args structure intact
        # (formatters such as uvicorn's access formatter index into record.args).
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: (redact(v) if isinstance(v, str) else v) for k, v in record.args.items()}
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install_log_redaction() -> None:
    f = RedactingFilter()
    root = logging.getLogger()
    root.addFilter(f)
    for h in root.handlers:
        h.addFilter(f)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "app"):
        lg = logging.getLogger(name)
        lg.addFilter(f)
        for h in lg.handlers:
            h.addFilter(f)
