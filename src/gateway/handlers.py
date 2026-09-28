"""The single place an exception becomes an HTTP response body."""

import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.models.errors import ApiError, InvalidQueryError, UnauthenticatedError


def _error_body(error_code: str, message: str, **optional: Any) -> dict[str, Any]:
    """Render the wire body, omitting the optional keys that are unset."""
    body: dict[str, Any] = {"error_code": error_code, "message": message}
    body.update({key: value for key, value in optional.items() if value is not None})
    return body


def _retry_after_seconds(retry_after_ms: int) -> str:
    """``Retry-After`` is whole seconds; round up so we never advise too early."""
    return str(math.ceil(retry_after_ms / 1000))


async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
    body = _error_body(
        exc.code.value,
        exc.message,
        retry_after_ms=exc.retry_after_ms,
        suggested_action=exc.suggested_action,
    )
    headers = None
    if exc.retry_after_ms is not None:
        headers = {"Retry-After": _retry_after_seconds(exc.retry_after_ms)}
    return JSONResponse(status_code=exc.http, content=body, headers=headers)


async def _handle_unauthenticated(_request: Request, exc: UnauthenticatedError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http,
        content=_error_body(exc.error_code, exc.message),
    )


async def _handle_invalid_query(_request: Request, exc: InvalidQueryError) -> JSONResponse:
    """400. ``detail`` names the offending construct when one was identified."""
    return JSONResponse(
        status_code=exc.http,
        content=_error_body(exc.error_code, exc.message, detail=exc.detail),
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register the three exception handlers on the app.

    One per exception type, and the set is closed: anything else reaching here
    is a bug and must surface as a 500 with a traceback in the log, not be
    quietly rendered as a tidy error body (LAW 4).
    """
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(UnauthenticatedError, _handle_unauthenticated)
    app.add_exception_handler(InvalidQueryError, _handle_invalid_query)
