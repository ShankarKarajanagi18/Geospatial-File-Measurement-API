"""Coordinate reference system helpers.

Area and distance can never be computed from latitude/longitude degrees. This
module answers two questions:

1. *Which projected CRS should a feature be measured in?* (:func:`plan_measurement_crs`)
2. *How do I move geometry between CRSs safely?* (:func:`reproject`)

Selection strategy (per feature, so one file may use several zones):

``source_projected``
    The file's own CRS is projected, has a known linear unit and does not badly
    distort area (Web Mercator and friends do). Measure in place, converting
    feet etc. to metres.
``utm``
    Geographic input that fits inside one 6° UTM zone between 80°S and 84°N.
    Distortion is below ~0.2 % (typically 0.04 %).
``ups``
    Entirely inside a polar cap (north of 84°N / south of 80°S).
``equal_area_geodesic``
    Wide features (more than 6° of longitude, or crossing the UTM latitude
    limits). Area is measured in a Lambert azimuthal equal-area projection
    centred on the feature, which preserves area at any extent. Lengths and
    perimeters switch to the WGS84 ellipsoid, because no single projection
    preserves distance over such extents.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np
import shapely
from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry

WGS84 = CRS.from_epsg(4326)
WGS84_LABEL = "EPSG:4326"

#: Widest longitude span (degrees) that is still measured inside a single UTM zone.
UTM_MAX_SPAN_DEG = 6.0
UTM_MIN_LAT = -80.0
UTM_MAX_LAT = 84.0


class CRSError(ValueError):
    """Raised when a geometry cannot be reprojected or has impossible coordinates."""


@dataclass(frozen=True)
class MeasurementPlan:
    strategy: str
    #: CRS used for the primary area measurement (``None`` => ellipsoidal only).
    crs: CRS | None
    #: Human-readable label for the CRS above.
    crs_label: str
    #: Metres per CRS unit (only meaningful for ``source_projected``).
    unit_factor: float = 1.0
    #: Whether lengths/perimeters should be measured on the ellipsoid.
    geodesic_lengths: bool = False


def crs_label(crs: CRS) -> str:
    """Return ``EPSG:xxxx`` when the CRS can be identified, else a descriptive label."""
    epsg = crs.to_epsg()
    if epsg:
        return f"EPSG:{epsg}"
    authority = crs.to_authority()
    if authority:
        return f"{authority[0]}:{authority[1]}"
    return f"CUSTOM:{crs.name}"


def is_wgs84(label: str) -> bool:
    return label == WGS84_LABEL


# --------------------------------------------------------------------------- #
# Transformers (cached per thread; PROJ transformers are not safe to share)
# --------------------------------------------------------------------------- #
_local = threading.local()


def _transformer(src: CRS, dst: CRS) -> Transformer:
    cache: dict[tuple[str, str], Transformer] | None = getattr(_local, "cache", None)
    if cache is None:
        cache = _local.cache = {}
    key = (src.srs, dst.srs)
    transformer = cache.get(key)
    if transformer is None:
        if len(cache) > 256:
            cache.clear()
        transformer = cache[key] = Transformer.from_crs(src, dst, always_xy=True)
    return transformer


def reproject(geometry: BaseGeometry, src: CRS, dst: CRS) -> BaseGeometry:
    """Reproject ``geometry`` (x/y order, always lon/lat for geographic CRSs)."""
    transformer = _transformer(src, dst)

    def apply(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack((x, y))

    result = shapely.transform(geometry, apply)
    if not np.isfinite(shapely.get_coordinates(result)).all():
        raise CRSError(
            f"Coordinates could not be transformed from {src.name} to {dst.name}; "
            "they are probably outside the area the CRS is defined for."
        )
    return result


# --------------------------------------------------------------------------- #
# Source-CRS classification
# --------------------------------------------------------------------------- #
_AREA_DISTORTING_METHODS = ("mercator", "equidistant cylindrical", "equirectangular")
_NOT_PLAIN_MERCATOR = ("transverse", "oblique", "hotine")


def distorts_area(crs: CRS) -> bool:
    """True for projections (Web Mercator, plate carrée, ...) unfit for area/length."""
    operation = crs.coordinate_operation
    if operation is None:
        return False
    method = operation.method_name.lower()
    if any(word in method for word in _NOT_PLAIN_MERCATOR):
        return False
    return any(word in method for word in _AREA_DISTORTING_METHODS)


def linear_unit_factor(crs: CRS) -> float | None:
    """Metres per unit of the CRS's horizontal axes, or ``None`` if unknown."""
    try:
        factor = crs.axis_info[0].unit_conversion_factor
    except (IndexError, AttributeError):
        return None
    if factor and factor > 0 and np.isfinite(factor):
        return float(factor)
    return None


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def utm_epsg(lon: float, lat: float) -> int:
    """EPSG code of the UTM zone containing ``lon``/``lat``."""
    lon = ((lon + 180.0) % 360.0) - 180.0
    zone = min(int((lon + 180.0) // 6.0) + 1, 60)
    return (32600 if lat >= 0 else 32700) + zone


@lru_cache(maxsize=256)
def _epsg_crs(code: int) -> CRS:
    return CRS.from_epsg(code)


@lru_cache(maxsize=512)
def _laea_crs(lat_0: float, lon_0: float) -> CRS:
    return CRS.from_proj4(
        f"+proj=laea +lat_0={lat_0} +lon_0={lon_0} +x_0=0 +y_0=0 "
        "+datum=WGS84 +units=m +no_defs"
    )


def plan_for_source_projected(crs: CRS) -> MeasurementPlan | None:
    """Plan for measuring directly in a usable projected source CRS, else ``None``."""
    if not crs.is_projected or distorts_area(crs):
        return None
    factor = linear_unit_factor(crs)
    if factor is None:
        return None
    return MeasurementPlan(
        strategy="source_projected",
        crs=crs,
        crs_label=crs_label(crs),
        unit_factor=factor,
    )


def plan_for_lonlat_bounds(bounds: tuple[float, float, float, float]) -> MeasurementPlan:
    """Choose a projected CRS from lon/lat bounds ``(minx, miny, maxx, maxy)``."""
    minx, miny, maxx, maxy = bounds
    if not all(np.isfinite(bounds)):
        raise CRSError("Geometry has non-finite coordinates.")
    if miny < -90.0 or maxy > 90.0 or minx < -360.0 or maxx > 360.0:
        raise CRSError(
            "Coordinates are outside valid longitude/latitude ranges; "
            "the file's CRS is probably not geographic."
        )

    lon_c = (minx + maxx) / 2.0
    lat_c = (miny + maxy) / 2.0
    span = maxx - minx

    if miny >= UTM_MIN_LAT and maxy <= UTM_MAX_LAT and span <= UTM_MAX_SPAN_DEG:
        code = utm_epsg(lon_c, lat_c)
        return MeasurementPlan("utm", _epsg_crs(code), f"EPSG:{code}")

    if miny >= UTM_MAX_LAT:
        return MeasurementPlan("ups", _epsg_crs(32661), "EPSG:32661")
    if maxy <= UTM_MIN_LAT:
        return MeasurementPlan("ups", _epsg_crs(32761), "EPSG:32761")

    lat_0 = float(round(lat_c))
    lon_0 = float(round(((lon_c + 180.0) % 360.0) - 180.0))
    return MeasurementPlan(
        strategy="equal_area_geodesic",
        crs=_laea_crs(lat_0, lon_0),
        crs_label=f"PROJ:laea lat_0={lat_0:g} lon_0={lon_0:g}",
        geodesic_lengths=True,
    )


def describe_plan(plan: MeasurementPlan) -> dict[str, Any]:
    info: dict[str, Any] = {"strategy": plan.strategy, "crs": plan.crs_label}
    if plan.geodesic_lengths:
        info["length_method"] = "geodesic (WGS84 ellipsoid)"
    if plan.strategy == "source_projected" and plan.unit_factor != 1.0:
        info["unit_factor_to_metres"] = plan.unit_factor
    return info
