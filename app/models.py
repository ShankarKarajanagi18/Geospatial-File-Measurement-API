"""ORM models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.database import Base
from app.domain import FileStatus, MeasurementStatus


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes, even on SQLite (which drops tzinfo)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str | None] = mapped_column(String(16))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), default=FileStatus.PENDING.value, index=True
    )
    error: Mapped[str | None] = mapped_column(Text)
    crs: Mapped[str | None] = mapped_column(String(255))
    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    geometry_types: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    features: Mapped[list[Feature]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Feature.feature_index",
    )


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (
        UniqueConstraint("file_id", "feature_index", name="uq_feature_file_index"),
        Index("ix_feature_file_status", "file_id", "measurement_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("uploaded_files.id", ondelete="CASCADE"), index=True
    )
    feature_index: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[str | None] = mapped_column(String(255))
    layer: Mapped[str | None] = mapped_column(String(255))
    geometry_type: Mapped[str] = mapped_column(String(32), index=True)
    #: GeoJSON geometry in the file's own CRS (elevation dropped).
    geometry: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    crs: Mapped[str | None] = mapped_column(String(255))
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    measurement_status: Mapped[str] = mapped_column(
        String(16), default=MeasurementStatus.EMPTY.value
    )
    area_m2: Mapped[float | None] = mapped_column(Float)
    perimeter_m: Mapped[float | None] = mapped_column(Float)
    length_m: Mapped[float | None] = mapped_column(Float)
    measurement_method: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)

    file: Mapped[UploadedFile] = relationship(back_populates="features")
