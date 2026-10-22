"""Filesystem layout and process-level settings.

All paths derive from ``DEBATE_STUDIO_HOME`` (default: repository root) so tests can
run against a temporary directory without touching real config or secrets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class AppSettings:
    home: Path
    enable_fake_provider: bool = False
    allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    )
    # attachment limits (DESIGN 4.6.1, configurable)
    max_file_bytes: int = 20 * 1024 * 1024
    max_session_bytes: int = 100 * 1024 * 1024
    max_files_per_session: int = 20
    max_uncompressed_bytes: int = 100 * 1024 * 1024
    max_zip_ratio: int = 200
    max_pdf_pages: int = 500
    max_xlsx_cells: int = 200_000
    max_image_pixels: int = 40_000_000
    extraction_timeout_s: float = 60.0
    heartbeat_s: float = 15.0
    extra: dict = field(default_factory=dict)

    @property
    def config_dir(self) -> Path:
        return self.home / "config"

    @property
    def roles_dir(self) -> Path:
        return self.home / "roles"

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "debates.sqlite3"

    @property
    def env_path(self) -> Path:
        return self.home / ".env"

    @property
    def frontend_dist(self) -> Path:
        return self.home / "frontend" / "dist"

    def ensure_dirs(self) -> None:
        for p in (self.config_dir, self.roles_dir, self.data_dir, self.uploads_dir):
            p.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.uploads_dir, 0o700)
        except OSError:
            pass


def load_settings() -> AppSettings:
    home = Path(os.environ.get("DEBATE_STUDIO_HOME", str(REPO_ROOT))).resolve()
    fake = os.environ.get("DEBATE_STUDIO_ENABLE_FAKE", "0") == "1"
    s = AppSettings(home=home, enable_fake_provider=fake)
    extra_origins = os.environ.get("DEBATE_STUDIO_ALLOWED_ORIGINS")
    if extra_origins:
        s.allowed_origins = tuple(o.strip() for o in extra_origins.split(",") if o.strip())
    return s
