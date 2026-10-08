"""KML / KMZ parser.

* XML is parsed with ``defusedxml`` (no entity expansion, no DTDs, no external
  resources), so billion-laughs and XXE payloads are rejected.
* KML is always WGS84 longitude/latitude (EPSG:4326) by specification.
* Handles ``Point``, ``LineString``, ``LinearRing``, ``Polygon`` (with holes),
  ``MultiGeometry`` (any mix -> Multi* or GeometryCollection) and Google's
  ``gx:Track`` / ``gx:MultiTrack`` extension.
* Placemarks that hold something else (``Model``, or nothing at all) are kept
  as features with ``geometry=None`` so nothing silently disappears.
* ``name``, ``description``, ``ExtendedData/Data`` and
  ``ExtendedData/SchemaData/SimpleData`` become feature properties; typed
  ``Schema`` fields are cast to int / float / bool.
* Elevation is ignored: the service works in plan view.
"""

from __future__ import annotations

import io
import math
import re
import zipfile
from typing import Any
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as SafeET
from defusedxml import DefusedXmlException
from shapely.geometry import (
    GeometryCollection,
    LinearRing,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry

from app.domain import ParsedDataset, ParsedFeature
from app.exceptions import InvalidGeospatialFile
from app.services import crs as crs_service

_GEOMETRY_TAGS = {
    "Point",
    "LineString",
    "LinearRing",
    "Polygon",
    "MultiGeometry",
    "Track",
    "MultiTrack",
}
_UNSUPPORTED_TAGS = {"Model"}
_COMMA_SPACING = re.compile(r"\s*,\s*")
_INT_TYPES = {"int", "uint", "short", "ushort"}
_FLOAT_TYPES = {"float", "double"}


def _strip_namespaces(root: Element) -> None:
    for element in root.iter():
        tag = element.tag
        if isinstance(tag, str) and "}" in tag:
            element.tag = tag.rsplit("}", 1)[1]


def _text(element: Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _child(element: Element, tag: str) -> Element | None:
    return element.find(tag)


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def _parse_coordinates(text: str | None) -> list[tuple[float, float]]:
    if not text or not text.strip():
        raise ValueError("empty <coordinates>")
    coords: list[tuple[float, float]] = []
    for token in _COMMA_SPACING.sub(",", text.strip()).split():
        pieces = token.split(",")
        if len(pieces) < 2:
            raise ValueError(f"malformed coordinate tuple {token!r}")
        try:
            lon, lat = float(pieces[0]), float(pieces[1])
        except ValueError as exc:
            raise ValueError(f"non-numeric coordinate {token!r}") from exc
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise ValueError(f"non-finite coordinate {token!r}")
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise ValueError(f"coordinate out of range: lon={lon}, lat={lat}")
        coords.append((lon, lat))
    return coords


def _ring(element: Element | None) -> list[tuple[float, float]]:
    if element is None:
        raise ValueError("polygon boundary has no <LinearRing>")
    coords = _parse_coordinates(_text(_child(element, "coordinates")))
    if coords[0] != coords[-1]:
        coords.append(coords[0])  # tolerate unclosed rings
    if len(coords) < 4:
        raise ValueError("a polygon ring needs at least 3 distinct vertices")
    return coords


def _polygon(element: Element) -> Polygon:
    outer = _child(element, "outerBoundaryIs")
    if outer is None:
        raise ValueError("<Polygon> has no <outerBoundaryIs>")
    shell = _ring(_child(outer, "LinearRing"))
    holes = [_ring(_child(inner, "LinearRing")) for inner in element.findall("innerBoundaryIs")]
    return Polygon(shell, holes)


def _track_coords(element: Element) -> list[tuple[float, float]]:
    coords: list[tuple[float, float]] = []
    for coord in element.findall("coord"):
        pieces = (coord.text or "").split()
        if len(pieces) < 2:
            raise ValueError("malformed <gx:coord>")
        coords.append((float(pieces[0]), float(pieces[1])))
    if len(coords) < 2:
        raise ValueError("a track needs at least two <gx:coord> values")
    return coords


def _geometry(element: Element) -> BaseGeometry:
    tag = element.tag
    if tag == "Point":
        coords = _parse_coordinates(_text(_child(element, "coordinates")))
        return Point(coords[0])
    if tag == "LineString":
        coords = _parse_coordinates(_text(_child(element, "coordinates")))
        if len(coords) < 2:
            raise ValueError("a LineString needs at least two points")
        return LineString(coords)
    if tag == "LinearRing":
        return LinearRing(_ring(element))
    if tag == "Polygon":
        return _polygon(element)
    if tag == "Track":
        return LineString(_track_coords(element))
    if tag == "MultiTrack":
        return MultiLineString([_track_coords(t) for t in element.findall("Track")])
    if tag == "MultiGeometry":
        return _combine([_geometry(c) for c in element if c.tag in _GEOMETRY_TAGS])
    raise ValueError(f"unsupported geometry element <{tag}>")


def _combine(parts: list[BaseGeometry]) -> BaseGeometry:
    if not parts:
        raise ValueError("<MultiGeometry> contains no geometry")
    # nested Multi* members are unpacked so that e.g. two MultiPolygons merge into one
    parts = [
        member
        for part in parts
        for member in (
            part.geoms
            if part.geom_type in {"MultiPoint", "MultiLineString", "MultiPolygon"}
            else [part]
        )
    ]
    kinds = {p.geom_type for p in parts}
    if kinds == {"Point"}:
        return MultiPoint(parts)
    if kinds <= {"LineString", "LinearRing"}:
        return MultiLineString([LineString(p.coords) for p in parts])
    if kinds == {"Polygon"}:
        return MultiPolygon(parts)
    # mixed or already-multi parts: keep them as they are
    return GeometryCollection(parts)


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #
def _schemas(root: Element) -> dict[str, dict[str, str]]:
    schemas: dict[str, dict[str, str]] = {}
    for schema in root.iter("Schema"):
        sid = schema.get("id")
        if not sid:
            continue
        schemas[sid] = {
            f.get("name", ""): (f.get("type") or "string").lower()
            for f in schema.findall("SimpleField")
        }
    return schemas


def _cast(value: str | None, kind: str | None) -> Any:
    if value is None or kind is None:
        return value
    try:
        if kind in _INT_TYPES:
            return int(value)
        if kind in _FLOAT_TYPES:
            number = float(value)
            return number if math.isfinite(number) else None
        if kind == "bool":
            return value.strip().lower() in {"1", "true"}
    except ValueError:
        return value
    return value


def _properties(placemark: Element, schemas: dict[str, dict[str, str]]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    name = _text(_child(placemark, "name"))
    description = _text(_child(placemark, "description"))
    if name is not None:
        props["name"] = name
    if description is not None:
        props["description"] = description
    extended = _child(placemark, "ExtendedData")
    if extended is not None:
        for data in extended.findall("Data"):
            key = data.get("name")
            if key:
                props[key] = _text(_child(data, "value"))
        for schema_data in extended.findall("SchemaData"):
            types = schemas.get((schema_data.get("schemaUrl") or "").lstrip("#"), {})
            for simple in schema_data.findall("SimpleData"):
                key = simple.get("name")
                if key:
                    props[key] = _cast((simple.text or "").strip() or None, types.get(key))
    return props


# --------------------------------------------------------------------------- #
# Document walk
# --------------------------------------------------------------------------- #
def _placemarks(node: Element, folders: list[str]) -> list[tuple[Element, str | None]]:
    found: list[tuple[Element, str | None]] = []
    for child in node:
        if child.tag == "Placemark":
            found.append((child, " / ".join(folders) or None))
        elif child.tag == "Folder":
            title = _text(_child(child, "name"))
            found.extend(_placemarks(child, folders + [title] if title else folders))
        elif child.tag == "Document":
            # the Document title is not a layer; only Folders group features
            found.extend(_placemarks(child, folders))
    return found


def parse_kml(content: bytes, *, max_features: int) -> ParsedDataset:
    try:
        root = SafeET.fromstring(content, forbid_dtd=True)
    except DefusedXmlException as exc:
        raise InvalidGeospatialFile(
            "KML contains a DTD or XML entities, which are not allowed.", code="UNSAFE_XML"
        ) from exc
    except Exception as exc:  # ParseError and friends
        raise InvalidGeospatialFile(
            f"File is not well-formed XML: {exc}", code="INVALID_KML"
        ) from exc

    _strip_namespaces(root)
    if root.tag != "kml":
        raise InvalidGeospatialFile(
            f"Not a KML document (root element is <{root.tag}>).", code="INVALID_KML"
        )

    schemas = _schemas(root)
    placemarks = _placemarks(root, [])
    if not placemarks:
        raise InvalidGeospatialFile("KML contains no Placemark features.", code="NO_FEATURES")
    if len(placemarks) > max_features:
        raise InvalidGeospatialFile(
            f"KML contains {len(placemarks)} features; the limit is {max_features}.",
            code="TOO_MANY_FEATURES",
        )

    features: list[ParsedFeature] = []
    for index, (placemark, layer) in enumerate(placemarks):
        feature = ParsedFeature(
            index=index,
            geometry=None,
            geometry_type="Empty",
            crs=crs_service.WGS84,
            crs_label=crs_service.WGS84_LABEL,
            properties=_properties(placemark, schemas),
            source_id=placemark.get("id"),
            layer=layer,
        )
        elements = [c for c in placemark if c.tag in _GEOMETRY_TAGS | _UNSUPPORTED_TAGS]
        supported = [e for e in elements if e.tag in _GEOMETRY_TAGS]
        unsupported = [e for e in elements if e.tag in _UNSUPPORTED_TAGS]

        if supported:
            try:
                geometry = (
                    _geometry(supported[0])
                    if len(supported) == 1
                    else _combine([_geometry(e) for e in supported])
                )
                feature.geometry = geometry
                feature.geometry_type = geometry.geom_type
            except ValueError as exc:
                feature.geometry_type = supported[0].tag if len(supported) == 1 else "Mixed"
                feature.error = f"Invalid {feature.geometry_type} geometry: {exc}"
        elif unsupported:
            feature.geometry_type = unsupported[0].tag
            feature.unsupported_reason = (
                f"KML <{unsupported[0].tag}> geometries are not supported for measurement."
            )
        features.append(feature)

    return ParsedDataset(file_type="kml", features=features)


def parse_kmz(content: bytes, *, max_features: int, max_uncompressed: int) -> ParsedDataset:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise InvalidGeospatialFile("KMZ is not a valid zip archive.", code="INVALID_ZIP") from exc
    with archive:
        kml_members = [
            i
            for i in archive.infolist()
            if not i.is_dir() and i.filename.lower().endswith(".kml")
        ]
        if not kml_members:
            raise InvalidGeospatialFile("KMZ contains no .kml document.", code="INVALID_KMZ")
        # the main document is conventionally doc.kml, else the first one at the top level
        kml_members.sort(key=lambda i: (i.filename.lower() != "doc.kml", i.filename.count("/")))
        member = kml_members[0]
        if member.file_size > max_uncompressed:
            raise InvalidGeospatialFile(
                "KMZ contents are too large when decompressed.", code="ZIP_TOO_LARGE"
            )
        with archive.open(member) as handle:
            data = handle.read(max_uncompressed + 1)
        if len(data) > max_uncompressed:
            raise InvalidGeospatialFile(
                "KMZ contents are too large when decompressed.", code="ZIP_TOO_LARGE"
            )
    dataset = parse_kml(data, max_features=max_features)
    dataset.file_type = "kmz"
    return dataset
