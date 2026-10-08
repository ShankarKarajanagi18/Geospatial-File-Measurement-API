"""Zipped shapefile parsing, through the public API."""

from __future__ import annotations

import io
import zipfile

import pytest
import shapefile
from pyproj import CRS, Transformer

from tests import builders as b
from tests.helpers import measurements, upload, upload_and_measure, upload_ok

UTM_PRJ = b.UTM43.to_wkt()


def feats(client, info):
    response = client.get(f"/api/files/{info['id']}/features/")
    assert response.status_code == 200
    return response.json()["features"]


def square_ring(dx=0, dy=0, size=1000, points=b.utm):
    return b.cw(b.square(dx, dy, size))


# --------------------------------------------------------------------------- #
# Geometry types
# --------------------------------------------------------------------------- #
def test_polygons_in_lonlat(client):
    ring = b.cw(b.to_lonlat(b.square(0, 0, 1000)))
    z = b.shapefile_zip(shapefile.POLYGON, [[ring]])
    info = upload_ok(client, "parcels.zip", z)
    assert info["file_type"] == "shapefile" and info["crs"] == "EPSG:4326"
    (m,) = measurements(client, info["id"])["measurements"]
    assert m["area_m2"] == pytest.approx(1_000_000, rel=1e-6)
    assert m["method"]["strategy"] == "utm"


def test_polygon_hole(client):
    outer = b.cw(b.to_lonlat(b.square(0, 0, 2000)))
    hole = b.ccw(b.to_lonlat(b.square(500, 500, 1000)))
    z = b.shapefile_zip(shapefile.POLYGON, [[outer, hole]])
    (m,) = upload_and_measure(client, "p.zip", z)
    assert m["area_m2"] == pytest.approx(3_000_000, rel=1e-6)
    assert m["perimeter_m"] == pytest.approx(12_000, rel=1e-6)


def test_multi_ring_polygon_is_multipolygon(client):
    rings = [
        b.cw(b.to_lonlat(b.square(0, 0, 1000))),
        b.cw(b.to_lonlat(b.square(3000, 0, 500))),
    ]
    z = b.shapefile_zip(shapefile.POLYGON, [rings])
    info = upload_ok(client, "p.zip", z)
    (f,) = feats(client, info)
    assert f["geometry_type"] == "MultiPolygon"
    assert f["area_m2"] == pytest.approx(1_250_000, rel=1e-6)


def test_counter_clockwise_outer_ring_is_not_mistaken_for_a_hole(client):
    """Badly wound shapefiles exist in the wild; the area must still be right."""
    ring = b.ccw(b.to_lonlat(b.square(0, 0, 1000)))
    z = b.shapefile_zip(shapefile.POLYGON, [[ring]])
    (m,) = upload_and_measure(client, "p.zip", z)
    assert m["area_m2"] == pytest.approx(1_000_000, rel=1e-6)


def test_polylines_single_and_multi_part(client):
    single = [b.to_lonlat([b.utm(0, 0), b.utm(1000, 0)])]
    multi = [
        b.to_lonlat([b.utm(0, 0), b.utm(1000, 0)]),
        b.to_lonlat([b.utm(0, 500), b.utm(0, 1500)]),
    ]
    z = b.shapefile_zip(shapefile.POLYLINE, [single, multi])
    info = upload_ok(client, "l.zip", z)
    f = feats(client, info)
    assert [x["geometry_type"] for x in f] == ["LineString", "MultiLineString"]
    assert f[0]["length_m"] == pytest.approx(1000, rel=1e-6)
    assert f[1]["length_m"] == pytest.approx(2000, rel=1e-6)


def test_points_and_multipoints_have_no_measurement(client):
    pts = b.shapefile_zip(shapefile.POINT, [(77.59, 12.97), (77.6, 12.98)])
    ms = upload_and_measure(client, "pts.zip", pts)
    assert [m["geometry_type"] for m in ms] == ["Point", "Point"]
    assert {m["status"] for m in ms} == {"NOT_APPLICABLE"}

    multi = b.shapefile_zip(shapefile.MULTIPOINT, [[(77.59, 12.97), (77.6, 12.98)]])
    (m,) = upload_and_measure(client, "mp.zip", multi)
    assert m["geometry_type"] == "MultiPoint" and m["status"] == "NOT_APPLICABLE"


