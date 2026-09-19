from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler


class DomainError(Exception):
    """业务规则错误基类。"""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "domain_error"

    def __init__(self, message, extra=None):
        super().__init__(message)
        self.message = message
        self.extra = extra or {}


class DomainValidationError(DomainError):
    code = "validation_error"


class DomainPermissionError(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"


class DomainConflictError(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class DomainNotFoundError(DomainError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


def domain_exception_handler(exc, context):
    if isinstance(exc, DomainError):
        payload = {"error": exc.code, "message": exc.message}
        payload.update(exc.extra)
        return Response(payload, status=exc.status_code)
    return exception_handler(exc, context)
