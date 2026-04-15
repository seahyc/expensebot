class OmniHRError(Exception):
    """Base."""


class AuthError(OmniHRError):
    """401/403 from OmniHR. Refresh token expired or rejected."""


class SchemaDriftError(OmniHRError):
    """API rejected payload because schema changed under us. Caller should
    invalidate cache, refetch schema, rebuild payload, retry once."""

    def __init__(self, message: str, field_errors: list[dict] | None = None):
        super().__init__(message)
        self.field_errors = field_errors or []


class ValidationError(OmniHRError):
    """Payload genuinely invalid (e.g. amount missing, date malformed).
    Surface to user, do not retry."""

    def __init__(self, message: str, field_errors: list[dict] | None = None):
        super().__init__(message)
        self.field_errors = field_errors or []
