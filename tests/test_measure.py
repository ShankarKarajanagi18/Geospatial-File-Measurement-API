"""Measurement maths, checked against independently known geometry."""

from __future__ import annotations

import pytest
from pyproj import CRS, Transformer
from shapely import segmentize
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.domain import MeasurementStatus
from app.services import crs as crs_service
from app.services.measure import measure
from tests import builders as b

WGS84 = crs_service.WGS84
LABEL = "EPSG:4326"


def lonlat_polygon(outer, holes=()):
    return Polygon(b.to_lonlat(outer), [b.to_lonlat(h) for h in holes])


def run(geometry):
    return measure(geometry, WGS84, LABEL)


# --------------------------------------------------------------------------- #
# Polygons
# --------------------------------------------------------------------------- #
def test_square_with_known_size():
    result = run(lonlat_polygon(b.square(0, 0, 1000)))
    assert result.status is MeasurementStatus.MEASURED
    assert result.area_m2 == pytest.approx(1_000_000, rel=1e-6)
    assert result.perimeter_m == pytest.approx(4000, rel=1e-6)
    assert result.length_m is None
    assert result.method["strategy"] == "utm"
    assert result.method["crs"] == "EPSG:32643"


def test_degrees_are_never_used_directly():
    """A 1°x1° box is 1 'square degree' - the real answer is ~1.2e10 m²."""
    result = run(box(77.0, 12.0, 78.0, 13.0))
    assert 1.19e10 < result.area_m2 < 1.22e10
    # perimeter of the same box: roughly 2 x (108 + 111) km
    assert 4.2e5 < result.perimeter_m < 4.5e5


def test_geodesic_cross_check_agrees_within_utm_distortion():
    result = run(lonlat_polygon(b.square(0, 0, 1000)))
    check = result.method["geodesic_check"]
    # UTM is conformal, not equal-area: expect a ~0.1 % difference here, never more than 0.3 %
    assert check["max_deviation_pct"] < 0.3
    assert check["area_m2"] == pytest.approx(result.area_m2, rel=3e-3)
    assert not result.warnings


def test_polygon_with_hole_subtracts_the_hole():
    outer = b.square(0, 0, 2000)
    hole = b.square(500, 500, 1000)
    result = run(lonlat_polygon(outer, [hole]))
    assert result.area_m2 == pytest.approx(4_000_000 - 1_000_000, rel=1e-6)
    assert result.perimeter_m == pytest.approx(8000 + 4000, rel=1e-6)
    # the geodesic reference must also subtract the hole (pyproj's own helper adds it)
    assert result.method["geodesic_check"]["area_m2"] == pytest.approx(3_000_000, rel=3e-3)


def test_multipolygon_sums_parts():
    parts = [lonlat_polygon(b.square(0, 0, 1000)), lonlat_polygon(b.square(3000, 0, 500))]
    result = run(MultiPolygon(parts))
    assert result.area_m2 == pytest.approx(1_000_000 + 250_000, rel=1e-6)
    assert result.perimeter_m == pytest.approx(4000 + 2000, rel=1e-6)


def test_self_intersecting_polygon_is_repaired_with_warning():
    # bow-tie: two triangles of 250 000 m² each
    bowtie = [b.utm(0, 0), b.utm(1000, 1000), b.utm(1000, 0), b.utm(0, 1000), b.utm(0, 0)]
    result = run(lonlat_polygon(bowtie))
    assert result.status is MeasurementStatus.MEASURED
    assert result.area_m2 == pytest.approx(500_000, rel=1e-5)
    assert any("repaired" in w for w in result.warnings)


def test_polygon_orientation_does_not_matter():
    ring = b.square(0, 0, 1000)
    ccw = run(lonlat_polygon(ring)).area_m2
    cw = run(lonlat_polygon(list(reversed(ring)))).area_m2
    assert ccw == pytest.approx(cw, rel=1e-9)


