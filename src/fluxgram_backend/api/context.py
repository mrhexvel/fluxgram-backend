import re
from contextvars import ContextVar
from uuid import uuid4

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        supplied_request_id = request.headers.get(REQUEST_ID_HEADER)
        request_id = (
            supplied_request_id
            if supplied_request_id is not None
            and REQUEST_ID_PATTERN.fullmatch(supplied_request_id) is not None
            else uuid4().hex
        )
        token = request_id_context.set(request_id)

        try:
            with logger.contextualize(request_id=request_id):
                response = await call_next(request)
        finally:
            request_id_context.reset(token)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response
