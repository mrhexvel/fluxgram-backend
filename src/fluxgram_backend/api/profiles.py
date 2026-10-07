import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, cast

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse, Response

from fluxgram_backend.contracts.appearance_v1 import (
    BadgeDto,
    BatchProfileRequest,
    BatchProfileResponse,
    DesignDto,
    PeerKeyDto,
    PeerProfileDto,
    PeerProfileResultDto,
    SingleProfileResponse,
)
from fluxgram_backend.domain.peer import MAX_TELEGRAM_ID, FluxGramPeerKey, PeerType
from fluxgram_backend.domain.profile_read import ProfileReadResult

SCHEMA_VERSION: Literal[1] = 1
MINIMUM_RENDERER_VERSION = 1
router = APIRouter(prefix="/v1")


class ProfileReader(Protocol):
    async def get_profile(self, peer: FluxGramPeerKey) -> ProfileReadResult: ...

    async def get_profiles(
        self,
        peers: Sequence[FluxGramPeerKey],
    ) -> tuple[ProfileReadResult, ...]: ...


@router.get("/profiles/{peer_type}/{peer_id}")
async def get_profile(
    request: Request,
    peer_type: PeerType,
    peer_id: Annotated[str, Path(max_length=16, pattern=r"^[1-9][0-9]*$")],
) -> Response:
    if int(peer_id) > MAX_TELEGRAM_ID:
        return _invalid_request()

    reader = _reader(request)
    result = await reader.get_profile(FluxGramPeerKey(peer_type, int(peer_id)))
    response = SingleProfileResponse(
        schema_version=SCHEMA_VERSION,
        minimum_renderer_version=MINIMUM_RENDERER_VERSION,
        result=_result_dto(result),
    )
    content = _response_content(response)
    encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
    etag = f'"{hashlib.sha256(encoded).hexdigest()}"'
    headers = {
        "ETag": etag,
        "Cache-Control": _cache_control((result,)),
    }

    if _etag_matches(request.headers.get("If-None-Match"), etag):
        return Response(status_code=304, headers=headers)

    return JSONResponse(content=content, headers=headers)


@router.post("/profiles:batch")
async def get_profiles_batch(request: Request, body: BatchProfileRequest) -> Response:
    reader = _reader(request)
    results = await reader.get_profiles(tuple(peer.to_domain() for peer in body.peers))
    response = BatchProfileResponse(
        schema_version=SCHEMA_VERSION,
        minimum_renderer_version=MINIMUM_RENDERER_VERSION,
        results=[_result_dto(result) for result in results],
    )
    return JSONResponse(
        content=_response_content(response),
        headers={"Cache-Control": _cache_control(results)},
    )


def _reader(request: Request) -> ProfileReader:
    return cast(ProfileReader, request.app.state.profile_reader)


def _result_dto(result: ProfileReadResult) -> PeerProfileResultDto:
    profile = result.profile
    profile_dto = None

    if profile is not None:
        design = profile.design
        profile_dto = PeerProfileDto(
            badges=[
                BadgeDto(
                    badge_id=badge.badge_id,
                    custom_emoji_document_id=str(badge.custom_emoji_document_id),
                    title=badge.title,
                    description=badge.description,
                    particles=badge.particles,
                    expires_at=badge.expires_at,
                )
                for badge in profile.badges
            ],
            design=(
                DesignDto(
                    design_id=design.design_id,
                    palette_id=design.palette_id,
                    pattern_id=design.pattern_id,
                    avatar_frame_id=design.avatar_frame_id,
                    badge_style_id=design.badge_style_id,
                )
                if design is not None
                else None
            ),
        )

    return PeerProfileResultDto(
        peer=PeerKeyDto.from_domain(result.peer),
        revision=str(result.revision),
        expires_at=result.expires_at,
        profile=profile_dto,
    )


def _cache_control(results: Sequence[ProfileReadResult]) -> str:
    expires_at = min(result.expires_at for result in results)
    max_age = max(0, int((expires_at - datetime.now(UTC)).total_seconds()))
    return f"public, max-age={max_age}, must-revalidate"


def _response_content(
    response: SingleProfileResponse | BatchProfileResponse,
) -> dict[str, Any]:
    content = response.model_dump(mode="json")
    results = [content["result"]] if "result" in content else content["results"]

    for result in results:
        profile = result["profile"]
        if profile is None or profile["design"] is None:
            continue

        profile["design"] = {
            field: value
            for field, value in profile["design"].items()
            if value is not None
        }

    return content


def _etag_matches(value: str | None, etag: str) -> bool:
    if value is None:
        return False

    expected = etag.removeprefix("W/")
    return any(
        candidate.strip() == "*" or candidate.strip().removeprefix("W/") == expected
        for candidate in value.split(",")
    )


def _invalid_request() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"code": "invalid_request", "message": "Invalid request"},
    )
