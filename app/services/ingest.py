"""Upload orchestration: validate -> record -> parse -> measure -> persist."""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import shapely
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain import FileStatus, Measurement, MeasurementStatus, ParsedDataset
from app.exceptions import AppError, EmptyUpload, UnsupportedFileType
from app.models import Feature, UploadedFile
from app.services import measure
from app.services.parsers import SUPPORTED_EXTENSIONS, parse_upload

logger = logging.getLogger(__name__)

_INSERT_CHUNK = 2000


def sanitize_filename(raw: str | None) -> str:
    """Keep only the final path component; never trust client-supplied paths."""
    name = posixpath.basename((raw or "").replace("\\", "/")).strip().replace("\x00", "")
    return name[:255]


def validate_upload(filename: str, content: bytes) -> None:
    if not filename:
        raise UnsupportedFileType("The upload has no filename.")
    ext = posixpath.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileType(
            f"Unsupported file type '{ext or filename}'. "
            "Upload a .zip containing a Shapefile, or a .kml file."
        )
    if not content:
        raise EmptyUpload("The uploaded file is empty.")


def _geojson(geometry: Any) -> dict[str, Any] | None:
    if geometry is None or geometry.is_empty:
        return None
    return json.loads(shapely.to_geojson(shapely.force_2d(geometry)))


def _feature_rows(file_id: str, dataset: ParsedDataset) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature in dataset.features:
        if feature.error:
            result = Measurement(MeasurementStatus.ERROR, warnings=[feature.error])
        elif feature.unsupported_reason:
            result = Measurement(
                MeasurementStatus.UNSUPPORTED, warnings=[feature.unsupported_reason]
            )
        else:
            result = measure.measure(feature.geometry, feature.crs, feature.crs_label)
        rows.append(
            {
                "file_id": file_id,
                "feature_index": feature.index,
                "source_id": feature.source_id,
                "layer": feature.layer,
                "geometry_type": feature.geometry_type,
                "geometry": _geojson(feature.geometry),
                "crs": feature.crs_label,
                "properties": feature.properties,
                "measurement_status": result.status.value,
                "area_m2": result.area_m2,
                "perimeter_m": result.perimeter_m,
                "length_m": result.length_m,
                "measurement_method": result.method,
                "warnings": feature.warnings + result.warnings,
            }
        )
    return rows


def ingest_upload(db: Session, settings: Settings, filename: str, content: bytes) -> UploadedFile:
    """Process an upload end to end and return the stored file record.

    Raises :class:`AppError` for rejected uploads. When the failure happens
    after the file record exists (unreadable content), the record is kept with
    status ``FAILED`` and its id is attached to the exception.
    """
    filename = sanitize_filename(filename)
    validate_upload(filename, content)

    record = UploadedFile(
        filename=filename,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        status=FileStatus.PROCESSING.value,
    )
    db.add(record)
    db.commit()  # visible immediately as PROCESSING

    started = time.perf_counter()
    try:
        dataset = parse_upload(filename, content, settings)
        rows = _feature_rows(record.id, dataset)
        for start in range(0, len(rows), _INSERT_CHUNK):
            db.execute(insert(Feature), rows[start : start + _INSERT_CHUNK])

        record.file_type = dataset.file_type
        record.crs = dataset.crs_label
        record.feature_count = len(rows)
        record.geometry_types = dict(Counter(r["geometry_type"] for r in rows))
        record.warnings = dataset.warnings
        record.status = FileStatus.COMPLETED.value
        record.completed_at = datetime.now(UTC)
        db.commit()
    except AppError as exc:
        _mark_failed(db, record, exc.message)
        exc.file_id = record.id
        raise
    except Exception as exc:
        logger.exception("Unexpected failure while processing file %s", record.id)
        _mark_failed(db, record, "Internal error while processing the file.")
        wrapped = AppError("Internal error while processing the file.")
        wrapped.file_id = record.id
        raise wrapped from exc

    logger.info(
        "Processed %s (%s): %d features in %.0f ms",
        record.id,
        filename,
        record.feature_count,
        (time.perf_counter() - started) * 1000,
    )
    return record


def _mark_failed(db: Session, record: UploadedFile, message: str) -> None:
    db.rollback()
    record.status = FileStatus.FAILED.value
    record.error = message
    record.completed_at = datetime.now(UTC)
    db.add(record)
    db.commit()
