import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fluxgram_backend.application.profile_queries import ProfileQueryService
from fluxgram_backend.database.migrations import run_migrations
from fluxgram_backend.database.models import (
    BadgeDefinitionRecord,
    PeerAliasRecord,
    PeerBadgeRecord,
    PeerProfileRecord,
    ProfileDesignRecord,
)
from fluxgram_backend.domain.peer import FluxGramPeerKey, PeerType
from fluxgram_backend.settings import Settings


@pytest.mark.integration
def test_profile_queries_return_renderer_data_aliases_and_tombstones() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    run_migrations(Settings.model_validate({"database_url": database_url}))
    now = datetime(2026, 10, 7, 12, 0, 30, tzinfo=UTC)

    async def check() -> None:
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        canonical = FluxGramPeerKey(PeerType.GROUP, 57002)
        alias = FluxGramPeerKey(PeerType.GROUP, 57001)
        tombstone = FluxGramPeerKey(PeerType.USER, 57003)
        missing = FluxGramPeerKey(PeerType.CHANNEL, 57004)
        badge_expiry = now + timedelta(seconds=25)

        try:
            async with sessions.begin() as session:
                await session.execute(
                    delete(PeerAliasRecord).where(
                        PeerAliasRecord.source_peer_id.in_((alias.peer_id, canonical.peer_id))
                    )
                )
                await session.execute(
                    delete(PeerProfileRecord).where(
                        PeerProfileRecord.peer_id.in_(
                            (alias.peer_id, canonical.peer_id, tombstone.peer_id, missing.peer_id)
                        )
                    )
                )
                definition = await session.get(BadgeDefinitionRecord, "stage7-query")
                if definition is None:
                    session.add(
                        BadgeDefinitionRecord(
                            badge_id="stage7-query",
                            custom_emoji_document_id=5373141891321699086,
                            title="Stage 7",
                            description="Public API integration fixture",
                            particles=True,
                        )
                    )
                expired_definition = await session.get(
                    BadgeDefinitionRecord,
                    "stage7-expired",
                )
                if expired_definition is None:
                    session.add(
                        BadgeDefinitionRecord(
                            badge_id="stage7-expired",
                            custom_emoji_document_id=5373141891321699087,
                            title="Expired",
                            description="Expired integration fixture",
                            particles=False,
                        )
                    )

                session.add_all(
                    [
                        PeerProfileRecord(
                            peer_type=canonical.peer_type,
                            peer_id=canonical.peer_id,
                            revision=9,
                        ),
                        PeerProfileRecord(
                            peer_type=tombstone.peer_type,
                            peer_id=tombstone.peer_id,
                            revision=4,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        PeerAliasRecord(
                            source_peer_type=alias.peer_type,
                            source_peer_id=alias.peer_id,
                            target_peer_type=canonical.peer_type,
                            target_peer_id=canonical.peer_id,
                        ),
                        PeerBadgeRecord(
                            peer_type=canonical.peer_type,
                            peer_id=canonical.peer_id,
                            badge_id="stage7-query",
                            position=0,
                            expires_at=badge_expiry,
                        ),
                        PeerBadgeRecord(
                            peer_type=canonical.peer_type,
                            peer_id=canonical.peer_id,
                            badge_id="stage7-expired",
                            position=1,
                            expires_at=now - timedelta(seconds=1),
                        ),
                        ProfileDesignRecord(
                            peer_type=canonical.peer_type,
                            peer_id=canonical.peer_id,
                            palette_id="midnight",
                        ),
                    ]
                )

            service = ProfileQueryService(
                sessions,
                cache_ttl_seconds=300,
                negative_cache_ttl_seconds=60,
                clock=lambda: now,
            )
            results = await service.get_profiles((missing, alias, tombstone))

            assert [result.peer for result in results] == [missing, canonical, tombstone]
            assert results[0].revision == 0
            assert results[0].profile is None
            assert results[0].expires_at == datetime(2026, 10, 7, 12, 1, tzinfo=UTC)
            assert results[1].revision == 9
            assert results[1].expires_at == badge_expiry
            assert results[1].profile is not None
            assert results[1].profile.design is not None
            assert results[1].profile.design.palette_id == "midnight"
            assert results[1].profile.badges[0].title == "Stage 7"
            assert results[1].profile.badges[0].custom_emoji_document_id == 5373141891321699086
            assert len(results[1].profile.badges) == 1
            assert results[2].revision == 4
            assert results[2].profile is None
            assert results[2].expires_at == datetime(2026, 10, 7, 12, 1, tzinfo=UTC)
        finally:
            await engine.dispose()

    asyncio.run(check())
