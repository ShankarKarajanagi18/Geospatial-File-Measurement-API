"""Area / length calculation for a single feature.

Pipeline for every feature::

    geometry (source CRS)
      -> flatten Multi*/collections into Polygon / LineString / Point parts
      -> repair self-intersecting polygons (make_valid), recording a warning
      -> pick a projected CRS (see :mod:`app.services.crs`)
      -> reproject and measure planar area / perimeter / length
      -> cross-check against an independent ellipsoidal (geodesic) calculation

The geodesic cross-check uses the WGS84 ellipsoid and is reported alongside
every result, so a client can see how far the projected value is from the
"physical" truth. A large disagreement (usually a feature that straddles
several UTM zones) is surfaced as a warning.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import shapely
from pyproj import CRS, Geod
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

from app.domain import Measurement, MeasurementStatus
from app.services import crs as crs_service
from app.services.crs import MeasurementPlan

GEOD = Geod(ellps="WGS84")
#: Deviation (percent) between projected and geodesic values that triggers a warning.
DEVIATION_WARN_PCT = 1.0
_DECIMALS = 4

_LINE_TYPES = {"LineString", "LinearRing"}


def _flatten(geometry: BaseGeometry) -> Iterator[BaseGeometry]:
    """Yield the simple parts (Point / LineString / LinearRing / Polygon) of a geometry."""
    if geometry.is_empty:
        return
    if geometry.geom_type in {
        "MultiPoint",
        "MultiLineString",
        "MultiPolygon",
        "GeometryCollection",
    }:
        for part in geometry.geoms:
            yield from _flatten(part)
    else:
        yield geometry


def _ring_coords(ring: BaseGeometry) -> np.ndarray:
    return np.asarray(ring.coords)[:, :2]


def _geodesic_polygon(polygon: BaseGeometry) -> tuple[float, float]:
    """Ellipsoidal (area, perimeter) of a polygon with holes, in m² and m.

    ``pyproj.Geod.geometry_area_perimeter`` adds hole areas instead of
    subtracting them for some ring orientations, so rings are handled
    explicitly: |exterior| - sum(|holes|).
    """
    xs, ys = _ring_coords(polygon.exterior).T
    area, perimeter = GEOD.polygon_area_perimeter(xs, ys)
    area = abs(area)
    for hole in polygon.interiors:
        hx, hy = _ring_coords(hole).T
        hole_area, hole_perimeter = GEOD.polygon_area_perimeter(hx, hy)
        area -= abs(hole_area)
        perimeter += hole_perimeter
    return max(area, 0.0), perimeter


def _geodesic_line(line: BaseGeometry) -> float:
    xs, ys = _ring_coords(line).T
    return float(GEOD.line_length(xs, ys))


def _deviation_pct(value: float, reference: float) -> float:
    if reference == 0:
        return 0.0 if value == 0 else float("inf")
    return abs(value - reference) / reference * 100.0


def _prepare_parts(
    geometry: BaseGeometry, warnings: list[str]
) -> tuple[list[BaseGeometry], list[BaseGeometry], list[BaseGeometry]]:
    """Split into (polygons, lines, points), repairing invalid polygons."""
    polygons: list[BaseGeometry] = []
    lines: list[BaseGeometry] = []
    points: list[BaseGeometry] = []
    repaired = 0
    for part in _flatten(geometry):
        kind = part.geom_type
        if kind == "Polygon":
            if part.is_valid:
                polygons.append(part)
                continue
            repaired += 1
            if repaired == 1:
                warnings.append(
                    f"Invalid polygon geometry ({explain_validity(part)}); "
                    "measured on the repaired geometry."
                )
            polygons.extend(
                piece
                for piece in _flatten(shapely.make_valid(part))
                if piece.geom_type == "Polygon" and not piece.is_empty
            )
        elif kind in _LINE_TYPES:
            lines.append(part)
        elif kind == "Point":
            points.append(part)
    if repaired > 1:
        warnings.append(f"{repaired} polygons in this feature were repaired before measuring.")
    return polygons, lines, points


def measure(geometry: BaseGeometry | None, source_crs: CRS, source_label: str) -> Measurement:
    """Measure one feature. Never raises for bad input: failures become statuses."""
    if geometry is None or geometry.is_empty:
        return Measurement(MeasurementStatus.EMPTY, warnings=["Feature has no geometry."])

    try:
        return _measure(shapely.force_2d(geometry), source_crs, source_label)
    except crs_service.CRSError as exc:
        return Measurement(MeasurementStatus.ERROR, warnings=[str(exc)])
    except Exception as exc:  # one bad feature must never take the whole file down
        return Measurement(
            MeasurementStatus.ERROR,
            warnings=[f"Measurement failed: {type(exc).__name__}: {exc}"],
        )


def _measure(geometry: BaseGeometry, source_crs: CRS, source_label: str) -> Measurement:
    warnings: list[str] = []
    polygons, lines, points = _prepare_parts(geometry, warnings)

    if not polygons and not lines:
        if points:
            return Measurement(
                MeasurementStatus.NOT_APPLICABLE,
                warnings=["Points have no area or length."],
            )
        return Measurement(
            MeasurementStatus.EMPTY,
            warnings=["Geometry has no measurable parts."] + warnings,
        )

    parts = polygons + lines

    # --- lon/lat view of the geometry: used for CRS selection and the geodesic check
    lonlat_parts: list[BaseGeometry] | None
    try:
        if crs_service.is_wgs84(source_label):
            lonlat_parts = parts
        else:
            lonlat_parts = [crs_service.reproject(p, source_crs, crs_service.WGS84) for p in parts]
    except crs_service.CRSError:
        lonlat_parts = None  # tolerated only when we can measure in the source CRS

    # --- choose the projected CRS
    plan: MeasurementPlan | None = None
    if not source_crs.is_geographic:
        plan = crs_service.plan_for_source_projected(source_crs)
    if plan is None:
        if lonlat_parts is None:
            raise crs_service.CRSError(
                f"Could not transform coordinates from {source_label} to WGS84."
            )
        minx, miny, maxx, maxy = (float(v) for v in shapely.total_bounds(lonlat_parts))
        plan = crs_service.plan_for_lonlat_bounds((minx, miny, maxx, maxy))

    # --- planar measurement
    if plan.strategy == "source_projected":
        planar = parts
    else:
        assert plan.crs is not None
        planar = [crs_service.reproject(p, source_crs, plan.crs) for p in parts]
    factor = plan.unit_factor
    n_poly = len(polygons)
    planar_polys, planar_lines = planar[:n_poly], planar[n_poly:]

    area = sum(p.area for p in planar_polys) * factor**2 if polygons else None

    perimeter: float | None = None
    length: float | None = None
    if plan.geodesic_lengths:
        # lengths are measured on the ellipsoid for very wide features
        assert lonlat_parts is not None
        ll_polys, ll_lines = lonlat_parts[:n_poly], lonlat_parts[n_poly:]
        if polygons:
            perimeter = sum(_geodesic_polygon(p)[1] for p in ll_polys)
        if lines:
            length = sum(_geodesic_line(line) for line in ll_lines)
    else:
        if polygons:
            perimeter = sum(p.length for p in planar_polys) * factor
        if lines:
            length = sum(line.length for line in planar_lines) * factor

    # --- geodesic cross-check (needs lon/lat)
    method = crs_service.describe_plan(plan)
    if lonlat_parts is not None:
        ll_polys, ll_lines = lonlat_parts[:n_poly], lonlat_parts[n_poly:]
        check: dict[str, float] = {}
        deviations: list[float] = []
        if polygons:
            geo = [_geodesic_polygon(p) for p in ll_polys]
            g_area, g_perimeter = sum(g[0] for g in geo), sum(g[1] for g in geo)
            check["area_m2"] = round(g_area, _DECIMALS)
            check["perimeter_m"] = round(g_perimeter, _DECIMALS)
            assert area is not None and perimeter is not None
            deviations += [_deviation_pct(area, g_area), _deviation_pct(perimeter, g_perimeter)]
        if lines:
            g_length = sum(_geodesic_line(line) for line in ll_lines)
            check["length_m"] = round(g_length, _DECIMALS)
            assert length is not None
            deviations.append(_deviation_pct(length, g_length))
        worst = max(deviations) if deviations else 0.0
        check["max_deviation_pct"] = round(worst, 6)
        method["geodesic_check"] = check
        if worst > DEVIATION_WARN_PCT:
            warnings.append(
                f"Projected result differs from the geodesic reference by {worst:.2f}%. "
                "The feature is very large or has widely spaced vertices, so treat the "
                "values as approximate."
            )
    else:
        warnings.append("Geodesic cross-check skipped: could not transform to WGS84.")

    return Measurement(
        status=MeasurementStatus.MEASURED,
        area_m2=None if area is None else round(float(area), _DECIMALS),
        perimeter_m=None if perimeter is None else round(float(perimeter), _DECIMALS),
        length_m=None if length is None else round(float(length), _DECIMALS),
        method=method,
        warnings=warnings,
    )
