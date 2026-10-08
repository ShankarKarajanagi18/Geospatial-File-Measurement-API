"""``/api/files`` endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import shapely
from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status
from pyproj import CRS
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas import (
    ErrorResponse,
    FeatureItem,
    FeaturesResponse,
    FileInfo,
    FileList,
    MeasurementsResponse,
    MeasurementSummary,
)
from app.config import Settings
from app.database import get_db
from app.domain import FileStatus, MeasurementStatus
from app.exceptions import Conflict, FileTooLarge, NotFound
from app.models import Feature, UploadedFile
from app.services import crs as crs_service
from app.services.ingest import ingest_upload, sanitize_filename

router = APIRouter(prefix="/api/files", tags=["files"])

_READ_CHUNK = 1024 * 1024

_ERRORS = {
    400: {"model": ErrorResponse, "description": "Unsupported type or empty upload"},
    413: {"model": ErrorResponse, "description": "Upload too large"},
    422: {"model": ErrorResponse, "description": "File contents cannot be processed"},
}


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _get_file(db: Session, file_id: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise NotFound(f"No file with id '{file_id}'.")
    return record


def _require_completed(record: UploadedFile) -> None:
    if record.status == FileStatus.FAILED.value:
        raise Conflict(
            f"Processing of this file failed: {record.error}", code="FILE_PROCESSING_FAILED"
        )
    if record.status != FileStatus.COMPLETED.value:
        raise Conflict(
            f"File is still {record.status.lower()}; try again shortly.", code="FILE_NOT_READY"
        )


def _measurement_item(feature: Feature) -> dict:
    return {
        "index": feature.feature_index,
        "source_id": feature.source_id,
        "layer": feature.layer,
        "geometry_type": feature.geometry_type,
        "crs": feature.crs,
        "status": feature.measurement_status,
        "area_m2": feature.area_m2,
        "perimeter_m": feature.perimeter_m,
        "length_m": feature.length_m,
        "method": feature.measurement_method,
        "warnings": feature.warnings or [],
    }


def _filters(file_id: str, geometry_type: str | None, status_filter: MeasurementStatus | None):
    conditions = [Feature.file_id == file_id]
    if geometry_type:
        conditions.append(func.lower(Feature.geometry_type) == geometry_type.lower())
    if status_filter:
        conditions.append(Feature.measurement_status == status_filter.value)
    return conditions


# --------------------------------------------------------------------------- #
# Upload / list / info / delete
# --------------------------------------------------------------------------- #
@router.post(
    "/",
    response_model=FileInfo,
    status_code=status.HTTP_201_CREATED,
    responses=_ERRORS,
    summary="Upload and process a geospatial file",
    description=(
        "Accepts a **.zip containing a Shapefile** (`.shp`, `.dbf`, `.shx`, `.prj`...) "
        "or a **.kml** file (`.kmz` is accepted too), sent as multipart form field `file`. "
        "The file is parsed and measured synchronously; the response has status "
        "`COMPLETED`. Unreadable content returns 422 and keeps a `FAILED` record whose "
        "id is in the error body."
    ),
)
@router.post("", response_model=FileInfo, status_code=201, include_in_schema=False)
def upload_file(
    file: UploadFile = File(..., description="A .zip (Shapefile) or .kml file"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UploadedFile:
    limit = settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while chunk := file.file.read(_READ_CHUNK):
        total += len(chunk)
        if total > limit:
            raise FileTooLarge(f"File exceeds the {limit // (1024 * 1024)} MB upload limit.")
        chunks.append(chunk)
    return ingest_upload(db, settings, file.filename or "", b"".join(chunks))


@router.get("/", response_model=FileList, summary="List uploaded files")
def list_files(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status_filter: FileStatus | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
) -> dict:
    conditions = [UploadedFile.status == status_filter.value] if status_filter else []
    total = db.scalar(select(func.count()).select_from(UploadedFile).where(*conditions)) or 0
    files = db.scalars(
        select(UploadedFile)
        .where(*conditions)
        .order_by(UploadedFile.created_at.desc(), UploadedFile.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return {"total": total, "limit": limit, "offset": offset, "files": files}


@router.get(
    "/{file_id}/",
    response_model=FileInfo,
    summary="Get information about an uploaded file",
    responses={404: {"model": ErrorResponse}},
)
def get_file(file_id: str, db: Session = Depends(get_db)) -> UploadedFile:
    return _get_file(db, file_id)


@router.delete(
    "/{file_id}/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an uploaded file and its features",
    responses={404: {"model": ErrorResponse}},
)
def delete_file(file_id: str, db: Session = Depends(get_db)) -> Response:
    record = _get_file(db, file_id)
    db.delete(record)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Measurements / features
# --------------------------------------------------------------------------- #
@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Get area / length measurements for every feature",
    description=(
        "Polygons get `area_m2` and `perimeter_m`, lines get `length_m`, points have "
        "nothing to measure. Unsupported or broken geometries are reported with a "
        "status and a warning instead of failing the request. `summary` totals the "
        "features matching the filters."
    ),
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def get_measurements(
    file_id: str,
    limit: int = Query(1000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    geometry_type: str | None = Query(None, description="e.g. Polygon, LineString, Point"),
    status_filter: MeasurementStatus | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
) -> dict:
    record = _get_file(db, file_id)
    _require_completed(record)
    conditions = _filters(file_id, geometry_type, status_filter)

    total = db.scalar(select(func.count()).select_from(Feature).where(*conditions)) or 0
    features = db.scalars(
        select(Feature)
        .where(*conditions)
        .order_by(Feature.feature_index)
        .limit(limit)
        .offset(offset)
    ).all()

    counts = dict(
        db.execute(
            select(Feature.measurement_status, func.count())
            .where(*conditions)
            .group_by(Feature.measurement_status)
        ).all()
    )
    totals = db.execute(
        select(
            func.coalesce(func.sum(Feature.area_m2), 0.0),
            func.coalesce(func.sum(Feature.perimeter_m), 0.0),
            func.coalesce(func.sum(Feature.length_m), 0.0),
        ).where(*conditions)
    ).one()
    summary = MeasurementSummary(
        measured=counts.get(MeasurementStatus.MEASURED.value, 0),
        not_applicable=counts.get(MeasurementStatus.NOT_APPLICABLE.value, 0),
        unsupported=counts.get(MeasurementStatus.UNSUPPORTED.value, 0),
        empty=counts.get(MeasurementStatus.EMPTY.value, 0),
        errors=counts.get(MeasurementStatus.ERROR.value, 0),
        total_area_m2=round(totals[0], 4),
        total_perimeter_m=round(totals[1], 4),
        total_length_m=round(totals[2], 4),
    )
    return {
        "file_id": record.id,
        "filename": record.filename,
        "crs": record.crs,
        "summary": summary,
        "total": total,
        "limit": limit,
        "offset": offset,
        "measurements": [_measurement_item(f) for f in features],
    }


def _reproject_geometry_to_wgs84(geometry: dict | None, crs_str: str | None) -> dict | None:
    if not geometry or not crs_str or crs_service.is_wgs84(crs_str):
        return geometry
    try:
        src_crs = CRS.from_user_input(crs_str)
        sh_geom = shapely.from_geojson(json.dumps(geometry))
        reproj = crs_service.reproject(sh_geom, src_crs, crs_service.WGS84)
        return json.loads(shapely.to_geojson(shapely.force_2d(reproj)))
    except Exception:
        return geometry


@router.get(
    "/{file_id}/features/",
    response_model=FeaturesResponse,
    summary="Get every feature with geometry, CRS, properties and measurements",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def get_features(
    file_id: str,
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    geometry_type: str | None = Query(None),
    status_filter: MeasurementStatus | None = Query(None, alias="status"),
    to_wgs84: bool = Query(
        False, description="Reproject returned geometries to EPSG:4326 for map visualization"
    ),
    db: Session = Depends(get_db),
) -> dict:
    record = _get_file(db, file_id)
    _require_completed(record)
    conditions = _filters(file_id, geometry_type, status_filter)
    total = db.scalar(select(func.count()).select_from(Feature).where(*conditions)) or 0
    features = db.scalars(
        select(Feature)
        .where(*conditions)
        .order_by(Feature.feature_index)
        .limit(limit)
        .offset(offset)
    ).all()
    return {
        "file_id": record.id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "features": [
            {
                **_measurement_item(f),
                "geometry": (
                    _reproject_geometry_to_wgs84(f.geometry, f.crs) if to_wgs84 else f.geometry
                ),
                "properties": f.properties or {},
            }
            for f in features
        ],
    }


@router.get(
    "/{file_id}/features/{index}/",
    response_model=FeatureItem,
    summary="Get a single feature by its index",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def get_feature(
    file_id: str,
    index: int,
    to_wgs84: bool = Query(
        False, description="Reproject returned geometry to EPSG:4326 for map visualization"
    ),
    db: Session = Depends(get_db),
) -> dict:
    record = _get_file(db, file_id)
    _require_completed(record)
    feature = db.scalar(
        select(Feature).where(Feature.file_id == file_id, Feature.feature_index == index)
    )
    if feature is None:
        raise NotFound(f"File '{file_id}' has no feature with index {index}.")
    geom = (
        _reproject_geometry_to_wgs84(feature.geometry, feature.crs)
        if to_wgs84
        else feature.geometry
    )
    return {
        **_measurement_item(feature),
        "geometry": geom,
        "properties": feature.properties or {},
    }


@router.get("/samples/list", summary="List bundled sample files", include_in_schema=False)
def list_samples() -> list[dict]:
    samples_dir = Path(__file__).resolve().parents[2] / "sample_data"
    if not samples_dir.exists():
        return []
    items = []
    descriptions = {
        "bengaluru_survey.kml": "KML survey with parcels, courtyard hole, road and landmarks",
        "parcels_utm43.zip": "Shapefile in projected UTM Zone 43N (EPSG:32643)",
        "districts_wgs84.zip": "Shapefile in geographic WGS84 (EPSG:4326)",
        "edge_cases.kml": "KML with self-intersecting polygon, 3D Model, empty placemark",
    }
    for p in sorted(samples_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in (".kml", ".kmz", ".zip"):
            items.append({
                "name": p.name,
                "size_bytes": p.stat().st_size,
                "description": descriptions.get(p.name, f"Sample {p.suffix} file"),
            })
    return items


@router.post(
    "/samples/load/{sample_name}",
    response_model=FileInfo,
    summary="Load a bundled sample file",
    include_in_schema=False,
)
def load_sample(
    sample_name: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UploadedFile:
    safe_name = sanitize_filename(sample_name)
    sample_path = Path(__file__).resolve().parents[2] / "sample_data" / safe_name
    if not sample_path.exists() or not sample_path.is_file():
        raise NotFound(f"Sample '{safe_name}' not found.")
    content = sample_path.read_bytes()
    return ingest_upload(db, settings, safe_name, content)