def test_elevation_is_ignored():
    flat = lonlat_polygon(b.square(0, 0, 1000))
    raised = Polygon([(x, y, 900.0) for x, y in flat.exterior.coords])
    assert run(raised).area_m2 == pytest.approx(run(flat).area_m2, rel=1e-12)


# --------------------------------------------------------------------------- #
# Lines, points, collections
# --------------------------------------------------------------------------- #
def test_line_length():
    line = LineString(b.to_lonlat([b.utm(0, 0), b.utm(3000, 0), b.utm(3000, 4000)]))
    result = run(line)
    assert result.status is MeasurementStatus.MEASURED
    assert result.length_m == pytest.approx(7000, rel=1e-6)
    assert result.area_m2 is None and result.perimeter_m is None
    assert result.method["geodesic_check"]["length_m"] == pytest.approx(7000, rel=3e-3)


def test_multilinestring_sums_parts():
    lines = MultiLineString(
        [
            b.to_lonlat([b.utm(0, 0), b.utm(1000, 0)]),
            b.to_lonlat([b.utm(0, 100), b.utm(0, 600)]),
        ]
    )
    assert run(lines).length_m == pytest.approx(1500, rel=1e-6)


def test_linear_ring_is_measured_as_a_line():
    ring = LinearRing(b.to_lonlat(b.square(0, 0, 1000)))
    result = run(ring)
    assert result.length_m == pytest.approx(4000, rel=1e-6)


@pytest.mark.parametrize("geometry", [Point(77.59, 12.97), MultiPoint([(77.59, 12.97), (77.6, 13)])])
def test_points_need_no_measurement(geometry):
    result = run(geometry)
    assert result.status is MeasurementStatus.NOT_APPLICABLE
    assert result.area_m2 is None and result.length_m is None


def test_geometry_collection_measures_each_member():
    collection = GeometryCollection(
        [
            lonlat_polygon(b.square(0, 0, 1000)),
            LineString(b.to_lonlat([b.utm(0, 2000), b.utm(2000, 2000)])),
            Point(*b.to_lonlat([b.utm(0, 0)])[0]),
        ]
    )
    result = run(collection)
    assert result.status is MeasurementStatus.MEASURED
    assert result.area_m2 == pytest.approx(1_000_000, rel=1e-6)
    assert result.length_m == pytest.approx(2000, rel=1e-6)


@pytest.mark.parametrize("geometry", [None, Polygon(), Point(), LineString()])
def test_empty_geometry(geometry):
    result = run(geometry)
    assert result.status is MeasurementStatus.EMPTY


def test_collection_of_only_points_is_not_applicable():
    result = run(GeometryCollection([Point(77.5, 12.9), Point(77.6, 13.0)]))
    assert result.status is MeasurementStatus.NOT_APPLICABLE


# --------------------------------------------------------------------------- #
# CRS strategies
# --------------------------------------------------------------------------- #
def test_southern_hemisphere_uses_south_zone():
    sydney = box(151.20, -33.88, 151.21, -33.87)
    result = run(sydney)
    assert result.method["crs"] == "EPSG:32756"
    assert result.method["geodesic_check"]["max_deviation_pct"] < 0.3


def test_wide_polygon_uses_equal_area_and_matches_ellipsoid():
    # densify so "edge" means the same thing to the projection and to the ellipsoid
    result = run(segmentize(box(0.0, 0.0, 20.0, 10.0), 0.05))
    assert result.method["strategy"] == "equal_area_geodesic"
    # equal-area projection preserves area exactly -> agrees with the ellipsoidal calculation
    assert result.method["geodesic_check"]["max_deviation_pct"] < 0.01
    assert 2.4e12 < result.area_m2 < 2.5e12  # ~20°x10° at the equator
    assert not result.warnings


def test_sparse_continent_scale_polygon_warns_when_methods_disagree():
    """Four far-apart vertices: chords vs geodesics differ, and the API says so."""
    result = run(box(0.0, 0.0, 60.0, 40.0))
    assert result.status is MeasurementStatus.MEASURED
    assert result.method["geodesic_check"]["max_deviation_pct"] > 1.0
    assert any("approximate" in w for w in result.warnings)


