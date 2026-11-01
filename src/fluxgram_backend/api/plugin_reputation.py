from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import Response

from fluxgram_backend.application.plugin_reputation import PluginReputationService
from fluxgram_backend.reputation.protocol import MAX_QUERY_BYTES, ProtocolError, ReputationQuery
from fluxgram_backend.reputation.verification import VerificationError

router = APIRouter(prefix="/v1")
NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


@router.post("/plugin-reputation/query")
async def query_plugin_reputation(request: Request) -> Response:
    if request.headers.get("Content-Type") != "application/octet-stream":
        return Response(status_code=415, headers=NO_STORE_HEADERS)

    content_encoding = request.headers.get("Content-Encoding")
    if content_encoding is not None and content_encoding.strip().lower() != "identity":
        return Response(status_code=415, headers=NO_STORE_HEADERS)

    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdecimal():
            return Response(status_code=400, headers=NO_STORE_HEADERS)
        if int(content_length) > MAX_QUERY_BYTES:
            return Response(status_code=413, headers=NO_STORE_HEADERS)

    encoded_query = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_QUERY_BYTES - len(encoded_query):
            return Response(status_code=413, headers=NO_STORE_HEADERS)

        encoded_query.extend(chunk)

    try:
        query = ReputationQuery.decode(bytes(encoded_query))
    except ProtocolError:
        return Response(status_code=400, headers=NO_STORE_HEADERS)

    service = cast(PluginReputationService, request.app.state.plugin_reputation_service)

    try:
        result = await service.lookup(query)
    except (ProtocolError, VerificationError):
        return Response(status_code=503, headers=NO_STORE_HEADERS)

    if result is None:
        return Response(status_code=404, headers=NO_STORE_HEADERS)

    return Response(
        content=result,
        media_type="application/octet-stream",
        headers=NO_STORE_HEADERS,
    )
