"""The error taxonomy. Every failure the agent can see is one of these categories,
and each category has a recovery strategy, so the agent reacts to the kind of
failure rather than parsing messages.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    CONNECTION = "connection"
    PERMISSION = "permission"
    SQL = "sql"
    EMPTY_RESULT = "empty_result"
    RENDER = "render"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"


class NovaError(Exception):
    """Base class. `message` must be safe to show the agent: no secrets."""

    category = ErrorCategory.INVALID_REQUEST
    retryable = False

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class ConnectionFailure(NovaError):
    category = ErrorCategory.CONNECTION
    retryable = True


class ServerFailure(NovaError):
    """The service answered with an internal error. Unlike a ConnectionFailure,
    repeating the same request gets the same answer."""

    category = ErrorCategory.CONNECTION


class PermissionFailure(NovaError):
    category = ErrorCategory.PERMISSION


class NotFound(NovaError):
    category = ErrorCategory.NOT_FOUND


class InvalidRequest(NovaError):
    category = ErrorCategory.INVALID_REQUEST


class QueryFailure(NovaError):
    category = ErrorCategory.SQL
