"""Zipped ESRI Shapefile parser.

Safety
    Members are read straight from the archive into memory - nothing is ever
    extracted to disk, so path-traversal ("zip slip") names cannot write
    anywhere. Total decompressed bytes, member count and feature count are all
    capped to defuse zip bombs.

Robustness
    * Several shapefiles in one archive (any folder depth) are supported; each
      becomes a ``layer``.
    * ``.shx`` and ``.dbf`` are optional (attributes are simply empty without a
      ``.dbf``); ``.cpg`` selects the attribute encoding (default UTF-8).
    * The CRS comes from ``.prj`` (ESRI or OGC WKT). Without a ``.prj`` the CRS
      is assumed to be EPSG:4326 *only* when all coordinates look like
      lon/lat; otherwise the file is rejected rather than measured wrongly.
    * NULL shapes become ``EMPTY`` features, ``MultiPatch`` becomes
      ``UNSUPPORTED``; neither aborts the upload.
"""

from __future__ import annotations

import io
import logging
import math
import posixpath
import zipfile
from datetime import date, datetime
from typing import Any

import shapefile  # pyshp
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.geometry import shape as shapely_shape

from app.domain import ParsedDataset, ParsedFeature
from app.exceptions import InvalidGeospatialFile
from app.services import crs as crs_service

logger = logging.getLogger(__name__)
logging.getLogger("shapefile").setLevel(logging.ERROR)  # pyshp warns on odd ring winding

_SIDECARS = {".shp", ".shx", ".dbf", ".prj", ".cpg"}


# --------------------------------------------------------------------------- #
# Zip handling
# --------------------------------------------------------------------------- #
def _is_junk(name: str) -> bool:
    parts = name.split("/")
    base = parts[-1]
    return "__MACOSX" in parts or base.startswith(".") or base.startswith("~")


