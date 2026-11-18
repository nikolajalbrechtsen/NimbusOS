# =============================================================================
# nimbus_control/errors.py: API errors the user can act on
# =============================================================================
# What: ApiError(status, message, field) and the handlers that turn it, a
#       privd error and a request-validation error into one JSON shape:
#         {"message": "<what went wrong and how to fix it>", "field": "<input>"}
#       privd error codes map to HTTP statuses: invalid_argument 422,
#       busy 409, failed 502, unavailable 503, anything else 500.
# Why:  spec "Copy": error messages say what went wrong and how to fix it;
#       the frontend shows "message" as is and highlights "field".
# =============================================================================

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .privd_client import PrivdError


class ApiError(Exception):
    def __init__(self, status, message, field=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.field = field


PRIVD_STATUS = {"invalid_argument": 422, "busy": 409, "failed": 502, "unavailable": 503}


def _body(message, field=None):
    body = {"message": message}
    if field:
        body["field"] = field
    return body


def install(app):
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return JSONResponse(_body(exc.message, exc.field), status_code=exc.status)

    @app.exception_handler(PrivdError)
    async def privd_error(request: Request, exc: PrivdError):
        return JSONResponse(_body(exc.message, exc.field), status_code=PRIVD_STATUS.get(exc.code, 500))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or None
        return JSONResponse(_body(f"Invalid input: {first.get('msg', 'not accepted')}.", field), status_code=422)
