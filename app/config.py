"""Runtime configuration, read from environment variables.

Every limit has a conservative default so the service is safe out of the box.
Tests build their own :class:`Settings` instances instead of touching the
environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # fail fast on typos instead of silently ignoring them
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"Environment variable {name} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    """Application settings."""

    database_url: str = "sqlite:///./data/geo_measure.db"
    #: Largest accepted upload (bytes).
    max_upload_bytes: int = 50 * 1024 * 1024
    #: Largest total *decompressed* size of a zip upload (zip-bomb guard).
    max_uncompressed_bytes: int = 500 * 1024 * 1024
    #: Largest number of members in a zip upload.
    max_zip_members: int = 1000
    #: Largest number of features accepted from one file.
    max_features: int = 250_000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("DATABASE_URL", cls.database_url),
            max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", cls.max_upload_bytes),
            max_uncompressed_bytes=_env_int(
                "MAX_UNCOMPRESSED_BYTES", cls.max_uncompressed_bytes
            ),
            max_zip_members=_env_int("MAX_ZIP_MEMBERS", cls.max_zip_members),
            max_features=_env_int("MAX_FEATURES", cls.max_features),
            log_level=os.environ.get("LOG_LEVEL", cls.log_level).upper(),
        )
