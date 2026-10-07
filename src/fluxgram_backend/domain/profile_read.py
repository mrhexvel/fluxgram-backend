from dataclasses import dataclass
from datetime import datetime

from fluxgram_backend.domain.appearance import ProfileDesign
from fluxgram_backend.domain.peer import FluxGramPeerKey


@dataclass(frozen=True, slots=True)
class RendererBadge:
    badge_id: str
    custom_emoji_document_id: int
    title: str
    description: str
    particles: bool
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class RendererProfile:
    badges: tuple[RendererBadge, ...]
    design: ProfileDesign | None


@dataclass(frozen=True, slots=True)
class ProfileReadResult:
    peer: FluxGramPeerKey
    revision: int
    expires_at: datetime
    profile: RendererProfile | None
