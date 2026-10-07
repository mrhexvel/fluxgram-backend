from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from fluxgram_backend.domain.peer import MAX_TELEGRAM_ID, FluxGramPeerKey, PeerType

DecimalId = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*$")]
PeerId = Annotated[str, StringConstraints(max_length=16, pattern=r"^[1-9][0-9]*$")]
Revision = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]*)$")]
StableId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$"),
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PeerKeyDto(ContractModel):
    peer_type: PeerType
    peer_id: PeerId

    @field_validator("peer_id")
    @classmethod
    def validate_peer_id_range(cls, value: str) -> str:
        if int(value) > MAX_TELEGRAM_ID:
            raise ValueError("peer_id must fit within Telegram's 52-bit identifier range")

        return value

    @classmethod
    def from_domain(cls, peer: FluxGramPeerKey) -> Self:
        return cls(peer_type=peer.peer_type, peer_id=str(peer.peer_id))

    def to_domain(self) -> FluxGramPeerKey:
        return FluxGramPeerKey(peer_type=self.peer_type, peer_id=int(self.peer_id))


class BadgeDto(ContractModel):
    badge_id: StableId
    custom_emoji_document_id: DecimalId
    title: Annotated[str, Field(min_length=1, max_length=64)]
    description: Annotated[str, Field(max_length=256)]
    particles: bool
    expires_at: AwareDatetime | None = None


class DesignDto(ContractModel):
    design_id: StableId | None = None
    palette_id: StableId | None = None
    pattern_id: StableId | None = None
    avatar_frame_id: StableId | None = None
    badge_style_id: StableId | None = None

    @model_validator(mode="after")
    def require_value(self) -> Self:
        values = (
            self.design_id,
            self.palette_id,
            self.pattern_id,
            self.avatar_frame_id,
            self.badge_style_id,
        )

        if all(value is None for value in values):
            raise ValueError("design must contain at least one identifier")

        return self


class PeerProfileDto(ContractModel):
    badges: Annotated[list[BadgeDto], Field(max_length=8)]
    design: DesignDto | None

    @model_validator(mode="after")
    def require_appearance(self) -> Self:
        if not self.badges and self.design is None:
            raise ValueError("profile must contain a badge or design")

        return self


class PeerProfileResultDto(ContractModel):
    peer: PeerKeyDto
    revision: Revision
    expires_at: AwareDatetime
    profile: PeerProfileDto | None


class SingleProfileResponse(ContractModel):
    schema_version: Literal[1]
    minimum_renderer_version: Annotated[int, Field(ge=1)]
    result: PeerProfileResultDto


class BatchProfileRequest(ContractModel):
    peers: Annotated[list[PeerKeyDto], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def require_unique_peers(self) -> Self:
        peers = {(peer.peer_type, peer.peer_id) for peer in self.peers}

        if len(peers) != len(self.peers):
            raise ValueError("peers must be unique")

        return self


class BatchProfileResponse(ContractModel):
    schema_version: Literal[1]
    minimum_renderer_version: Annotated[int, Field(ge=1)]
    results: Annotated[list[PeerProfileResultDto], Field(min_length=1, max_length=100)]


class ErrorResponse(ContractModel):
    code: StableId
    message: Annotated[str, Field(min_length=1, max_length=256)]