def test_wide_line_uses_geodesic_length():
    # London -> Moscow-ish: far wider than one UTM zone
    result = run(LineString([(-0.1, 51.5), (37.6, 55.75)]))
    assert result.method["strategy"] == "equal_area_geodesic"
    assert 2.4e6 < result.length_m < 2.6e6
    assert result.method["geodesic_check"]["length_m"] == pytest.approx(result.length_m, rel=1e-9)


def test_polar_polygon_uses_ups():
    result = run(box(10.0, 85.0, 20.0, 86.0))
    assert result.method["strategy"] == "ups"
    assert result.method["crs"] == "EPSG:32661"
    assert result.method["geodesic_check"]["max_deviation_pct"] < 1.0


def test_feature_spanning_two_utm_zones_still_measures_sensibly():
    # straddles the 78°E zone boundary but is < 6° wide
    result = run(box(77.0, 12.0, 79.0, 13.0))
    assert result.method["strategy"] == "utm"
    assert result.method["geodesic_check"]["max_deviation_pct"] < 1.0


def test_projected_source_is_measured_in_place():
    crs = CRS.from_epsg(32643)
    polygon = Polygon(b.square(0, 0, 1000))
    result = measure(polygon, crs, "EPSG:32643")
    assert result.area_m2 == pytest.approx(1_000_000, rel=1e-12)
    assert result.method["strategy"] == "source_projected"
    assert "geodesic_check" in result.method


def test_feet_based_projected_source_is_converted_to_metres():
    crs = CRS.from_epsg(2263)  # NY Long Island, US survey feet
    x, y = 990_000.0, 200_000.0
    polygon = box(x, y, x + 100, y + 100)  # 100 ft x 100 ft
    result = measure(polygon, crs, "EPSG:2263")
    foot = 1200 / 3937
    assert result.area_m2 == pytest.approx(10_000 * foot**2, rel=1e-6)
    assert result.perimeter_m == pytest.approx(400 * foot, rel=1e-6)
    assert result.method["unit_factor_to_metres"] == pytest.approx(foot)
    assert result.method["geodesic_check"]["max_deviation_pct"] < 0.2


def test_web_mercator_source_is_not_measured_in_mercator_units():
    """Mercator inflates area ~4x at 60°N, so the service must reproject first."""
    to_mercator = Transformer.from_crs(4326, 3857, always_xy=True)
    lonlat = box(10.0, 60.0, 10.2, 60.1)
    mercator = Polygon([to_mercator.transform(x, y) for x, y in lonlat.exterior.coords])
    result = measure(mercator, CRS.from_epsg(3857), "EPSG:3857")
    reference = run(lonlat).area_m2
    assert result.method["strategy"] == "utm"
    assert result.area_m2 == pytest.approx(reference, rel=1e-4)
    assert mercator.area > 3.5 * result.area_m2  # what naive code would have reported


def test_non_wgs84_geographic_source_is_handled():
    nad83 = CRS.from_epsg(4269)
    result = measure(box(-100.0, 40.0, -99.9, 40.1), nad83, "EPSG:4269")
    assert result.status is MeasurementStatus.MEASURED
    assert result.method["strategy"] == "utm"
    assert result.method["geodesic_check"]["max_deviation_pct"] < 0.3


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
def test_out_of_range_latitude_becomes_an_error_status_not_an_exception():
    result = run(Polygon([(0, 80), (10, 80), (10, 100), (0, 100)]))
    assert result.status is MeasurementStatus.ERROR
    assert result.warnings and "latitude" in result.warnings[0].lower()


def test_untransformable_projected_coordinates_become_error_status():
    bad = box(1e13, 1e13, 1e13 + 10, 1e13 + 10)
    result = measure(bad, CRS.from_epsg(4269), "EPSG:4269")
    assert result.status is MeasurementStatus.ERROR
