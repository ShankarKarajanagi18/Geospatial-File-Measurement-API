"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import files
from app.api.schemas import HealthResponse
from app.config import Settings
from app.database import Base, make_engine, make_session_factory
from app.exceptions import AppError

logger = logging.getLogger("app")

DESCRIPTION = """
Upload a **Shapefile (.zip)** or **KML** file and get back every feature with its
geometry, CRS and properties, plus **area** (polygons) and **length** (lines).

Measurements are never taken in degrees: geographic coordinates are projected to
a suitable UTM zone (or an equal-area projection for very wide features) first,
and every result carries an independent geodesic cross-check.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        Base.metadata.create_all(app.state.engine)
        yield
        app.state.engine.dispose()

    app = FastAPI(
        title="Geospatial File Measurement API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[{"name": "files", "description": "Upload files and read measurements."}],
    )
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    app.state.session_factory = make_session_factory(app.state.engine)

    # Reject oversized uploads from the Content-Length header before the body is
    # spooled to disk (chunked uploads without the header are still checked while reading).
    @app.middleware("http")
    async def limit_request_size(request: Request, call_next):
        declared = request.headers.get("content-length")
        slack = 1_048_576  # multipart framing overhead
        if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes + slack:
            return JSONResponse(
                status_code=413,
                content={
                    "detail": f"File exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB "
                    "upload limit.",
                    "code": "FILE_TOO_LARGE",
                },
            )
        return await call_next(request)

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        body: dict = {"detail": exc.message, "code": exc.code}
        if exc.file_id:
            body["id"] = exc.file_id
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError):
        errors = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
        summary = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in errors
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"Invalid request: {summary}",
                "code": "VALIDATION_ERROR",
                "errors": errors,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error.", "code": "INTERNAL_ERROR"},
        )

    app.include_router(files.router)

    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/health", response_model=HealthResponse, tags=["meta"], summary="Liveness check")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/", include_in_schema=False)
    def root(request: Request):
        accept = request.headers.get("accept", "")
        index_file = static_dir / "index.html"
        if "text/html" in accept and index_file.exists():
            return FileResponse(index_file)
        return {"name": app.title, "version": __version__, "docs": "/docs"}

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return JSONResponse(status_code=404, content={"detail": "Dashboard frontend not found."})

    return app


app = create_app()