def read_zip_members(
    content: bytes, *, max_uncompressed: int, max_members: int
) -> dict[str, bytes]:
    """Read every relevant sidecar file of a zip into memory, within budget."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise InvalidGeospatialFile("File is not a valid zip archive.", code="INVALID_ZIP") from exc

    members: dict[str, bytes] = {}
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > max_members:
            raise InvalidGeospatialFile(
                f"Archive contains {len(infos)} files; the limit is {max_members}.",
                code="ZIP_TOO_LARGE",
            )
        if sum(i.file_size for i in infos) > max_uncompressed:
            raise InvalidGeospatialFile(
                "Archive is too large when decompressed.", code="ZIP_TOO_LARGE"
            )
        budget = max_uncompressed
        for info in infos:
            name = info.filename.replace("\\", "/")
            ext = posixpath.splitext(name)[1].lower()
            if _is_junk(name) or ext not in _SIDECARS | {".kml"}:
                continue
            try:
                with archive.open(info) as handle:
                    data = handle.read(budget + 1)  # real bytes, not the declared size
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                raise InvalidGeospatialFile(
                    f"Could not read {info.filename!r} from the archive: {exc}",
                    code="INVALID_ZIP",
                ) from exc
            budget -= len(data)
            if budget < 0:
                raise InvalidGeospatialFile(
                    "Archive is too large when decompressed.", code="ZIP_TOO_LARGE"
                )
            members[name] = data
    return members


# --------------------------------------------------------------------------- #
# Attribute helpers
# --------------------------------------------------------------------------- #
def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip("\x00").rstrip()
    return value


def _normalise_date(value: Any) -> Any:
    """pyshp gives dBase dates as ``YYYYMMDD`` strings; make them ISO."""
    if isinstance(value, str) and len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def _encoding(cpg: bytes | None) -> str:
    if not cpg:
        return "utf-8"
    label = cpg.decode("ascii", errors="ignore").strip()
    mapping = {"UTF8": "utf-8", "UTF-8": "utf-8", "ANSI 1252": "cp1252", "LATIN1": "latin-1"}
    resolved = mapping.get(label.upper(), label or "utf-8")
    try:
        "".encode(resolved)
    except LookupError:
        return "utf-8"
    return resolved


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def _type_label(shape_type: int) -> str:
    name = shapefile.SHAPETYPE_LOOKUP.get(shape_type, "UNKNOWN")
    upper = str(name).upper()
    if upper.startswith("MULTIPOINT"):
        return "MultiPoint"
    if upper.startswith("POINT"):
        return "Point"
    if upper.startswith("POLYLINE"):
        return "LineString"
    if upper.startswith("POLYGON"):
        return "Polygon"
    if upper.startswith("MULTIPATCH"):
        return "MultiPatch"
    if upper == "NULL":
        return "Empty"
    return upper.title()


def _load_crs(prj: bytes | None) -> tuple[CRS | None, str | None]:
    if not prj:
        return None, None
    try:
        crs = CRS.from_wkt(prj.decode("utf-8", errors="ignore").strip())
    except CRSError as exc:
        raise InvalidGeospatialFile(
            f"The .prj file could not be understood as a coordinate system: {exc}",
            code="INVALID_PRJ",
        ) from exc
    return crs, crs_service.crs_label(crs)


def _bounds_look_like_lonlat(bbox: list[float] | tuple[float, ...]) -> bool:
    if len(bbox) < 4 or not all(math.isfinite(v) for v in bbox[:4]):
        return False
    minx, miny, maxx, maxy = bbox[:4]
    return -180.0 <= minx <= maxx <= 180.0 and -90.0 <= miny <= maxy <= 90.0


# --------------------------------------------------------------------------- #
# Layer parsing
# --------------------------------------------------------------------------- #
def _parse_layer(
    layer: str,
    files: dict[str, bytes],
    start_index: int,
    max_features: int,
    dataset_warnings: list[str],
) -> list[ParsedFeature]:
    shp = files[".shp"]
    dbf = files.get(".dbf")
    if dbf is None:
        dataset_warnings.append(f"Layer '{layer}' has no .dbf file; feature properties are empty.")
    try:
        reader = shapefile.Reader(
            shp=io.BytesIO(shp),
            shx=io.BytesIO(files[".shx"]) if ".shx" in files else None,
            dbf=io.BytesIO(dbf) if dbf is not None else None,
            encoding=_encoding(files.get(".cpg")),
            encodingErrors="replace",
        )
    except Exception as exc:
        raise InvalidGeospatialFile(
            f"Layer '{layer}' is not a readable shapefile: {exc}", code="INVALID_SHAPEFILE"
        ) from exc

    with reader:
        crs, label = _load_crs(files.get(".prj"))
        if crs is None:
            if not _bounds_look_like_lonlat(reader.bbox or []):
                raise InvalidGeospatialFile(
                    f"Layer '{layer}' has no .prj file and its coordinates are not longitude/"
                    "latitude, so its coordinate system cannot be determined. "
                    "Include the .prj file in the zip.",
                    code="MISSING_CRS",
                )
            crs, label = crs_service.WGS84, crs_service.WGS84_LABEL
            dataset_warnings.append(
                f"Layer '{layer}' has no .prj file; assumed EPSG:4326 because all "
                "coordinates are valid longitude/latitude values."
            )
        assert label is not None

        field_names = [f[0] for f in reader.fields[1:]] if dbf is not None else []
        iterator = reader.iterShapeRecords() if dbf is not None else (
            (s, None) for s in reader.iterShapes()
        )

        features: list[ParsedFeature] = []
        try:
            for position, item in enumerate(iterator):
                if start_index + position >= max_features:
                    raise InvalidGeospatialFile(
                        f"Shapefile contains more than {max_features} features.",
                        code="TOO_MANY_FEATURES",
                    )
                shp_shape, record = (item.shape, item.record) if dbf is not None else item
                properties: dict[str, Any] = {}
                if record is not None:
                    for name, value in zip(field_names, record, strict=False):
                        properties[name] = _json_safe(_normalise_date(value))
                feature = ParsedFeature(
                    index=start_index + position,
                    geometry=None,
                    geometry_type=_type_label(shp_shape.shapeType),
                    crs=crs,
                    crs_label=label,
                    properties=properties,
                    source_id=str(position),
                    layer=layer,
                )
                _attach_geometry(feature, shp_shape)
                features.append(feature)
        except InvalidGeospatialFile:
            raise
        except Exception as exc:
            raise InvalidGeospatialFile(
                f"Layer '{layer}' is corrupt and could not be read to the end: {exc}",
                code="INVALID_SHAPEFILE",
            ) from exc
    return features


def _attach_geometry(feature: ParsedFeature, shp_shape: shapefile.Shape) -> None:
    kind = _type_label(shp_shape.shapeType)
    if kind == "Empty":
        return
    if kind == "MultiPatch":
        feature.unsupported_reason = (
            "Shapefile MultiPatch (3D surface) geometries are not supported."
        )
        return
    try:
        geometry = shapely_shape(shp_shape.__geo_interface__)
    except Exception as exc:
        feature.error = f"Invalid {kind} geometry: {exc}"
        return
    if geometry.is_empty:
        feature.geometry_type = "Empty"
        return
    feature.geometry = geometry
    feature.geometry_type = geometry.geom_type


def parse_shapefile_zip(
    content: bytes, *, max_features: int, max_uncompressed: int, max_members: int
) -> ParsedDataset:
    members = read_zip_members(
        content, max_uncompressed=max_uncompressed, max_members=max_members
    )

    layers: dict[str, dict[str, bytes]] = {}
    for name, data in members.items():
        stem, ext = posixpath.splitext(name)
        if ext.lower() in _SIDECARS:
            layers.setdefault(stem.lower(), {})[ext.lower()] = data
    layers = {stem: files for stem, files in layers.items() if ".shp" in files}
    if not layers:
        raise InvalidGeospatialFile(
            "The zip archive does not contain a .shp file.", code="INVALID_SHAPEFILE"
        )

    dataset_warnings: list[str] = []
    features: list[ParsedFeature] = []
    for stem in sorted(layers):
        layer_name = posixpath.basename(stem)
        features.extend(
            _parse_layer(layer_name, layers[stem], len(features), max_features, dataset_warnings)
        )

    if not features:
        raise InvalidGeospatialFile("The shapefile contains no features.", code="NO_FEATURES")
    return ParsedDataset(file_type="shapefile", features=features, warnings=dataset_warnings)
