"""Errors the API turns into HTTP responses (see the handler in main.py)."""


class ServiceError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.headers: dict[str, str] | None = None  # extra response headers, if any


class UnauthorizedError(ServiceError):
    status_code = 401

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.headers = {"WWW-Authenticate": "Bearer"}


class ForbiddenError(ServiceError):
    status_code = 403


class NotFoundError(ServiceError):
    status_code = 404


class ConflictError(ServiceError):
    status_code = 409


class ValidationError(ServiceError):
    status_code = 422


class TooManyRequestsError(ServiceError):
    status_code = 429


class RateLimitedError(TooManyRequestsError):
    """Too many wrong attempts; the answer says how long to wait."""

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.headers = {"Retry-After": str(max(1, retry_after_seconds))}


class UpstreamError(ServiceError):
    status_code = 502


class UnavailableError(ServiceError):
    status_code = 503


class AuditUnavailableError(UnavailableError):
    """An action that must be logged first could not be logged, so it was not carried out."""
