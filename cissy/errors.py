"""Errors that carry an HTTP status and a sentence meant for a human.

Every message here is shown directly in the browser, so it says what is wrong
and what to do about it — never a stack trace or a raw tool error.
"""


class CissyError(Exception):
    """A failure the user can understand and usually act on."""

    status = 400

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"error": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload


class ValidationError(CissyError):
    """The submitted configuration cannot produce a working app."""

    status = 422


class NotFoundError(CissyError):
    status = 404


class ConflictError(CissyError):
    """The request is fine but the server is not in a state to do it."""

    status = 409


class ToolchainError(CissyError):
    """Flutter, the Android SDK or the JDK is missing or unusable."""

    status = 503
