"""
Domain exception hierarchy + FastAPI exception handlers.

Previously, `cad_generator.py` caught a bare `Exception` around the whole
pipeline and returned `{"success": False, "error": str(e) + traceback}` -
which means a parsing failure, a validation failure, an OCCT geometry
failure, and a disk-full error all look identical to a caller, and a full
Python traceback (file paths, internal function names) gets shipped in
every HTTP 200 response body regardless of who's asking or whether
DEBUG is on.

This module gives each failure mode its own exception class (so callers -
and tests - can distinguish "bad input" from "internal geometry bug"),
and a set of FastAPI exception handlers that convert them into
consistent, appropriately-scoped JSON error responses: 4xx with a safe
message for the client's own mistakes, 5xx with a generic message (full
detail only in logs) for anything that's this service's fault.
"""

from __future__ import annotations

from typing import Any


class NitocadError(Exception):
    """Base class for all domain errors raised by this service."""

    #: HTTP status code the FastAPI handler should map this to.
    status_code: int = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ParseError(NitocadError):
    """The description couldn't be turned into a recognized part type
    (regex fallback found nothing, or DeepSeek returned unparseable
    output). Client-fixable - status 422."""

    status_code = 422


class UnsupportedPartTypeError(NitocadError):
    """Parser identified a part_type with no registered template."""

    status_code = 422


class GeometryValidationError(NitocadError):
    """Parameters are geometrically impossible even after auto-correction
    (validator.py's ValidationResult.errors is non-empty)."""

    status_code = 422


class UnsupportedFormatError(NitocadError):
    """Caller requested an export format that doesn't exist (e.g. a typo
    like "stpe", or a format not yet implemented). Client-fixable, same
    reasoning as UnsupportedPartTypeError - status 422."""

    status_code = 422


class GenerationError(NitocadError):
    """CadQuery/OpenCASCADE failed to produce or export geometry. Not the
    caller's fault in the sense that their input parsed and validated
    fine - this is a kernel/template bug - so it's a 500, but still a
    known, named failure mode rather than a bare exception."""

    status_code = 500


class StorageError(NitocadError):
    """R2 upload failed after local generation succeeded."""

    status_code = 502


class AssemblyError(NitocadError):
    """Multi-part assembly construction failed."""

    status_code = 422


class PaymentError(NitocadError):
    """BOT Chain payment verification failed - unconfirmed, wrong
    amount, wrong recipient, wrong sender, reverted, or an already-used
    tx hash. See botchain_pay.py. Client-fixable in the sense that a
    correct payment resolves it - status 402."""

    status_code = 402


def register_exception_handlers(app: Any) -> None:
    """Wire NitocadError (and its subclasses) plus a catch-all fallback
    into the given FastAPI app. Kept as a function (rather than
    decorators at import time) so it can be unit-tested against a
    throwaway FastAPI app without importing the whole web_app module."""
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from logging_config import get_logger

    logger = get_logger(__name__)

    @app.exception_handler(NitocadError)
    async def _handle_nitocad_error(request: Request, exc: NitocadError) -> JSONResponse:
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "%s on %s: %s",
            type(exc).__name__,
            request.url.path,
            exc.message,
            exc_info=exc.status_code >= 500,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": exc.message,
                "error_type": type(exc).__name__,
                "details": exc.details,
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled exception on %s", request.url.path, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": "Internal server error. This has been logged.",
                "error_type": "InternalError",
                "details": {},
            },
        )
