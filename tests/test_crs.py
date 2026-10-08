"""CRS selection and reprojection helpers."""

from __future__ import annotations

import pytest
from pyproj import CRS
from shapely.geometry import box

from app.services import crs as crs_service
from tests.builders import WGS84_PRJ


@pytest.mark.parametrize(
    ("lon", "lat", "expected"),
    [
        (77.59, 12.97, 32643),  # Bengaluru
        (-0.12, 51.5, 32630),  # London
        (151.2, -33.87, 32756),  # Sydney (southern hemisphere)
        (-74.0, 40.7, 32618),  # New York
        (-180.0, 10.0, 32601),  # western edge
        (179.99, -10.0, 32760),  # eastern edge
        (179.999, 5.0, 32660),  # last degree before the antimeridian is zone 60
        (190.0, 5.0, 32602),  # longitudes above 180 wrap around (190 == -170)
    ],
)
def test_utm_zone_selection(lon, lat, expected):
    assert crs_service.utm_epsg(lon, lat) == expected


def test_utm_zone_never_overflows_at_the_antimeridian():
    # exactly 180 is the zone 60/1 boundary; either is fine, 61 is not
    assert crs_service.utm_epsg(180.0, 5.0) in {32601, 32660}


def test_plan_small_feature_uses_utm():
    plan = crs_service.plan_for_lonlat_bounds((77.5, 12.9, 77.6, 13.0))
    assert plan.strategy == "utm"
    assert plan.crs_label == "EPSG:32643"
    assert not plan.geodesic_lengths


def test_plan_polar_features_use_ups():
    north = crs_service.plan_for_lonlat_bounds((10.0, 85.0, 40.0, 86.0))
    south = crs_service.plan_for_lonlat_bounds((10.0, -88.0, 40.0, -85.0))
    assert (north.strategy, north.crs_label) == ("ups", "EPSG:32661")
    assert (south.strategy, south.crs_label) == ("ups", "EPSG:32761")


def test_plan_wide_feature_uses_equal_area_and_geodesic_lengths():
    plan = crs_service.plan_for_lonlat_bounds((0.0, 0.0, 20.0, 10.0))
    assert plan.strategy == "equal_area_geodesic"
    assert plan.geodesic_lengths
    assert plan.crs is not None and "laea" in plan.crs.to_proj4()


def test_plan_feature_crossing_utm_latitude_limit_uses_equal_area():
    plan = crs_service.plan_for_lonlat_bounds((10.0, 80.0, 12.0, 88.0))
    assert plan.strategy == "equal_area_geodesic"


@pytest.mark.parametrize(
    "bounds",
    [
        (0.0, 0.0, 10.0, 95.0),  # latitude > 90
        (0.0, -91.0, 10.0, 0.0),
        (-400.0, 0.0, 10.0, 10.0),
        (float("nan"), 0.0, 1.0, 1.0),
    ],
)
def test_plan_rejects_impossible_coordinates(bounds):
    with pytest.raises(crs_service.CRSError):
        crs_service.plan_for_lonlat_bounds(bounds)


@pytest.mark.parametrize(
    ("epsg", "expected"),
    [
        (3857, True),  # Web Mercator
        (3395, True),  # World Mercator
        (32662, True),  # WGS84 Plate Carree
        (32643, False),  # UTM is *Transverse* Mercator - fine
        (2263, False),  # Lambert conformal conic
        (3035, False),  # LAEA Europe
        (4326, False),  # geographic
    ],
)
def test_distorts_area(epsg, expected):
    assert crs_service.distorts_area(CRS.from_epsg(epsg)) is expected


def test_linear_unit_factor():
    assert crs_service.linear_unit_factor(CRS.from_epsg(32643)) == pytest.approx(1.0)
    # NY Long Island is in US survey feet
    assert crs_service.linear_unit_factor(CRS.from_epsg(2263)) == pytest.approx(1200 / 3937)


def test_source_projected_plan_rules():
    utm = crs_service.plan_for_source_projected(CRS.from_epsg(32643))
    assert utm is not None and utm.strategy == "source_projected"
    feet = crs_service.plan_for_source_projected(CRS.from_epsg(2263))
    assert feet is not None and feet.unit_factor == pytest.approx(1200 / 3937)
    assert crs_service.plan_for_source_projected(CRS.from_epsg(3857)) is None
    assert crs_service.plan_for_source_projected(CRS.from_epsg(4326)) is None


def test_crs_label_variants():
    assert crs_service.crs_label(CRS.from_epsg(32643)) == "EPSG:32643"
    assert crs_service.crs_label(CRS.from_wkt(WGS84_PRJ)) == "EPSG:4326"  # ESRI-style WKT
    custom = CRS.from_proj4("+proj=laea +lat_0=10 +lon_0=20 +datum=WGS84 +units=m")
    assert crs_service.crs_label(custom).startswith(("CUSTOM:", "EPSG:", "PROJ:"))


def test_reproject_round_trip():
    geometry = box(77.5, 12.9, 77.6, 13.0)
    utm = crs_service.reproject(geometry, crs_service.WGS84, CRS.from_epsg(32643))
    back = crs_service.reproject(utm, CRS.from_epsg(32643), crs_service.WGS84)
    assert back.equals_exact(geometry, tolerance=1e-9)
    # projected coordinates are metres, nothing like degrees
    assert utm.bounds[0] > 100_000


def test_reproject_raises_for_untransformable_coordinates():
    # a UTM easting/northing interpreted as lon/lat in a projected CRS far from its zone
    bad = box(1e12, 1e12, 1e12 + 1, 1e12 + 1)
    with pytest.raises(crs_service.CRSError):
        crs_service.reproject(bad, CRS.from_epsg(32643), crs_service.WGS84)
