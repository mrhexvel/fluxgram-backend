from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from fluxgram_backend.contracts.appearance_v1 import ErrorResponse


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        response = ErrorResponse(code="invalid_request", message="Invalid request")
        return JSONResponse(status_code=400, content=response.model_dump(mode="json"))
