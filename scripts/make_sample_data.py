"""Generate the demo files in ``sample_data/``.

The geometry is built in UTM 43N (Bengaluru) with exactly known dimensions and
converted to longitude/latitude, so the expected measurements are known
independently of the service. Run from the repository root::

    python scripts/make_sample_data.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import shapefile  # noqa: E402  (pyshp)
from pyproj import CRS  # noqa: E402

from tests import builders as b  # noqa: E402

OUT = ROOT / "sample_data"


def bengaluru_survey() -> bytes:
    """KML in EPSG:4326 with folders, a holed polygon, a road, points and metadata."""
    parcel_a = b.square(0, 0, 1000)  # 1 000 000 m2, perimeter 4 000 m
    parcel_b = b.square(1500, 0, 500)  # 250 000 m2 ...
    hole = b.square(1600, 100, 100)  # ... minus 10 000 m2 = 240 000 m2
    road = [b.utm(0, -200), b.utm(1500, -200), b.utm(1500, -1200)]  # 2 500 m
    pois = [b.utm(500, 500), b.utm(1750, 250)]

    def folder(name: str, body: str) -> str:
        return f"<Folder><name>{name}</name>{body}</Folder>"

    parcels = folder(
        "Parcels",
        b.placemark(
            "Parcel A",
            b.kml_polygon(b.to_lonlat(parcel_a)),
            "<ExtendedData><Data name='owner'><value>Asha Rao</value></Data>"
            "<Data name='survey_no'><value>42/1</value></Data></ExtendedData>",
            pid="parcel-a",
        )
        + b.placemark(
            "Parcel B (with courtyard)",
            b.kml_polygon(b.to_lonlat(parcel_b), [b.to_lonlat(hole)]),
            "<ExtendedData><Data name='owner'><value>Vikram Iyer</value></Data></ExtendedData>",
            pid="parcel-b",
        ),
    )
    roads = folder(
        "Roads", b.placemark("Service road", b.kml_line(b.to_lonlat(road)), pid="road-1")
    )
    landmarks = folder(
        "Landmarks",
        "".join(
            b.placemark(f"Gate {i + 1}", b.kml_point(pt))
            for i, pt in enumerate(b.to_lonlat(pois))
        ),
    )
    return b.kml_document(parcels + roads + landmarks)


def edge_cases() -> bytes:
    """KML that exercises graceful degradation."""
    bowtie = [b.utm(0, 0), b.utm(100, 100), b.utm(100, 0), b.utm(0, 100), b.utm(0, 0)]
    body = (
        b.placemark("Fine square (100 m)", b.kml_polygon(b.to_lonlat(b.square(0, 0, 100))))
        + b.placemark("Self-intersecting bow-tie", b.kml_polygon(b.to_lonlat(bowtie)))
        + b.placemark(
            "3D model (unsupported)",
            "<Model><Location><longitude>77.59</longitude><latitude>12.97</latitude>"
            "</Location></Model>",
        )
        + b.placemark("No geometry at all", "")
        + b.placemark("Bad coordinates", "<Point><coordinates>abc,def</coordinates></Point>")
    )
    return b.kml_document(body)


def parcels_utm43() -> bytes:
    """Shapefile already in a projected CRS (EPSG:32643), 2 polygons."""
    prj = CRS.from_epsg(32643).to_wkt()
    square_1 = b.cw(b.square(0, 0, 1000))
    square_2 = b.cw(b.square(2000, 0, 200))
    return b.shapefile_zip(
        shapefile.POLYGON,
        [[square_1], [square_2]],
        records=[("Block 1",), ("Block 2",)],
        prj=prj,
        name="parcels_utm43",
    )


def districts_wgs84() -> bytes:
    """Shapefile in EPSG:4326: a single 2 km x 2 km polygon."""
    return b.shapefile_zip(
        shapefile.POLYGON,
        [[b.cw(b.to_lonlat(b.square(0, 0, 2000)))]],
        records=[("District 1",)],
        name="districts_wgs84",
    )


def main() -> None:
    OUT.mkdir(exist_ok=True)
    files = {
        "bengaluru_survey.kml": bengaluru_survey(),
        "edge_cases.kml": edge_cases(),
        "parcels_utm43.zip": parcels_utm43(),
        "districts_wgs84.zip": districts_wgs84(),
    }
    for name, data in files.items():
        (OUT / name).write_bytes(data)
        print(f"wrote sample_data/{name} ({len(data):,} bytes)")


if __name__ == "__main__":
    main()
