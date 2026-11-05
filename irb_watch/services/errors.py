"""Errors the API turns into HTTP responses (see the handler in main.py)."""


class ServiceError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(ServiceError):
    status_code = 404


class ConflictError(ServiceError):
    status_code = 409


class ValidationError(ServiceError):
    status_code = 422


class UpstreamError(ServiceError):
    status_code = 502