def test_null_shape_is_an_empty_feature_not_a_crash(client):
    ring = b.cw(b.to_lonlat(b.square(0, 0, 1000)))
    z = b.shapefile_zip(shapefile.POLYGON, [None, [ring]])
    info = upload_ok(client, "n.zip", z)
    assert info["feature_count"] == 2
    ms = measurements(client, info["id"])["measurements"]
    assert [m["status"] for m in ms] == ["EMPTY", "MEASURED"]


# --------------------------------------------------------------------------- #
# Attributes / encodings
# --------------------------------------------------------------------------- #
def test_attribute_types_roundtrip(client):
    fields = (
        ("name", "C", 60, 0),
        ("plot_no", "N", 10, 0),
        ("depth_m", "F", 12, 3),
        ("surveyed", "D", 8, 0),
        ("active", "L", 1, 0),
    )
    ring = b.cw(b.to_lonlat(b.square(0, 0, 100)))
    records = [("ಬೆಂಗಳೂರು ಉತ್ತರ", 7, 1.25, [2026, 3, 9], True), ("blank", None, None, None, None)]
    z = b.shapefile_zip(
        shapefile.POLYGON, [[ring], [ring]], records=records, fields=fields, cpg="UTF-8"
    )
    info = upload_ok(client, "attrs.zip", z)
    first, second = feats(client, info)
    assert first["properties"] == {
        "name": "ಬೆಂಗಳೂರು ಉತ್ತರ",
        "plot_no": 7,
        "depth_m": 1.25,
        "surveyed": "2026-03-09",
        "active": True,
    }
    assert second["properties"]["name"] == "blank"
    assert second["properties"]["plot_no"] is None


def test_cpg_selects_legacy_encoding(client):
    ring = b.cw(b.to_lonlat(b.square(0, 0, 100)))
    z = b.shapefile_zip(
        shapefile.POLYGON,
        [[ring]],
        records=[("Café Zürich",)],
        encoding="latin-1",
        cpg="ISO-8859-1",
    )
    info = upload_ok(client, "legacy.zip", z)
    assert feats(client, info)[0]["properties"]["name"] == "Café Zürich"


# --------------------------------------------------------------------------- #
# CRS handling
# --------------------------------------------------------------------------- #
def test_projected_utm_prj_is_recognised_and_measured_in_place(client):
    z = b.shapefile_zip(shapefile.POLYGON, [[square_ring()]], prj=UTM_PRJ)
    info = upload_ok(client, "utm.zip", z)
    assert info["crs"] == "EPSG:32643"
    (m,) = measurements(client, info["id"])["measurements"]
    assert m["crs"] == "EPSG:32643"
    assert m["area_m2"] == pytest.approx(1_000_000, rel=1e-9)
    assert m["method"]["strategy"] == "source_projected"


def test_esri_flavoured_utm_prj(client):
    esri = CRS.from_epsg(32643).to_wkt(version="WKT1_ESRI")
    z = b.shapefile_zip(shapefile.POLYGON, [[square_ring()]], prj=esri)
    info = upload_ok(client, "esri.zip", z)
    assert info["crs"] == "EPSG:32643"


def test_us_survey_feet_prj_is_converted_to_metres(client):
    prj = CRS.from_epsg(2263).to_wkt()
    x, y = 990_000.0, 200_000.0
    ring = b.cw([(x, y), (x + 100, y), (x + 100, y + 100), (x, y + 100), (x, y)])
    z = b.shapefile_zip(shapefile.POLYGON, [[ring]], prj=prj)
    info = upload_ok(client, "ny.zip", z)
    assert info["crs"] == "EPSG:2263"
    (m,) = measurements(client, info["id"])["measurements"]
    assert m["area_m2"] == pytest.approx(10_000 * (1200 / 3937) ** 2, rel=1e-6)


def test_web_mercator_shapefile_is_not_measured_in_mercator_metres(client):
    to_merc = Transformer.from_crs(4326, 3857, always_xy=True)
    lonlat = [(10.0, 60.0), (10.2, 60.0), (10.2, 60.1), (10.0, 60.1), (10.0, 60.0)]
    merc = b.cw([to_merc.transform(x, y) for x, y in lonlat])
    z = b.shapefile_zip(shapefile.POLYGON, [[merc]], prj=CRS.from_epsg(3857).to_wkt())
    (m,) = upload_and_measure(client, "merc.zip", z)
    expected = b.to_lonlat  # noqa: F841 - documents that truth comes from the lon/lat box
    # 0.2° x 0.1° at 60°N: ≈ 11.1 km * 11.1 km * cos(60°) * 2 ... about 1.1e8 m²; Mercator says ~4x more
    assert 1.0e8 < m["area_m2"] < 1.3e8
    assert m["method"]["strategy"] == "utm"
    assert m["method"]["geodesic_check"]["max_deviation_pct"] < 0.5


