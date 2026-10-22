"""`.env` secret store. Values are never returned to callers outside the backend.

Writes: lock → temp file in same dir → fsync → atomic replace → chmod 600.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path

from app.core.fsutil import replace_with_retry, safe_chmod
from app.core.redact import register_secret, unregister_secret

_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


class SecretStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._values: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._values = self._parse()
            for v in self._values.values():
                register_secret(v)

    def _parse(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if not self.path.exists():
            return out
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = _LINE.match(line)
            if not m:
                continue
            k, v = m.group(1), m.group(2)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if v:
                out[k] = v
        return out

    def has(self, name: str | None) -> bool:
        if not name:
            return False
        with self._lock:
            return bool(self._values.get(name)) or bool(os.environ.get(name))

    def get(self, name: str | None) -> str | None:
        """Backend-internal only (provider adapters)."""
        if not name:
            return None
        with self._lock:
            v = self._values.get(name)
        if v:
            return v
        env = os.environ.get(name)
        if env:
            register_secret(env)
        return env or None

    def set(self, name: str, value: str) -> None:
        if not re.match(r"^[A-Z][A-Z0-9_]{1,63}$", name):
            raise ValueError("invalid env var name")
        if "\n" in value or "\r" in value:
            raise ValueError("secret must be a single line")
        with self._lock:
            old = self._values.get(name)
            lines: list[str] = []
            replaced = False
            if self.path.exists():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    m = _LINE.match(line)
                    if m and m.group(1) == name:
                        if not replaced:
                            lines.append(f"{name}={value}")
                            replaced = True
                        continue
                    lines.append(line)
            if not replaced:
                lines.append(f"{name}={value}")
            self._atomic_write("\n".join(lines) + "\n")
            self._values[name] = value
            register_secret(value)
            if old and old != value:
                unregister_secret(old)  # keep redacting new one; old no longer used
                register_secret(old)  # still redact old value if it appears in stale logs

    def _atomic_write(self, content: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".env.tmp.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            safe_chmod(tmp, 0o600)
            replace_with_retry(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def next_free_var(self, prefix: str = "LLM_API_", suffix: str = "_KEY") -> str:
        with self._lock:
            used = set(self._values)
        i = 1
        while True:
            name = f"{prefix}{i:03d}{suffix}"
            if name not in used:
                return name
            i += 1
