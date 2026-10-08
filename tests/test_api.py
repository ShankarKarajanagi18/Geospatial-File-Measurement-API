"""HTTP contract of the /api/files endpoints (what a client or reviewer sees)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests import builders as b
from tests.helpers import measurements, upload, upload_ok

SAMPLES = Path(__file__).resolve().parents[1] / "sample_data"


def _survey_kml() -> bytes:
    """Two polygons, one line, two points; all dimensions known exactly."""
    square = b.kml_polygon(b.to_lonlat(b.square(0, 0, 1000)))
    big = b.kml_polygon(b.to_lonlat(b.square(2000, 0, 2000)))
    road = b.kml_line(b.to_lonlat([b.utm(0, -100), b.utm(1500, -100)]))
    return b.kml_document(
        b.placemark("Parcel", square, "<ExtendedData><Data name='owner'><value>A</value></Data>"
                                      "</ExtendedData>")
        + b.placemark("Big parcel", big)
        + b.placemark("Road", road)
        + b.placemark("Gate 1", b.kml_point(b.to_lonlat([b.utm(10, 10)])[0]))
        + b.placemark("Gate 2", b.kml_point(b.to_lonlat([b.utm(20, 20)])[0]))
    )


@pytest.fixture
def survey(client: TestClient) -> dict:
    return upload_ok(client, "survey.kml", _survey_kml())


# --------------------------------------------------------------------------- #
# Upload + file information
# --------------------------------------------------------------------------- #
def test_health_and_docs_are_served(client: TestClient):
    assert client.get("/health").json()["status"] == "ok"
    schema = client.get("/openapi.json").json()
    assert "/api/files/" in schema["paths"]
    assert "/api/files/{file_id}/measurements/" in schema["paths"]


def test_upload_returns_the_documented_shape(survey: dict):
    assert survey["filename"] == "survey.kml"
    assert survey["feature_count"] == 5
    assert survey["crs"] == "EPSG:4326"
    assert survey["status"] == "COMPLETED"
    assert survey["file_type"] == "kml"
    assert survey["geometry_types"] == {"Polygon": 2, "LineString": 1, "Point": 2}
    assert survey["error"] is None
    assert len(survey["id"]) == 32


def test_get_file_matches_the_upload_response(client: TestClient, survey: dict):
    response = client.get(f"/api/files/{survey['id']}/")
    assert response.status_code == 200
    assert response.json() == survey


def test_unknown_id_is_a_404_with_a_code(client: TestClient):
    response = client.get("/api/files/does-not-exist/")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    for suffix in ("measurements/", "features/", "features/0/"):
        assert client.get(f"/api/files/does-not-exist/{suffix}").status_code == 404


def test_urls_work_with_and_without_trailing_slash(client: TestClient, survey: dict):
    assert client.get(f"/api/files/{survey['id']}").status_code == 200
    assert client.get(f"/api/files/{survey['id']}/measurements").status_code == 200
    assert upload(client, "again.kml", _survey_kml()).status_code == 201
    assert client.post("/api/files", files={"file": ("n.kml", _survey_kml())}).status_code == 201


def test_uploading_the_same_bytes_twice_makes_two_records(client: TestClient):
    first = upload_ok(client, "a.kml", _survey_kml())
    second = upload_ok(client, "a.kml", _survey_kml())
    assert first["id"] != second["id"]


def test_client_supplied_paths_are_stripped_from_filenames(client: TestClient):
    info = upload_ok(client, "../../etc/evil.kml", _survey_kml())
    assert info["filename"] == "evil.kml"
    info = upload_ok(client, "C:\\Users\\me\\survey.kml", _survey_kml())
    assert info["filename"] == "survey.kml"


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", ["notes.txt", "photo.png", "data.geojson", "noextension"])
def test_unsupported_extensions_are_rejected_with_400(client: TestClient, filename: str):
    response = upload(client, filename, b"hello")
    assert response.status_code == 400
    assert response.json()["code"] == "UNSUPPORTED_FILE_TYPE"
    assert client.get("/api/files/").json()["total"] == 0  # nothing was stored


def test_empty_upload_is_rejected(client: TestClient):
    response = upload(client, "empty.kml", b"")
    assert response.status_code == 400
    assert response.json()["code"] == "EMPTY_FILE"


def test_missing_file_field_is_a_validation_error(client: TestClient):
    response = client.post("/api/files/", data={"x": "y"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_oversized_upload_is_rejected_with_413(make_client):
    client = make_client(max_upload_bytes=500)
    response = upload(client, "big.kml", _survey_kml())
    assert response.status_code == 413
    assert response.json()["code"] == "FILE_TOO_LARGE"


def test_unreadable_file_keeps_a_failed_record(client: TestClient):
    response = upload(client, "broken.kml", b"<kml><Placemark>")
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_KML"
    info = client.get(f"/api/files/{body['id']}/").json()
    assert info["status"] == "FAILED"
    assert info["error"]
    # reading results of a failed file is a clear conflict, not a crash
    conflict = client.get(f"/api/files/{body['id']}/measurements/")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "FILE_PROCESSING_FAILED"


# --------------------------------------------------------------------------- #
# Measurements
# --------------------------------------------------------------------------- #
def test_measurements_have_the_right_values_per_geometry_type(client: TestClient, survey: dict):
    body = measurements(client, survey["id"])
    by_name = {m["index"]: m for m in body["measurements"]}
    assert body["total"] == 5
    assert by_name[0]["area_m2"] == pytest.approx(1_000_000, rel=1e-6)
    assert by_name[0]["perimeter_m"] == pytest.approx(4_000, rel=1e-6)
    assert by_name[1]["area_m2"] == pytest.approx(4_000_000, rel=1e-6)
    assert by_name[2]["length_m"] == pytest.approx(1_500, rel=1e-6)
    for point in (by_name[3], by_name[4]):
        assert point["status"] == "NOT_APPLICABLE"
        assert point["area_m2"] is None and point["length_m"] is None
    assert body["summary"]["measured"] == 3
    assert body["summary"]["not_applicable"] == 2
    assert body["summary"]["total_area_m2"] == pytest.approx(5_000_000, rel=1e-6)
    assert body["summary"]["total_length_m"] == pytest.approx(1_500, rel=1e-6)
    assert body["units"] == {"area": "square metres", "length": "metres"}


def test_degrees_are_never_reported_as_metres(client: TestClient, survey: dict):
    """A 1 km square is ~0.009 degrees wide; a degree-based area would be ~8e-5."""
    first = measurements(client, survey["id"])["measurements"][0]
    assert first["area_m2"] > 900_000
    assert first["method"]["crs"].startswith("EPSG:326")  # a UTM zone


def test_measurement_filters_and_pagination(client: TestClient, survey: dict):
    polygons = measurements(client, survey["id"], geometry_type="polygon")
    assert [m["geometry_type"] for m in polygons["measurements"]] == ["Polygon", "Polygon"]
    assert polygons["summary"]["total_area_m2"] == pytest.approx(5_000_000, rel=1e-6)

    only_points = measurements(client, survey["id"], status="NOT_APPLICABLE")
    assert only_points["total"] == 2

    page = measurements(client, survey["id"], limit=2, offset=1)
    assert [m["index"] for m in page["measurements"]] == [1, 2]
    assert page["total"] == 5  # total ignores paging

    assert client.get(f"/api/files/{survey['id']}/measurements/?status=bogus").status_code == 422
    assert client.get(f"/api/files/{survey['id']}/measurements/?limit=0").status_code == 422


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #
def test_features_expose_geometry_crs_and_properties(client: TestClient, survey: dict):
    body = client.get(f"/api/files/{survey['id']}/features/").json()
    assert body["total"] == 5
    first = body["features"][0]
    assert first["index"] == 0
    assert first["geometry_type"] == "Polygon"
    assert first["crs"] == "EPSG:4326"
    assert first["geometry"]["type"] == "Polygon"
    assert first["properties"] == {"name": "Parcel", "owner": "A"}
    lon, lat = first["geometry"]["coordinates"][0][0]
    assert 77.5 < lon < 77.7 and 12.9 < lat < 13.1  # still lon/lat, not projected


def test_single_feature_lookup(client: TestClient, survey: dict):
    road = client.get(f"/api/files/{survey['id']}/features/2/").json()
    assert road["geometry_type"] == "LineString"
    assert road["length_m"] == pytest.approx(1_500, rel=1e-6)
    missing = client.get(f"/api/files/{survey['id']}/features/99/")
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# List + delete
# --------------------------------------------------------------------------- #
def test_list_files_newest_first_with_paging_and_status_filter(client: TestClient):
    ids = [upload_ok(client, f"f{i}.kml", _survey_kml())["id"] for i in range(3)]
    upload(client, "bad.kml", b"not xml")

    everything = client.get("/api/files/").json()
    assert everything["total"] == 4
    assert everything["files"][0]["filename"] == "bad.kml"

    completed = client.get("/api/files/", params={"status": "COMPLETED"}).json()
    assert completed["total"] == 3
    assert {f["id"] for f in completed["files"]} == set(ids)

    page = client.get("/api/files/", params={"limit": 1, "offset": 1}).json()
    assert len(page["files"]) == 1 and page["total"] == 4


def test_delete_removes_the_file_and_its_features(client: TestClient, survey: dict):
    assert client.delete(f"/api/files/{survey['id']}/").status_code == 204
    assert client.get(f"/api/files/{survey['id']}/").status_code == 404
    assert client.get(f"/api/files/{survey['id']}/measurements/").status_code == 404
    assert client.delete(f"/api/files/{survey['id']}/").status_code == 404


# --------------------------------------------------------------------------- #
# Infrastructure behaviour
# --------------------------------------------------------------------------- #
def test_data_survives_an_application_restart(tmp_path: Path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'db' / 'geo.db'}", log_level="WARNING")
    with TestClient(create_app(settings)) as first:
        file_id = upload_ok(first, "survey.kml", _survey_kml())["id"]
    with TestClient(create_app(settings)) as second:
        assert second.get(f"/api/files/{file_id}/").json()["feature_count"] == 5
        assert measurements(second, file_id)["summary"]["measured"] == 3


def test_concurrent_uploads_do_not_interfere(tmp_path: Path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'geo.db'}", log_level="WARNING")
    with TestClient(create_app(settings)) as client:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda i: upload(client, f"c{i}.kml", _survey_kml()), range(12)))
        assert [r.status_code for r in results] == [201] * 12
        assert len({r.json()["id"] for r in results}) == 12
        assert client.get("/api/files/").json()["total"] == 12


# --------------------------------------------------------------------------- #
# The shipped sample files stay valid (and keep documenting expected numbers)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not SAMPLES.exists(), reason="sample_data not generated")
class TestSampleData:
    def test_survey_kml(self, client: TestClient):
        info = upload_ok(client, "s.kml", (SAMPLES / "bengaluru_survey.kml").read_bytes())
        summary = measurements(client, info["id"])["summary"]
        assert summary["total_area_m2"] == pytest.approx(1_240_000, rel=1e-6)
        assert summary["total_length_m"] == pytest.approx(2_500, rel=1e-6)

    def test_edge_case_kml_degrades_gracefully(self, client: TestClient):
        info = upload_ok(client, "e.kml", (SAMPLES / "edge_cases.kml").read_bytes())
        summary = measurements(client, info["id"])["summary"]
        assert (summary["measured"], summary["unsupported"], summary["empty"]) == (2, 1, 1)
        assert summary["errors"] == 1

    def test_projected_shapefile_is_measured_in_place(self, client: TestClient):
        info = upload_ok(client, "p.zip", (SAMPLES / "parcels_utm43.zip").read_bytes())
        assert info["crs"] == "EPSG:32643"
        items = measurements(client, info["id"])["measurements"]
        assert [m["area_m2"] for m in items] == [pytest.approx(1_000_000), pytest.approx(40_000)]
        assert items[0]["method"]["strategy"] == "source_projected"

    def test_wgs84_shapefile(self, client: TestClient):
        info = upload_ok(client, "d.zip", (SAMPLES / "districts_wgs84.zip").read_bytes())
        (item,) = measurements(client, info["id"])["measurements"]
        assert item["area_m2"] == pytest.approx(4_000_000, rel=1e-6)
