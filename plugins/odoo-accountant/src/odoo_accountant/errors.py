"""Exception types."""


class OdooAccountantError(Exception):
    """Base class."""


class ConfigError(OdooAccountantError):
    pass


class PolicyError(OdooAccountantError):
    pass


class ApprovalError(OdooAccountantError):
    pass


class ValidationError(OdooAccountantError, ValueError):
    pass


class OdooClientError(OdooAccountantError):
    """Transport-level or blocked-call error (never carries secrets)."""


class OdooError(OdooClientError):
    """Error reported by Odoo (traceback/debug info is dropped)."""

    def __init__(self, status: int, name: str, message: str):
        super().__init__(f"Odoo error {status} {name}: {message}")
        self.status = status
        self.name = name
        self.message = message


class StatementError(OdooAccountantError):
    """Bank-statement input problem. `str(exc)` is a clear Arabic message."""

    def __init__(self, code: str, message_ar: str):
        super().__init__(message_ar)
        self.code = code
        self.message_ar = message_ar
