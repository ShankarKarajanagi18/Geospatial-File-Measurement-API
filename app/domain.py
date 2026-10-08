"""Plain data types shared by the parsing and measurement layers.

Nothing in here depends on FastAPI or SQLAlchemy, which keeps the geospatial
core easy to unit test.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from pyproj import CRS
from shapely.geometry.base import BaseGeometry


class FileStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MeasurementStatus(str, enum.Enum):
    #: Area and/or length were calculated.
    MEASURED = "MEASURED"
    #: Valid geometry that has nothing to measure (points).
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: Geometry kind we do not support (e.g. KML ``Model``, shapefile ``MultiPatch``).
    UNSUPPORTED = "UNSUPPORTED"
    #: Null / empty geometry.
    EMPTY = "EMPTY"
    #: Something went wrong while measuring this one feature.
    ERROR = "ERROR"


@dataclass
class ParsedFeature:
    index: int
    geometry: BaseGeometry | None
    geometry_type: str
    crs: CRS
    crs_label: str
    properties: dict[str, Any] = field(default_factory=dict)
    source_id: str | None = None
    layer: str | None = None
    #: Set when the source holds a geometry kind we deliberately do not handle.
    unsupported_reason: str | None = None
    #: Set when the geometry exists in the source but could not be built.
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedDataset:
    file_type: str
    features: list[ParsedFeature]
    warnings: list[str] = field(default_factory=list)

    @property
    def crs_label(self) -> str | None:
        labels = {f.crs_label for f in self.features}
        if not labels:
            return None
        return labels.pop() if len(labels) == 1 else "MIXED"


@dataclass
class Measurement:
    status: MeasurementStatus
    area_m2: float | None = None
    perimeter_m: float | None = None
    length_m: float | None = None
    method: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
