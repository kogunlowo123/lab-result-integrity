"""Domain errors mapped to HTTP status codes and OAuth-style error bodies by the API layer."""

from __future__ import annotations


class IntegrityError(Exception):
    code = "invalid_request"
    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(IntegrityError):
    code = "not_found"
    status = 404


class ConflictError(IntegrityError):
    code = "conflict"
    status = 409


class InvalidStateError(IntegrityError):
    code = "invalid_state"
    status = 409


class UnauthorizedError(IntegrityError):
    code = "unauthorized"
    status = 401


class ForbiddenError(IntegrityError):
    code = "forbidden"
    status = 403


class CatalogueError(IntegrityError):
    """The analyte catalogue is malformed or not approved for this environment."""

    code = "catalogue_error"
    status = 500


class UpstreamError(IntegrityError):
    """A dependency (identity fabric, model gateway) failed; callers fail closed."""

    code = "upstream_unavailable"
    status = 503
