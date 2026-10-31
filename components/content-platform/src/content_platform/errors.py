"""Typed errors shared by the offline service layer."""


class ContentPlatformError(Exception):
    """Base class for expected application errors."""


class ValidationError(ContentPlatformError):
    """An input value failed a service boundary check."""


class NotFound(ContentPlatformError):
    """A requested user or content record does not exist."""


class AlreadyExists(ContentPlatformError):
    """A unique identity already exists."""


class IdempotencyConflict(ContentPlatformError):
    """An idempotency key was reused for a different request body."""


class InvalidCursor(ContentPlatformError):
    """A feed cursor is malformed or incompatible."""


class AuthenticationFailed(ContentPlatformError):
    """Credentials or a bearer session token were not accepted."""


class CacheUnavailable(ContentPlatformError):
    """An optional external cache could not complete an operation."""
