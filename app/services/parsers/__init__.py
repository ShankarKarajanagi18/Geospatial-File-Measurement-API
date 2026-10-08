"""Format detection and dispatch for uploaded geospatial files."""

from __future__ import annotations

import io
import posixpath
import zipfile

from app.config import Settings
from app.domain import ParsedDataset
from app.exceptions import InvalidGeospatialFile, UnsupportedFileType
from app.services.parsers.kml_parser import parse_kml, parse_kmz
from app.services.parsers.shapefile_parser import parse_shapefile_zip

SUPPORTED_EXTENSIONS = (".zip", ".kml", ".kmz")


def parse_upload(filename: str, content: bytes, settings: Settings) -> ParsedDataset:
    """Parse an upload according to its extension (and, for zips, its contents)."""
    ext = posixpath.splitext(filename)[1].lower()

    if ext == ".kml":
        if content[:2] == b"PK":
            raise InvalidGeospatialFile(
                "The file has a .kml extension but is a zip archive. "
                "Upload .kmz files with a .kmz extension.",
                code="INVALID_KML",
            )
        return parse_kml(content, max_features=settings.max_features)

    if ext == ".kmz":
        return parse_kmz(
            content,
            max_features=settings.max_features,
            max_uncompressed=settings.max_uncompressed_bytes,
        )

    if ext == ".zip":
        # A zip normally holds a shapefile; tolerate one holding only a KML.
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = [n.lower() for n in archive.namelist()]
        except zipfile.BadZipFile as exc:
            raise InvalidGeospatialFile(
                "File is not a valid zip archive.", code="INVALID_ZIP"
            ) from exc
        if not any(n.endswith(".shp") for n in names) and any(n.endswith(".kml") for n in names):
            return parse_kmz(
                content,
                max_features=settings.max_features,
                max_uncompressed=settings.max_uncompressed_bytes,
            )
        return parse_shapefile_zip(
            content,
            max_features=settings.max_features,
            max_uncompressed=settings.max_uncompressed_bytes,
            max_members=settings.max_zip_members,
        )

    raise UnsupportedFileType(
        f"Unsupported file type '{ext or filename}'. "
        "Upload a .zip containing a Shapefile, or a .kml file."
    )
