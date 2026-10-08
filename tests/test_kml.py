"""KML / KMZ parsing, through the public API."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.parsers.kml_parser import parse_kml
from tests import builders as b
from tests.helpers import measurements, upload, upload_and_measure, upload_ok

SQ = b.to_lonlat(b.square(0, 0, 1000))
LINE = b.to_lonlat([b.utm(0, 0), b.utm(2000, 0)])


def features(client, kml: bytes, filename: str = "t.kml"):
    info = upload_ok(client, filename, kml)
    response = client.get(f"/api/files/{info['id']}/features/")
    assert response.status_code == 200
    return info, response.json()["features"]


# --------------------------------------------------------------------------- #
# Document structure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "namespace",
    [b.KML_NS, "http://earth.google.com/kml/2.1", "http://www.opengis.net/kml/2.3", None],
)
def test_namespace_variants(client, namespace):
    kml = b.kml_document(b.placemark("a", b.kml_polygon(SQ)), namespace=namespace)
    result = upload_and_measure(client, "t.kml", kml)
    assert result[0]["area_m2"] == pytest.approx(1_000_000, rel=1e-6)


def test_nested_folders_become_layers(client):
    body = (
        "<Folder><name>Parcels</name>"
        + b.placemark("p1", b.kml_polygon(SQ))
        + "<Folder><name>Sub</name>"
        + b.placemark("p2", b.kml_polygon(SQ))
        + "</Folder></Folder>"
        + b.placemark("loose", b.kml_point(SQ[0]))
    )
    _, feats = features(client, b.kml_document(body))
    assert [f["layer"] for f in feats] == ["Parcels", "Parcels / Sub", None]
    assert [f["index"] for f in feats] == [0, 1, 2]


def test_placemark_id_becomes_source_id(client):
    kml = b.kml_document(b.placemark("a", b.kml_point(SQ[0]), pid="plot-7"))
    _, feats = features(client, kml)
    assert feats[0]["source_id"] == "plot-7"


def test_every_feature_reports_index_type_geometry_crs_properties(client):
    kml = b.kml_document(
        b.placemark("Plot A", b.kml_polygon(SQ), "<description>North field</description>")
    )
    info, feats = features(client, kml)
    f = feats[0]
    assert f["index"] == 0
    assert f["geometry_type"] == "Polygon"
    assert f["crs"] == "EPSG:4326"
    assert f["geometry"]["type"] == "Polygon"
    assert f["geometry"]["coordinates"][0][0] == pytest.approx(list(SQ[0]))
    assert f["properties"] == {"name": "Plot A", "description": "North field"}
    assert info["crs"] == "EPSG:4326" and info["feature_count"] == 1


# --------------------------------------------------------------------------- #
# Geometry kinds
# --------------------------------------------------------------------------- #
def test_polygon_with_hole(client):
    hole = b.to_lonlat(b.square(250, 250, 500))
    kml = b.kml_document(b.placemark("donut", b.kml_polygon(SQ, [hole])))
    (m,) = upload_and_measure(client, "t.kml", kml)
    assert m["area_m2"] == pytest.approx(1_000_000 - 250_000, rel=1e-6)


def test_unclosed_ring_is_closed_automatically(client):
    open_ring = SQ[:-1]
    kml = b.kml_document(b.placemark("open", b.kml_polygon(open_ring)))
    (m,) = upload_and_measure(client, "t.kml", kml)
    assert m["status"] == "MEASURED"
    assert m["area_m2"] == pytest.approx(1_000_000, rel=1e-6)


def test_altitude_is_ignored(client):
    kml = b.kml_document(
        "<Placemark><name>x</name><Polygon><outerBoundaryIs><LinearRing><coordinates>"
        + b.coords_text(SQ, altitude=912.5)
        + "</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    )
    _, (f,) = features(client, kml)
    assert all(len(c) == 2 for c in f["geometry"]["coordinates"][0])
    assert f["area_m2"] == pytest.approx(1_000_000, rel=1e-6)


def test_sloppy_coordinate_whitespace(client):
    text = "\n   " + "\n   ".join(f"{x:.9f}, {y:.9f} , 0" for x, y in LINE) + "\n"
    kml = b.kml_document(
        f"<Placemark><LineString><coordinates>{text}</coordinates></LineString></Placemark>"
    )
    (m,) = upload_and_measure(client, "t.kml", kml)
    assert m["length_m"] == pytest.approx(2000, rel=1e-6)


def test_multigeometry_of_polygons_is_a_multipolygon(client):
    second = b.to_lonlat(b.square(2000, 0, 500))
    geometry = (
        "<MultiGeometry>" + b.kml_polygon(SQ) + b.kml_polygon(second) + "</MultiGeometry>"
    )
    _, (f,) = features(client, b.kml_document(b.placemark("multi", geometry)))
    assert f["geometry_type"] == "MultiPolygon"
    assert f["area_m2"] == pytest.approx(1_250_000, rel=1e-6)


def test_multigeometry_of_lines_and_points(client):
    lines = "<MultiGeometry>" + b.kml_line(LINE) + b.kml_line(LINE) + "</MultiGeometry>"
    points = "<MultiGeometry>" + b.kml_point(SQ[0]) + b.kml_point(SQ[1]) + "</MultiGeometry>"
    _, feats = features(
        client, b.kml_document(b.placemark("l", lines) + b.placemark("p", points))
    )
    assert [f["geometry_type"] for f in feats] == ["MultiLineString", "MultiPoint"]
    assert feats[0]["length_m"] == pytest.approx(4000, rel=1e-6)
    assert feats[1]["status"] == "NOT_APPLICABLE"


def test_mixed_multigeometry_is_a_collection_with_both_measurements(client):
    geometry = (
        "<MultiGeometry>" + b.kml_polygon(SQ) + b.kml_line(LINE) + b.kml_point(SQ[0])
        + "</MultiGeometry>"
    )
    _, (f,) = features(client, b.kml_document(b.placemark("mix", geometry)))
    assert f["geometry_type"] == "GeometryCollection"
    assert f["area_m2"] == pytest.approx(1_000_000, rel=1e-6)
    assert f["length_m"] == pytest.approx(2000, rel=1e-6)


def test_nested_multigeometry_flattens(client):
    inner = "<MultiGeometry>" + b.kml_polygon(SQ) + "</MultiGeometry>"
    outer = "<MultiGeometry>" + inner + b.kml_polygon(b.to_lonlat(b.square(3000, 0, 500))) + "</MultiGeometry>"
    _, (f,) = features(client, b.kml_document(b.placemark("n", outer)))
    assert f["geometry_type"] == "MultiPolygon"
    assert f["area_m2"] == pytest.approx(1_250_000, rel=1e-6)


def test_standalone_linear_ring(client):
    ring = (
        "<LinearRing><coordinates>" + b.coords_text(SQ) + "</coordinates></LinearRing>"
    )
    _, (f,) = features(client, b.kml_document(b.placemark("ring", ring)))
    assert f["geometry_type"] == "LinearRing"
    assert f["length_m"] == pytest.approx(4000, rel=1e-6)


def test_gx_track_is_a_line(client):
    coords = "".join(f"<gx:coord>{x:.9f} {y:.9f} 0</gx:coord>" for x, y in LINE)
    track = f"<gx:Track>{coords}</gx:Track>"
    kml = b.kml_document(
        b.placemark("gps", track), extra_root=' xmlns:gx="http://www.google.com/kml/ext/2.2"'
    )
    _, (f,) = features(client, kml)
    assert f["geometry_type"] == "LineString"
    assert f["length_m"] == pytest.approx(2000, rel=1e-6)


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #
def test_extended_data_and_typed_schema_data(client):
    extra = (
        "<ExtendedData>"
        '<Data name="owner"><value>Asha</value></Data>'
        '<SchemaData schemaUrl="#parcels">'
        '<SimpleData name="plot_no">42</SimpleData>'
        '<SimpleData name="yield_t">3.5</SimpleData>'
        '<SimpleData name="irrigated">true</SimpleData>'
        '<SimpleData name="note">plain text</SimpleData>'
        "</SchemaData></ExtendedData>"
    )
    schema = (
        '<Schema name="parcels" id="parcels">'
        '<SimpleField type="int" name="plot_no"/>'
        '<SimpleField type="double" name="yield_t"/>'
        '<SimpleField type="bool" name="irrigated"/>'
        '<SimpleField type="string" name="note"/></Schema>'
    )
    kml = b.kml_document(schema + b.placemark("p", b.kml_polygon(SQ), extra))
    _, (f,) = features(client, kml)
    assert f["properties"] == {
        "name": "p",
        "owner": "Asha",
        "plot_no": 42,
        "yield_t": 3.5,
        "irrigated": True,
        "note": "plain text",
    }


def test_unicode_properties_survive(client):
    kml = b.kml_document(b.placemark("ಬೆಂಗಳೂರು ವಾರ್ಡ್ १२", b.kml_point(SQ[0])))
    _, (f,) = features(client, kml)
    assert f["properties"]["name"] == "ಬೆಂಗಳೂರು ವಾರ್ಡ್ १२"


# --------------------------------------------------------------------------- #
# Graceful handling of unsupported / broken geometries
# --------------------------------------------------------------------------- #
def test_unsupported_and_empty_placemarks_do_not_crash_the_upload(client):
    body = (
        b.placemark("good", b.kml_polygon(SQ))
        + "<Placemark><name>3d</name><Model><Location/></Model></Placemark>"
        + "<Placemark><name>nothing</name></Placemark>"
        + b.placemark("pt", b.kml_point(SQ[0]))
    )
    info = upload_ok(client, "t.kml", b.kml_document(body))
    assert info["status"] == "COMPLETED" and info["feature_count"] == 4
    data = measurements(client, info["id"])
    assert [m["status"] for m in data["measurements"]] == [
        "MEASURED",
        "UNSUPPORTED",
        "EMPTY",
        "NOT_APPLICABLE",
    ]
    assert data["measurements"][1]["geometry_type"] == "Model"
    assert data["measurements"][1]["warnings"]
    assert data["summary"] == {
        "measured": 1,
        "not_applicable": 1,
        "unsupported": 1,
        "empty": 1,
        "errors": 0,
        "total_area_m2": pytest.approx(1_000_000, rel=1e-6),
        "total_perimeter_m": pytest.approx(4000, rel=1e-6),
        "total_length_m": 0.0,
    }


@pytest.mark.parametrize(
    "coordinates",
    [
        "77.5,abc",  # not numeric
        "77.5",  # missing latitude
        "77.5,95.0 77.6,13.0",  # latitude out of range
        "200.0,10.0 201.0,11.0",  # longitude out of range
        "nan,12 77,13",
        "",
    ],
)
def test_bad_coordinates_only_break_their_own_feature(client, coordinates):
    bad = f"<Placemark><name>bad</name><LineString><coordinates>{coordinates}</coordinates></LineString></Placemark>"
    kml = b.kml_document(bad + b.placemark("good", b.kml_line(LINE)))
    info = upload_ok(client, "t.kml", kml)
    assert info["status"] == "COMPLETED"
    data = measurements(client, info["id"])["measurements"]
    assert data[0]["status"] == "ERROR" and data[0]["warnings"]
    assert data[1]["status"] == "MEASURED"


def test_degenerate_polygon_is_an_error_feature(client):
    two_points = "77.5,12.9 77.6,13.0 77.5,12.9"
    kml = b.kml_document(
        "<Placemark><Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{two_points}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    )
    (m,) = upload_and_measure(client, "t.kml", kml)
    assert m["status"] == "ERROR"


# --------------------------------------------------------------------------- #
# Whole-file failures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"this is not xml", "INVALID_KML"),
        (b"<kml><Document><Placemark></kml>", "INVALID_KML"),
        (b"<?xml version='1.0'?><gpx><trk/></gpx>", "INVALID_KML"),
        (b.kml_document(""), "NO_FEATURES"),
    ],
)
def test_unusable_kml_is_rejected_with_422(client, content, code):
    response = upload(client, "t.kml", content)
    assert response.status_code == 422
    assert response.json()["code"] == code
    assert response.json()["id"]


def test_xml_entity_attacks_are_rejected(client):
    billion_laughs = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
        b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
        b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>&lol3;</name>'
        b"</Placemark></Document></kml>"
    )
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>&xxe;</name>'
        b"</Placemark></Document></kml>"
    )
    for payload in (billion_laughs, xxe):
        response = upload(client, "evil.kml", payload)
        assert response.status_code == 422
        assert response.json()["code"] == "UNSAFE_XML"


def test_zip_disguised_as_kml_is_rejected(client):
    response = upload(client, "t.kml", b.kmz_bytes(b.kml_document(b.placemark("a", b.kml_point(SQ[0])))))
    assert response.status_code == 422
    assert "kmz" in response.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# KMZ
# --------------------------------------------------------------------------- #
def test_kmz_upload(client):
    kml = b.kml_document(b.placemark("a", b.kml_polygon(SQ)))
    info = upload_ok(client, "plots.kmz", b.kmz_bytes(kml))
    assert info["file_type"] == "kmz" and info["feature_count"] == 1


def test_zip_containing_only_a_kml_is_accepted(client):
    kml = b.kml_document(b.placemark("a", b.kml_polygon(SQ)))
    info = upload_ok(client, "plots.zip", b.kmz_bytes(kml, name="folder/survey.kml"))
    assert info["feature_count"] == 1


def test_kmz_without_a_kml_is_rejected(client):
    response = upload(client, "x.kmz", b.kmz_bytes(b"hello", name="readme.txt"))
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Direct parser unit checks
# --------------------------------------------------------------------------- #
def test_parser_limits_feature_count():
    kml = b.kml_document("".join(b.placemark(str(i), b.kml_point(SQ[0])) for i in range(5)))
    with pytest.raises(Exception) as excinfo:
        parse_kml(kml, max_features=3)
    assert getattr(excinfo.value, "code", None) == "TOO_MANY_FEATURES"


def test_settings_defaults_are_sane():
    s = Settings()
    assert s.max_upload_bytes >= 1024 * 1024 and s.max_features > 1000
