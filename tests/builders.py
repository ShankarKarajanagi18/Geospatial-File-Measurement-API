"""Helpers that build KML documents and zipped shapefiles for tests.

Geometry is generated in a *projected* CRS with exactly known dimensions
(UTM 43N, Bengaluru) and converted to lon/lat, so expected areas and lengths
are known independently of the code under test (e.g. a 1000 m x 1000 m square
must measure 1 000 000 m²).
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable, Sequence

import shapefile
from pyproj import CRS, Transformer

UTM43 = CRS.from_epsg(32643)
WGS84_PRJ = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,'
    '298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)
#: Origin used for all synthetic geometry (easting, northing in EPSG:32643 ≈ 12.97N 77.59E).
ORIGIN = (777_000.0, 1_434_000.0)

_to_lonlat = Transformer.from_crs(UTM43, 4326, always_xy=True)
_from_lonlat = Transformer.from_crs(4326, UTM43, always_xy=True)

XY = tuple[float, float]


def utm(dx: float, dy: float) -> XY:
    """Offset (metres) from ORIGIN -> UTM coordinates."""
    return ORIGIN[0] + dx, ORIGIN[1] + dy


def to_lonlat(points: Iterable[XY]) -> list[XY]:
    return [tuple(_to_lonlat.transform(x, y)) for x, y in points]


def square(dx: float, dy: float, size: float) -> list[XY]:
    """Closed square ring in UTM metres (counter-clockwise)."""
    return [
        utm(dx, dy),
        utm(dx + size, dy),
        utm(dx + size, dy + size),
        utm(dx, dy + size),
        utm(dx, dy),
    ]


# --------------------------------------------------------------------------- #
# KML
# --------------------------------------------------------------------------- #
KML_NS = "http://www.opengis.net/kml/2.2"


def coords_text(points: Sequence[XY], altitude: float | None = None) -> str:
    return " ".join(
        f"{x:.10f},{y:.10f}" + (f",{altitude}" if altitude is not None else "")
        for x, y in points
    )


def kml_polygon(outer: Sequence[XY], holes: Sequence[Sequence[XY]] = ()) -> str:
    inner = "".join(
        f"<innerBoundaryIs><LinearRing><coordinates>{coords_text(h)}</coordinates>"
        "</LinearRing></innerBoundaryIs>"
        for h in holes
    )
    return (
        "<Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{coords_text(outer)}</coordinates>"
        f"</LinearRing></outerBoundaryIs>{inner}</Polygon>"
    )


def kml_line(points: Sequence[XY]) -> str:
    return f"<LineString><coordinates>{coords_text(points)}</coordinates></LineString>"


def kml_point(point: XY) -> str:
    return f"<Point><coordinates>{coords_text([point])}</coordinates></Point>"


def placemark(name: str, geometry: str, extra: str = "", pid: str | None = None) -> str:
    id_attr = f' id="{pid}"' if pid else ""
    return f"<Placemark{id_attr}><name>{name}</name>{extra}{geometry}</Placemark>"


def kml_document(body: str, namespace: str | None = KML_NS, extra_root: str = "") -> bytes:
    xmlns = f' xmlns="{namespace}"' if namespace else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><kml{xmlns}{extra_root}>'
        f"<Document><name>test</name>{body}</Document></kml>"
    ).encode()


def kmz_bytes(kml: bytes, name: str = "doc.kml") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, kml)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Shapefile
# --------------------------------------------------------------------------- #
def shapefile_zip(
    shape_type: int,
    shapes: Sequence,
    records: Sequence[Sequence] | None = None,
    fields: Sequence[tuple] = (("name", "C", 50, 0),),
    prj: str | None = WGS84_PRJ,
    name: str = "layer",
    folder: str = "",
    cpg: str | None = None,
    include_dbf: bool = True,
    include_shx: bool = True,
    encoding: str = "utf-8",
    extra_members: dict[str, bytes] | None = None,
) -> bytes:
    """Create a zipped shapefile.

    ``shapes`` items are: point -> (x, y); multipoint -> [(x, y), ...];
    polyline -> list of parts, each a list of (x, y); polygon -> list of
    rings (outer rings clockwise, holes counter-clockwise); ``None`` -> NULL shape.
    """
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shape_type, encoding=encoding)
    for field in fields:
        writer.field(*field)
    for i, geometry in enumerate(shapes):
        if geometry is None:
            writer.null()
        elif shape_type == shapefile.POINT:
            writer.point(*geometry)
        elif shape_type == shapefile.MULTIPOINT:
            writer.multipoint(geometry)
        elif shape_type == shapefile.POLYLINE:
            writer.line(geometry)
        elif shape_type == shapefile.POLYGON:
            writer.poly(geometry)
        else:  # pragma: no cover
            raise ValueError(shape_type)
        record = records[i] if records else (f"feature {i}",)
        writer.record(*record)
    writer.close()

    prefix = f"{folder}/" if folder else ""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{prefix}{name}.shp", shp.getvalue())
        if include_shx:
            archive.writestr(f"{prefix}{name}.shx", shx.getvalue())
        if include_dbf:
            archive.writestr(f"{prefix}{name}.dbf", dbf.getvalue())
        if prj is not None:
            archive.writestr(f"{prefix}{name}.prj", prj)
        if cpg is not None:
            archive.writestr(f"{prefix}{name}.cpg", cpg)
        for member, data in (extra_members or {}).items():
            archive.writestr(member, data)
    return buffer.getvalue()


def cw(ring: Sequence[XY]) -> list[XY]:
    """Orient a ring clockwise (shapefile outer-ring convention)."""
    area2 = sum(
        ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)
    )
    return list(ring) if area2 < 0 else list(reversed(ring))


def ccw(ring: Sequence[XY]) -> list[XY]:
    return list(reversed(cw(ring)))
