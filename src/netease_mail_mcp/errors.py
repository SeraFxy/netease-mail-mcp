"""Error types.

Every message is shown to the agent and usually to the human as well, so messages are
bilingual (Chinese first, English second) and say how to fix the problem.
"""

from __future__ import annotations


class MailError(Exception):
    """Base class for all errors this server reports as MCP tool errors."""

    code = "error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        self.message = message
        self.hint = hint
        super().__init__(message if not hint else f"{message}\n{hint}")

    def render(self) -> str:
        return f"[{self.code}] {self}"


class ConfigError(MailError):
    code = "config_error"


class CredentialError(MailError):
    code = "credential_error"


class AuthError(MailError):
    code = "auth_failed"


class ConnectionFailed(MailError):
    code = "connection_failed"


class MailTimeout(MailError):
    code = "timed_out"


class ProtocolError(MailError):
    code = "server_error"


class NotFoundError(MailError):
    code = "not_found"


class InvalidIdError(MailError):
    code = "invalid_id"


class StaleIdError(MailError):
    code = "stale_id"


class LimitError(MailError):
    code = "limit_exceeded"


class InvalidArgument(MailError):
    code = "invalid_argument"


class ModeError(MailError):
    code = "mode_forbidden"


class ConfirmationError(MailError):
    code = "confirmation_failed"