def test_missing_prj_with_lonlat_coordinates_assumes_wgs84_and_warns(client):
    ring = b.cw(b.to_lonlat(b.square(0, 0, 1000)))
    z = b.shapefile_zip(shapefile.POLYGON, [[ring]], prj=None)
    info = upload_ok(client, "noprj.zip", z)
    assert info["crs"] == "EPSG:4326"
    assert any("assumed EPSG:4326" in w for w in info["warnings"])
    (m,) = measurements(client, info["id"])["measurements"]
    assert m["area_m2"] == pytest.approx(1_000_000, rel=1e-6)


def test_missing_prj_with_projected_coordinates_is_rejected(client):
    z = b.shapefile_zip(shapefile.POLYGON, [[square_ring()]], prj=None)
    response = upload(client, "noprj.zip", z)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "MISSING_CRS" and ".prj" in body["detail"]


def test_garbage_prj_is_rejected(client):
    z = b.shapefile_zip(shapefile.POLYGON, [[square_ring()]], prj="this is not wkt")
    response = upload(client, "badprj.zip", z)
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_PRJ"


def test_coordinates_outside_lonlat_range_in_a_geographic_crs_become_feature_errors(client):
    ring = b.cw([(0, 80), (10, 80), (10, 100), (0, 100), (0, 80)])
    z = b.shapefile_zip(shapefile.POLYGON, [[ring]])  # WGS84 prj but lat > 90
    info = upload_ok(client, "bad.zip", z)
    (m,) = measurements(client, info["id"])["measurements"]
    assert m["status"] == "ERROR"


# --------------------------------------------------------------------------- #
# Archive layouts
# --------------------------------------------------------------------------- #
def test_shapefile_inside_a_folder_with_uppercase_names_and_macos_junk(client):
    ring = b.cw(b.to_lonlat(b.square(0, 0, 1000)))
    raw = b.shapefile_zip(
        shapefile.POLYGON,
        [[ring]],
        name="DATA",
        folder="export/2026",
        extra_members={"__MACOSX/export/._DATA.shp": b"junk", ".DS_Store": b"junk", "notes.txt": b"hi"},
    )
    # shout the extensions
    src = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            dst.writestr(item.filename.replace(".shp", ".SHP").replace(".dbf", ".DBF"), src.read(item))
    (m,) = upload_and_measure(client, "x.zip", out.getvalue())
    assert m["layer"] == "data" or m["layer"] == "DATA".lower()
    assert m["area_m2"] == pytest.approx(1_000_000, rel=1e-6)


def test_missing_dbf_gives_empty_properties_and_a_warning(client):
    ring = b.cw(b.to_lonlat(b.square(0, 0, 1000)))
    z = b.shapefile_zip(shapefile.POLYGON, [[ring]], include_dbf=False)
    info = upload_ok(client, "nodbf.zip", z)
    assert any(".dbf" in w for w in info["warnings"])
    (f,) = feats(client, info)
    assert f["properties"] == {} and f["status"] == "MEASURED"


def test_missing_shx_is_tolerated(client):
    ring = b.cw(b.to_lonlat(b.square(0, 0, 1000)))
    z = b.shapefile_zip(shapefile.POLYGON, [[ring]], include_shx=False)
    info = upload_ok(client, "noshx.zip", z)
    assert info["feature_count"] == 1


def test_several_layers_in_one_zip(client):
    ring = b.cw(b.to_lonlat(b.square(0, 0, 1000)))
    first = b.shapefile_zip(shapefile.POLYGON, [[ring], [ring]], name="alpha")
    second = b.shapefile_zip(shapefile.POLYLINE, [[b.to_lonlat([b.utm(0, 0), b.utm(500, 0)])]], name="beta")
    merged = io.BytesIO()
    with zipfile.ZipFile(merged, "w") as out:
        for blob in (first, second):
            with zipfile.ZipFile(io.BytesIO(blob)) as src:
                for item in src.infolist():
                    out.writestr(item.filename, src.read(item))
    info = upload_ok(client, "multi.zip", merged.getvalue())
    assert info["feature_count"] == 3
    assert info["geometry_types"] == {"Polygon": 2, "LineString": 1}
    got = feats(client, info)
    assert [f["layer"] for f in got] == ["alpha", "alpha", "beta"]
    assert [f["index"] for f in got] == [0, 1, 2]
    assert [f["source_id"] for f in got] == ["0", "1", "0"]


