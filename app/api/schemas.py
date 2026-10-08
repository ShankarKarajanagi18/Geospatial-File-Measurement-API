"""Pydantic response models (these also drive the OpenAPI documentation)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain import FileStatus, MeasurementStatus


class FileInfo(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "3f2c9d5ab1e84c0f9b7a6d1e2c3b4a50",
                "filename": "survey.kml",
                "file_type": "kml",
                "size_bytes": 48213,
                "feature_count": 120,
                "crs": "EPSG:4326",
                "status": "COMPLETED",
                "geometry_types": {"Point": 40, "LineString": 40, "Polygon": 40},
                "warnings": [],
                "error": None,
                "created_at": "2026-10-07T09:05:11.123456Z",
                "completed_at": "2026-10-07T09:05:11.481902Z",
            }
        },
    )

    id: str
    filename: str
    file_type: str | None = Field(None, description="kml, kmz or shapefile")
    size_bytes: int
    feature_count: int
    crs: str | None = Field(
        None, description="CRS of the data; MIXED if layers of one upload differ."
    )
    status: FileStatus
    geometry_types: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class FileList(BaseModel):
    total: int
    limit: int
    offset: int
    files: list[FileInfo]


class MeasurementItem(BaseModel):
    index: int = Field(description="Zero-based position of the feature in the file.")
    source_id: str | None = Field(
        None, description="Placemark id (KML) or record number within its layer (shapefile)."
    )
    layer: str | None = None
    geometry_type: str
    crs: str | None = Field(None, description="CRS of the feature's source geometry.")
    status: MeasurementStatus
    area_m2: float | None = Field(None, description="Area in square metres (polygons).")
    perimeter_m: float | None = Field(None, description="Polygon perimeter in metres.")
    length_m: float | None = Field(None, description="Length in metres (lines).")
    method: dict[str, Any] | None = Field(
        None,
        description="How the value was obtained: strategy, projected CRS and the "
        "independent geodesic cross-check.",
    )
    warnings: list[str] = Field(default_factory=list)


class MeasurementSummary(BaseModel):
    measured: int = 0
    not_applicable: int = 0
    unsupported: int = 0
    empty: int = 0
    errors: int = 0
    total_area_m2: float = 0.0
    total_perimeter_m: float = 0.0
    total_length_m: float = 0.0


class MeasurementsResponse(BaseModel):
    file_id: str
    filename: str
    crs: str | None
    units: dict[str, str] = Field(
        default_factory=lambda: {"area": "square metres", "length": "metres"}
    )
    summary: MeasurementSummary
    total: int = Field(description="Number of features matching the filters.")
    limit: int
    offset: int
    measurements: list[MeasurementItem]


class FeatureItem(MeasurementItem):
    geometry: dict[str, Any] | None = Field(
        None, description="GeoJSON geometry in the feature's source CRS (elevation dropped)."
    )
    properties: dict[str, Any] = Field(default_factory=dict)


class FeaturesResponse(BaseModel):
    file_id: str
    total: int
    limit: int
    offset: int
    features: list[FeatureItem]


class ErrorResponse(BaseModel):
    detail: str
    code: str
    id: str | None = Field(None, description="Id of the FAILED file record, when one exists.")


class HealthResponse(BaseModel):
    status: str
    version: str
