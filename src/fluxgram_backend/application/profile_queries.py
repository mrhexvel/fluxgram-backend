from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fluxgram_backend.database.models import (
    BadgeDefinitionRecord,
    PeerAliasRecord,
    PeerBadgeRecord,
    PeerProfileRecord,
    ProfileDesignRecord,
)
from fluxgram_backend.domain.appearance import ProfileDesign
from fluxgram_backend.domain.peer import FluxGramPeerKey
from fluxgram_backend.domain.profile_read import (
    ProfileReadResult,
    RendererBadge,
    RendererProfile,
)


class ProfileQueryService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        cache_ttl_seconds: int,
        negative_cache_ttl_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._cache_ttl_seconds = cache_ttl_seconds
        self._negative_cache_ttl_seconds = negative_cache_ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def get_profile(self, peer: FluxGramPeerKey) -> ProfileReadResult:
        return (await self.get_profiles((peer,)))[0]

    async def get_profiles(
        self,
        peers: Sequence[FluxGramPeerKey],
    ) -> tuple[ProfileReadResult, ...]:
        if not peers:
            return ()

        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("query clock must return a timezone-aware datetime")

        async with self._sessions() as session:
            resolved_peers = await self._resolve_aliases(session, peers)
            unique_peers = tuple(dict.fromkeys(resolved_peers))
            profiles = await self._load_profiles(session, unique_peers)
            badges = await self._load_badges(session, unique_peers, now)
            designs = await self._load_designs(session, unique_peers)

        return tuple(
            self._build_result(peer, profiles, badges, designs, now)
            for peer in resolved_peers
        )

    async def _resolve_aliases(
        self,
        session: AsyncSession,
        peers: Sequence[FluxGramPeerKey],
    ) -> tuple[FluxGramPeerKey, ...]:
        keys = self._database_keys(peers)
        aliases = await session.scalars(
            select(PeerAliasRecord).where(
                tuple_(
                    PeerAliasRecord.source_peer_type,
                    PeerAliasRecord.source_peer_id,
                ).in_(keys)
            )
        )
        alias_map = {
            FluxGramPeerKey(alias.source_peer_type, alias.source_peer_id): FluxGramPeerKey(
                alias.target_peer_type,
                alias.target_peer_id,
            )
            for alias in aliases
        }
        return tuple(alias_map.get(peer, peer) for peer in peers)

    async def _load_profiles(
        self,
        session: AsyncSession,
        peers: Sequence[FluxGramPeerKey],
    ) -> dict[FluxGramPeerKey, PeerProfileRecord]:
        records = await session.scalars(
            select(PeerProfileRecord).where(
                tuple_(PeerProfileRecord.peer_type, PeerProfileRecord.peer_id).in_(
                    self._database_keys(peers)
                )
            )
        )
        return {
            FluxGramPeerKey(record.peer_type, record.peer_id): record
            for record in records
        }

    async def _load_badges(
        self,
        session: AsyncSession,
        peers: Sequence[FluxGramPeerKey],
        now: datetime,
    ) -> dict[FluxGramPeerKey, tuple[RendererBadge, ...]]:
        rows = await session.execute(
            select(PeerBadgeRecord, BadgeDefinitionRecord)
            .join(
                BadgeDefinitionRecord,
                BadgeDefinitionRecord.badge_id == PeerBadgeRecord.badge_id,
            )
            .where(
                tuple_(PeerBadgeRecord.peer_type, PeerBadgeRecord.peer_id).in_(
                    self._database_keys(peers)
                ),
                or_(PeerBadgeRecord.expires_at.is_(None), PeerBadgeRecord.expires_at > now),
            )
            .order_by(
                PeerBadgeRecord.peer_type,
                PeerBadgeRecord.peer_id,
                PeerBadgeRecord.position,
            )
        )
        badges: defaultdict[FluxGramPeerKey, list[RendererBadge]] = defaultdict(list)

        for assignment, definition in rows:
            peer = FluxGramPeerKey(assignment.peer_type, assignment.peer_id)
            badges[peer].append(
                RendererBadge(
                    badge_id=definition.badge_id,
                    custom_emoji_document_id=definition.custom_emoji_document_id,
                    title=definition.title,
                    description=definition.description,
                    particles=definition.particles,
                    expires_at=assignment.expires_at,
                )
            )

        return {peer: tuple(values) for peer, values in badges.items()}

    async def _load_designs(
        self,
        session: AsyncSession,
        peers: Sequence[FluxGramPeerKey],
    ) -> dict[FluxGramPeerKey, ProfileDesign]:
        records = await session.scalars(
            select(ProfileDesignRecord).where(
                tuple_(ProfileDesignRecord.peer_type, ProfileDesignRecord.peer_id).in_(
                    self._database_keys(peers)
                )
            )
        )
        return {
            FluxGramPeerKey(record.peer_type, record.peer_id): ProfileDesign(
                design_id=record.design_id,
                palette_id=record.palette_id,
                pattern_id=record.pattern_id,
                avatar_frame_id=record.avatar_frame_id,
                badge_style_id=record.badge_style_id,
            )
            for record in records
        }

    def _build_result(
        self,
        peer: FluxGramPeerKey,
        profiles: dict[FluxGramPeerKey, PeerProfileRecord],
        badges_by_peer: dict[FluxGramPeerKey, tuple[RendererBadge, ...]],
        designs: dict[FluxGramPeerKey, ProfileDesign],
        now: datetime,
    ) -> ProfileReadResult:
        record = profiles.get(peer)
        badges = badges_by_peer.get(peer, ())
        design = designs.get(peer)
        profile = RendererProfile(badges, design) if badges or design is not None else None
        ttl = self._cache_ttl_seconds if profile is not None else self._negative_cache_ttl_seconds
        expires_at = self._cache_boundary(now, ttl)

        badge_expiries = tuple(
            badge.expires_at
            for badge in badges
            if badge.expires_at is not None
        )
        if badge_expiries:
            expires_at = min(expires_at, *badge_expiries)

        return ProfileReadResult(
            peer=peer,
            revision=record.revision if record is not None else 0,
            expires_at=expires_at,
            profile=profile,
        )

    @staticmethod
    def _database_keys(peers: Sequence[FluxGramPeerKey]) -> list[tuple[object, int]]:
        return [(peer.peer_type, peer.peer_id) for peer in peers]

    @staticmethod
    def _cache_boundary(now: datetime, ttl_seconds: int) -> datetime:
        timestamp = int(now.timestamp())
        boundary = (timestamp // ttl_seconds + 1) * ttl_seconds
        return datetime.fromtimestamp(boundary, UTC)
