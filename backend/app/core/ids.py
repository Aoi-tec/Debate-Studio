from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def now_ts() -> float:
    return time.time()
