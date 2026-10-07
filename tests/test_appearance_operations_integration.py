import asyncio
import os
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fluxgram_backend.application.appearance import AppearanceService
from fluxgram_backend.database.migrations import build_migration_config, run_migrations
from fluxgram_backend.database.models import (
    AuditLogRecord,
    BadgeDefinitionRecord,
    OperationRecord,
    PeerAliasRecord,
    PeerBadgeRecord,
    PeerProfileRecord,
    ProfileDesignRecord,
)
from fluxgram_backend.domain.appearance import BadgeAssignment, ProfileDesign
from fluxgram_backend.domain.operations import (
    ActorType,
    OperationConflict,
    OperationContext,
    RollbackConflict,
)
from fluxgram_backend.domain.peer import FluxGramPeerKey, PeerMigration, PeerType
from fluxgram_backend.settings import Settings


def require_database_url() -> str:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    return database_url


def admin(operation_id: str) -> OperationContext:
    return OperationContext(operation_id, ActorType.TELEGRAM_ADMIN, 1001)


def system(operation_id: str) -> OperationContext:
    return OperationContext(operation_id, ActorType.SYSTEM, None)


@pytest.mark.integration
def test_appearance_mutations_are_revisioned_idempotent_and_reversible() -> None:
    database_url = require_database_url()
    run_migrations(Settings.model_validate({"database_url": database_url}))

    async def check() -> None:
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service = AppearanceService(sessions)
        peer = FluxGramPeerKey(PeerType.USER, 51001)

        try:
            async with sessions.begin() as session:
                await seed_badge(session, "stage5-founder")

            assignment = BadgeAssignment(
                "stage5-founder",
                0,
                datetime(2027, 1, 1, tzinfo=UTC),
            )
            assigned = await service.assign_badge(admin("stage5:assign"), peer, assignment)
            replayed = await service.assign_badge(admin("stage5:assign"), peer, assignment)

            assert assigned.revision == 1
            assert replayed.revision == 1
            assert replayed.replayed

            with pytest.raises(OperationConflict):
                await service.assign_badge(
                    admin("stage5:assign"),
                    peer,
                    BadgeAssignment("stage5-founder", 1),
                )

            designed = await service.set_design(
                admin("stage5:design"),
                peer,
                ProfileDesign(palette_id="midnight"),
            )
            removed = await service.remove_badge(
                admin("stage5:remove"),
                peer,
                "stage5-founder",
            )
            cleared = await service.clear_design(admin("stage5:clear"), peer)
            restored = await service.rollback(admin("stage5:rollback"), "stage5:clear")

            assert [designed.revision, removed.revision, cleared.revision, restored.revision] == [
                2,
                3,
                4,
                5,
            ]

            with pytest.raises(RollbackConflict):
                await service.rollback(admin("stage5:rollback-again"), "stage5:clear")

            with pytest.raises(RollbackConflict):
                await service.rollback(admin("stage5:rollback-stale"), "stage5:design")

            async with sessions() as session:
                profile = await session.get(PeerProfileRecord, (peer.peer_type, peer.peer_id))
                design = await session.get(ProfileDesignRecord, (peer.peer_type, peer.peer_id))
                badge_count = await session.scalar(
                    select(func.count()).select_from(PeerBadgeRecord).where(
                        PeerBadgeRecord.peer_type == peer.peer_type,
                        PeerBadgeRecord.peer_id == peer.peer_id,
                    )
                )
                audit_count = await session.scalar(
                    select(func.count()).select_from(AuditLogRecord).where(
                        AuditLogRecord.operation_id == "stage5:assign"
                    )
                )

            assert profile is not None and profile.revision == 5
            assert design is not None and design.palette_id == "midnight"
            assert badge_count == 0
            assert audit_count == 1

            cycle_peer = FluxGramPeerKey(PeerType.USER, 51002)
            await service.assign_badge(
                admin("stage5:cycle-original"),
                cycle_peer,
                BadgeAssignment("stage5-founder", 0),
            )
            await service.remove_badge(
                admin("stage5:cycle-remove"),
                cycle_peer,
                "stage5-founder",
            )
            await service.assign_badge(
                admin("stage5:cycle-restore"),
                cycle_peer,
                BadgeAssignment("stage5-founder", 0),
            )

            with pytest.raises(RollbackConflict):
                await service.rollback(
                    admin("stage5:cycle-rollback"),
                    "stage5:cycle-original",
                )

            timezone_peer = FluxGramPeerKey(PeerType.USER, 51003)
            offset_expiry = datetime(2027, 1, 1, 3, tzinfo=timezone(timedelta(hours=3)))
            await service.assign_badge(
                admin("stage5:timezone-assign"),
                timezone_peer,
                BadgeAssignment("stage5-founder", 0, offset_expiry),
            )
            timezone_rollback = await service.rollback(
                admin("stage5:timezone-rollback"),
                "stage5:timezone-assign",
            )

            assert timezone_rollback.revision == 2
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.integration
def test_concurrent_duplicate_and_group_migration_are_atomic() -> None:
    database_url = require_database_url()
    run_migrations(Settings.model_validate({"database_url": database_url}))

    async def check() -> None:
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        service = AppearanceService(sessions)
        source = FluxGramPeerKey(PeerType.GROUP, 52001)
        target = FluxGramPeerKey(PeerType.GROUP, 52002)
        parallel = FluxGramPeerKey(PeerType.GROUP, 52003)
        final_target = FluxGramPeerKey(PeerType.GROUP, 52006)

        try:
            async with sessions.begin() as session:
                await seed_badge(session, "stage5-group")
                await seed_badge(session, "stage5-parallel-a")
                await seed_badge(session, "stage5-parallel-b")

            parallel_first, parallel_second = await asyncio.gather(
                service.assign_badge(
                    admin("stage5:parallel-a"),
                    parallel,
                    BadgeAssignment("stage5-parallel-a", 0),
                ),
                service.assign_badge(
                    admin("stage5:parallel-b"),
                    parallel,
                    BadgeAssignment("stage5-parallel-b", 1),
                ),
            )

            assert sorted((parallel_first.revision, parallel_second.revision)) == [1, 2]

            context = admin("stage5:concurrent")
            assignment = BadgeAssignment("stage5-group", 0)
            first, second = await asyncio.gather(
                service.assign_badge(context, source, assignment),
                service.assign_badge(context, source, assignment),
            )

            assert sorted((first.replayed, second.replayed)) == [False, True]

            migrated = await service.migrate_group(
                system("stage5:migrate"),
                PeerMigration(source, target),
            )
            assert migrated.peer == target
            assert migrated.revision == 2

            async with sessions() as session:
                source_profile = await session.get(
                    PeerProfileRecord,
                    (source.peer_type, source.peer_id),
                )
                target_profile = await session.get(
                    PeerProfileRecord,
                    (target.peer_type, target.peer_id),
                )
                alias = await session.get(PeerAliasRecord, (source.peer_type, source.peer_id))
                badge = await session.get(
                    PeerBadgeRecord,
                    (target.peer_type, target.peer_id, "stage5-group"),
                )

            assert source_profile is None
            assert target_profile is not None and target_profile.revision == 2
            assert alias is not None and alias.target_peer_id == target.peer_id
            assert badge is not None

            with pytest.raises(OperationConflict):
                await service.assign_badge(
                    admin("stage5:alias-write"),
                    source,
                    BadgeAssignment("stage5-group", 1),
                )

            remigrated = await service.migrate_group(
                system("stage5:remigrate"),
                PeerMigration(target, final_target),
            )
            assert remigrated.revision == 3

            async with sessions() as session:
                source_alias = await session.get(
                    PeerAliasRecord,
                    (source.peer_type, source.peer_id),
                )
                target_alias = await session.get(
                    PeerAliasRecord,
                    (target.peer_type, target.peer_id),
                )
                final_profile = await session.get(
                    PeerProfileRecord,
                    (final_target.peer_type, final_target.peer_id),
                )
                alias_audits = await session.scalar(
                    select(func.count()).select_from(AuditLogRecord).where(
                        AuditLogRecord.operation_id == "stage5:remigrate",
                        AuditLogRecord.resource_type == "peer_alias",
                    )
                )

            assert source_alias is not None
            assert source_alias.target_peer_id == final_target.peer_id
            assert target_alias is not None
            assert target_alias.target_peer_id == final_target.peer_id
            assert final_profile is not None and final_profile.revision == 3
            assert alias_audits == 2

            conflict_source = FluxGramPeerKey(PeerType.GROUP, 52004)
            conflict_target = FluxGramPeerKey(PeerType.GROUP, 52005)
            await service.assign_badge(
                admin("stage5:conflict-source"),
                conflict_source,
                BadgeAssignment("stage5-group", 0),
            )
            await service.set_design(
                admin("stage5:conflict-target"),
                conflict_target,
                ProfileDesign(design_id="occupied"),
            )

            with pytest.raises(OperationConflict):
                await service.migrate_group(
                    system("stage5:conflict-migration"),
                    PeerMigration(conflict_source, conflict_target),
                )

            async with sessions() as session:
                conflict_source_profile = await session.get(
                    PeerProfileRecord,
                    (conflict_source.peer_type, conflict_source.peer_id),
                )
                conflict_target_profile = await session.get(
                    PeerProfileRecord,
                    (conflict_target.peer_type, conflict_target.peer_id),
                )
                failed_operation = await session.get(
                    OperationRecord,
                    "stage5:conflict-migration",
                )

            assert conflict_source_profile is not None
            assert conflict_target_profile is not None
            assert failed_operation is None
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.integration
def test_audit_log_rejects_mutation() -> None:
    database_url = require_database_url()
    run_migrations(Settings.model_validate({"database_url": database_url}))

    async def check() -> None:
        engine = create_async_engine(database_url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        try:
            async with sessions.begin() as session:
                operation_id = "stage5:audit-trigger"
                session.add(
                    OperationRecord(
                        operation_id=operation_id,
                        operation_type="trigger_test",
                        request_fingerprint="0" * 64,
                        actor_type="system",
                        actor_id=None,
                        rollbackable=False,
                        reverts_operation_id=None,
                        result={"status": "created"},
                    )
                )
                await session.flush()
                session.add(
                    AuditLogRecord(
                        operation_id=operation_id,
                        ordinal=1,
                        resource_type="trigger_test",
                        resource_key={"id": "stage5"},
                        before_value=None,
                        after_value={"status": "created"},
                        actor_type="system",
                        actor_id=None,
                    )
                )

            statements = (
                "UPDATE audit_log SET resource_type = 'changed' "
                "WHERE operation_id = :operation_id",
                "DELETE FROM audit_log WHERE operation_id = :operation_id",
                "TRUNCATE audit_log",
            )
            for statement in statements:
                with pytest.raises(DBAPIError):
                    async with engine.begin() as connection:
                        await connection.execute(
                            text(statement),
                            {"operation_id": operation_id},
                        )
        finally:
            await engine.dispose()

    asyncio.run(check())


@pytest.mark.integration
def test_migration_downgrade_and_upgrade_round_trip() -> None:
    database_url = require_database_url()
    settings = Settings.model_validate({"database_url": database_url})
    config = build_migration_config(settings)

    command.downgrade(config, "base")
    command.upgrade(config, "head")


async def seed_badge(session: AsyncSession, badge_id: str) -> None:
    existing = await session.get(BadgeDefinitionRecord, badge_id)
    if existing is not None:
        return

    session.add(
        BadgeDefinitionRecord(
            badge_id=badge_id,
            custom_emoji_document_id=5373141891321699086,
            title="Stage 5",
            description="Integration fixture",
            particles=True,
        )
    )