def test_layers_with_different_crs_report_mixed(client):
    wgs = b.shapefile_zip(
        shapefile.POLYGON, [[b.cw(b.to_lonlat(b.square(0, 0, 1000)))]], name="geo"
    )
    utm = b.shapefile_zip(shapefile.POLYGON, [[square_ring()]], prj=UTM_PRJ, name="proj")
    merged = io.BytesIO()
    with zipfile.ZipFile(merged, "w") as out:
        for blob in (wgs, utm):
            with zipfile.ZipFile(io.BytesIO(blob)) as src:
                for item in src.infolist():
                    out.writestr(item.filename, src.read(item))
    info = upload_ok(client, "mixed.zip", merged.getvalue())
    assert info["crs"] == "MIXED"
    got = feats(client, info)
    assert {f["crs"] for f in got} == {"EPSG:4326", "EPSG:32643"}
    # both describe the same 1 km square
    assert got[0]["area_m2"] == pytest.approx(got[1]["area_m2"], rel=1e-6)


# --------------------------------------------------------------------------- #
# Invalid archives
# --------------------------------------------------------------------------- #
def test_zip_without_shp(client):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("readme.txt", "hello")
    response = upload(client, "x.zip", out.getvalue())
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_SHAPEFILE"


def test_not_a_zip(client):
    response = upload(client, "x.zip", b"definitely not a zip file")
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_ZIP"


def test_garbage_shp_inside_zip(client):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("layer.shp", b"this is not a shapefile at all" * 10)
        z.writestr("layer.dbf", b"nope")
    response = upload(client, "x.zip", out.getvalue())
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_SHAPEFILE"


def test_truncated_shp_never_causes_a_server_error(client):
    ring = b.cw(b.to_lonlat(b.square(0, 0, 1000)))
    good = b.shapefile_zip(shapefile.POLYGON, [[ring]] * 20)
    with zipfile.ZipFile(io.BytesIO(good)) as src:
        shp = src.read("layer.shp")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("layer.shp", shp[: len(shp) // 2])
        z.writestr("layer.prj", b.WGS84_PRJ)
    response = upload(client, "cut.zip", out.getvalue())
    assert response.status_code in (201, 422)  # partial data or a clean rejection - never a 500


def test_corrupt_zip_member(client):
    ring = b.cw(b.to_lonlat(b.square(0, 0, 1000)))
    good = bytearray(b.shapefile_zip(shapefile.POLYGON, [[ring]]))
    middle = len(good) // 2
    good[middle : middle + 40] = b"\x00" * 40
    response = upload(client, "corrupt.zip", bytes(good))
    assert response.status_code in (201, 422)
    assert response.status_code != 500


# --------------------------------------------------------------------------- #
# Hostile archives
# --------------------------------------------------------------------------- #
def test_zip_bomb_is_rejected_by_the_decompression_budget(make_client):
    client = make_client(max_uncompressed_bytes=1_000_000)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("layer.shp", b"\x00" * 20_000_000)  # 20 MB of zeros -> a few KB zipped
    assert len(out.getvalue()) < 100_000
    response = upload(client, "bomb.zip", out.getvalue())
    assert response.status_code == 422
    assert response.json()["code"] == "ZIP_TOO_LARGE"


def test_zip_with_too_many_members_is_rejected(make_client):
    client = make_client(max_zip_members=10)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for i in range(50):
            z.writestr(f"f{i}.txt", "x")
    response = upload(client, "many.zip", out.getvalue())
    assert response.status_code == 422
    assert response.json()["code"] == "ZIP_TOO_LARGE"


def test_zip_slip_names_never_touch_the_filesystem(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ring = b.cw(b.to_lonlat(b.square(0, 0, 1000)))
    raw = b.shapefile_zip(shapefile.POLYGON, [[ring]], name="../../../evil")
    response = upload(client, "slip.zip", raw)
    assert response.status_code in (201, 422)
    assert not list(tmp_path.rglob("evil*"))
    assert not any(p.name.startswith("evil") for p in tmp_path.parent.glob("evil*"))


def test_feature_limit_is_enforced_for_shapefiles(make_client):
    client = make_client(max_features=3)
    ring = b.cw(b.to_lonlat(b.square(0, 0, 100)))
    response = upload(client, "big.zip", b.shapefile_zip(shapefile.POLYGON, [[ring]] * 5))
    assert response.status_code == 422
    assert response.json()["code"] == "TOO_MANY_FEATURES"
