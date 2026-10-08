"""Application errors.

Every error carries a stable machine-readable ``code`` and an HTTP status so the
API can return a uniform ``{"detail": ..., "code": ...}`` body.
"""

from __future__ import annotations


class AppError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        #: Set when the failure happened after a file record was created.
        self.file_id: str | None = None


class UnsupportedFileType(AppError):
    status_code = 400
    code = "UNSUPPORTED_FILE_TYPE"


class EmptyUpload(AppError):
    status_code = 400
    code = "EMPTY_FILE"


class FileTooLarge(AppError):
    status_code = 413
    code = "FILE_TOO_LARGE"


class InvalidGeospatialFile(AppError):
    """The upload is the right kind of file but its contents cannot be used."""

    status_code = 422
    code = "INVALID_GEOSPATIAL_FILE"


class NotFound(AppError):
    status_code = 404
    code = "NOT_FOUND"


class Conflict(AppError):
    status_code = 409
    code = "CONFLICT"
