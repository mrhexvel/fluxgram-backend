import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fluxgram_backend.database.models import (
    AuditLogRecord,
    BadgeDefinitionRecord,
    OperationRecord,
    PeerAliasRecord,
    PeerBadgeRecord,
    PeerProfileRecord,
    ProfileDesignRecord,
)
from fluxgram_backend.domain.appearance import BadgeAssignment, BadgeDefinition, ProfileDesign
from fluxgram_backend.domain.operations import (
    AppearanceNotFound,
    AuditEntry,
    OperationConflict,
    OperationContext,
    OperationResult,
    ProfileSummary,
    RollbackConflict,
)
from fluxgram_backend.domain.peer import FluxGramPeerKey, PeerMigration, PeerType

Snapshot = dict[str, Any]


class AppearanceService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list_badge_definitions(self) -> tuple[BadgeDefinition, ...]:
        async with self._sessions() as session:
            records = await session.scalars(
                select(BadgeDefinitionRecord).order_by(BadgeDefinitionRecord.badge_id)
            )
            return tuple(
                BadgeDefinition(
                    badge_id=record.badge_id,
                    custom_emoji_document_id=record.custom_emoji_document_id,
                    title=record.title,
                    description=record.description,
                    particles=record.particles,
                )
                for record in records
            )

    async def get_profile(self, peer: FluxGramPeerKey) -> ProfileSummary | None:
        async with self._sessions() as session:
            resolved_peer = await self._resolve_alias(session, peer)
            profile = await session.get(
                PeerProfileRecord,
                (resolved_peer.peer_type, resolved_peer.peer_id),
            )
            if profile is None:
                return None

            snapshot = await self._snapshot(session, resolved_peer)
            badges = tuple(
                BadgeAssignment(
                    badge_id=value["badge_id"],
                    position=value["position"],
                    expires_at=(
                        datetime.fromisoformat(value["expires_at"])
                        if value["expires_at"] is not None
                        else None
                    ),
                )
                for value in snapshot["badges"]
            )
            design_value = snapshot["design"]
            design = ProfileDesign(**design_value) if design_value is not None else None
            return ProfileSummary(
                peer=resolved_peer,
                revision=profile.revision,
                badges=badges,
                design=design,
            )

    async def get_history(
        self,
        peer: FluxGramPeerKey,
        limit: int = 10,
    ) -> tuple[AuditEntry, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("history limit must be between 1 and 50")

        async with self._sessions() as session:
            resolved_peer = await self._resolve_alias(session, peer)
            rows = await session.execute(
                select(AuditLogRecord, OperationRecord)
                .join(
                    OperationRecord,
                    OperationRecord.operation_id == AuditLogRecord.operation_id,
                )
                .where(
                    AuditLogRecord.resource_type == "peer_profile",
                    AuditLogRecord.resource_key["peer_type"].as_string()
                    == resolved_peer.peer_type.value,
                    AuditLogRecord.resource_key["peer_id"].as_string()
                    == str(resolved_peer.peer_id),
                )
                .order_by(AuditLogRecord.id.desc())
                .limit(limit)
            )
            return tuple(
                AuditEntry(
                    operation_id=operation.operation_id,
                    operation_type=operation.operation_type,
                    actor_id=audit.actor_id,
                    occurred_at=audit.occurred_at,
                )
                for audit, operation in rows
            )

    async def assign_badge(
        self,
        context: OperationContext,
        peer: FluxGramPeerKey,
        assignment: BadgeAssignment,
    ) -> OperationResult:
        intent = {
            "peer": self._peer_value(peer),
            "badge_id": assignment.badge_id,
            "position": assignment.position,
            "expires_at": self._datetime_value(assignment.expires_at),
        }

        async with self._sessions.begin() as session:
            fingerprint = self._fingerprint("assign_badge", context, intent)
            replay = await self._find_replay(session, context, "assign_badge", fingerprint)
            if replay is not None:
                return replay

            definition = await session.get(BadgeDefinitionRecord, assignment.badge_id)
            if definition is None:
                raise AppearanceNotFound(f"badge {assignment.badge_id} does not exist")

            profile, existed = await self._lock_or_create_profile(session, peer)
            before = await self._snapshot(session, peer) if existed else self._empty_snapshot()
            position_badge = await session.scalar(
                select(PeerBadgeRecord).where(
                    PeerBadgeRecord.peer_type == peer.peer_type,
                    PeerBadgeRecord.peer_id == peer.peer_id,
                    PeerBadgeRecord.position == assignment.position,
                    PeerBadgeRecord.badge_id != assignment.badge_id,
                )
            )
            if position_badge is not None:
                raise OperationConflict(f"badge position {assignment.position} is already occupied")

            badge = await session.get(
                PeerBadgeRecord,
                (peer.peer_type, peer.peer_id, assignment.badge_id),
            )
            if badge is None:
                session.add(
                    PeerBadgeRecord(
                        peer_type=peer.peer_type,
                        peer_id=peer.peer_id,
                        badge_id=assignment.badge_id,
                        position=assignment.position,
                        expires_at=assignment.expires_at,
                    )
                )
            else:
                badge.position = assignment.position
                badge.expires_at = assignment.expires_at

            await session.flush()
            after = await self._snapshot(session, peer)
            changed = before != after
            self._advance_revision(profile, existed, changed)

            return await self._finish_operation(
                session,
                context,
                "assign_badge",
                fingerprint,
                peer,
                profile.revision,
                before,
                after,
                changed,
            )

    async def remove_badge(
        self,
        context: OperationContext,
        peer: FluxGramPeerKey,
        badge_id: str,
    ) -> OperationResult:
        intent = {"peer": self._peer_value(peer), "badge_id": badge_id}

        async with self._sessions.begin() as session:
            fingerprint = self._fingerprint("remove_badge", context, intent)
            replay = await self._find_replay(session, context, "remove_badge", fingerprint)
            if replay is not None:
                return replay

            profile = await self._lock_profile(session, peer)
            if profile is None:
                raise AppearanceNotFound("profile does not exist")

            badge = await session.get(PeerBadgeRecord, (peer.peer_type, peer.peer_id, badge_id))
            if badge is None:
                raise AppearanceNotFound(f"badge {badge_id} is not assigned")

            before = await self._snapshot(session, peer)
            await session.delete(badge)
            await session.flush()
            after = await self._snapshot(session, peer)
            self._advance_revision(profile, True, True)

            return await self._finish_operation(
                session,
                context,
                "remove_badge",
                fingerprint,
                peer,
                profile.revision,
                before,
                after,
                True,
            )

    async def set_design(
        self,
        context: OperationContext,
        peer: FluxGramPeerKey,
        design: ProfileDesign,
    ) -> OperationResult:
        intent = {"peer": self._peer_value(peer), "design": self._design_value(design)}

        async with self._sessions.begin() as session:
            fingerprint = self._fingerprint("set_design", context, intent)
            replay = await self._find_replay(session, context, "set_design", fingerprint)
            if replay is not None:
                return replay

            profile, existed = await self._lock_or_create_profile(session, peer)
            before = await self._snapshot(session, peer) if existed else self._empty_snapshot()
            record = await session.get(ProfileDesignRecord, (peer.peer_type, peer.peer_id))
            values = self._design_value(design)

            if record is None:
                session.add(
                    ProfileDesignRecord(
                        peer_type=peer.peer_type,
                        peer_id=peer.peer_id,
                        **values,
                    )
                )
            else:
                for field, value in values.items():
                    setattr(record, field, value)

            await session.flush()
            after = await self._snapshot(session, peer)
            changed = before != after
            self._advance_revision(profile, existed, changed)

            return await self._finish_operation(
                session,
                context,
                "set_design",
                fingerprint,
                peer,
                profile.revision,
                before,
                after,
                changed,
            )

    async def clear_design(
        self,
        context: OperationContext,
        peer: FluxGramPeerKey,
    ) -> OperationResult:
        intent = {"peer": self._peer_value(peer)}

        async with self._sessions.begin() as session:
            fingerprint = self._fingerprint("clear_design", context, intent)
            replay = await self._find_replay(session, context, "clear_design", fingerprint)
            if replay is not None:
                return replay

            profile = await self._lock_profile(session, peer)
            if profile is None:
                raise AppearanceNotFound("profile does not exist")

            design = await session.get(ProfileDesignRecord, (peer.peer_type, peer.peer_id))
            if design is None:
                raise AppearanceNotFound("profile design does not exist")

            before = await self._snapshot(session, peer)
            await session.delete(design)
            await session.flush()
            after = await self._snapshot(session, peer)
            self._advance_revision(profile, True, True)

            return await self._finish_operation(
                session,
                context,
                "clear_design",
                fingerprint,
                peer,
                profile.revision,
                before,
                after,
                True,
            )

    async def migrate_group(
        self,
        context: OperationContext,
        migration: PeerMigration,
    ) -> OperationResult:
        intent = {
            "source": self._peer_value(migration.source),
            "target": self._peer_value(migration.target),
        }

        async with self._sessions.begin() as session:
            fingerprint = self._fingerprint("migrate_group", context, intent)
            replay = await self._find_replay(session, context, "migrate_group", fingerprint)
            if replay is not None:
                return replay

            profiles: dict[FluxGramPeerKey, PeerProfileRecord | None] = {}
            peers = sorted(
                (migration.source, migration.target),
                key=lambda value: value.peer_id,
            )
            for peer in peers:
                profiles[peer] = await self._lock_profile(session, peer)

            source_profile = profiles[migration.source]
            target_profile = profiles[migration.target]
            if source_profile is None:
                raise AppearanceNotFound("migration source profile does not exist")

            if target_profile is not None:
                raise OperationConflict("migration target profile already exists")

            source_snapshot = await self._snapshot(session, migration.source)
            target_profile = PeerProfileRecord(
                peer_type=migration.target.peer_type,
                peer_id=migration.target.peer_id,
                revision=source_profile.revision + 1,
            )
            session.add(target_profile)
            await session.flush()
            await self._restore_snapshot(session, migration.target, source_snapshot)
            await session.flush()

            aliases = list(
                await session.scalars(
                    select(PeerAliasRecord)
                    .where(
                        PeerAliasRecord.target_peer_type == migration.source.peer_type,
                        PeerAliasRecord.target_peer_id == migration.source.peer_id,
                    )
                    .with_for_update()
                )
            )
            await session.execute(
                update(PeerAliasRecord)
                .where(
                    PeerAliasRecord.target_peer_type == migration.source.peer_type,
                    PeerAliasRecord.target_peer_id == migration.source.peer_id,
                )
                .values(
                    target_peer_type=migration.target.peer_type,
                    target_peer_id=migration.target.peer_id,
                )
            )
            await session.delete(source_profile)
            session.add(
                PeerAliasRecord(
                    source_peer_type=migration.source.peer_type,
                    source_peer_id=migration.source.peer_id,
                    target_peer_type=migration.target.peer_type,
                    target_peer_id=migration.target.peer_id,
                )
            )
            after = {
                "source": self._peer_value(migration.source),
                "target": self._peer_value(migration.target),
                "profile": source_snapshot,
            }
            before = {
                "source": self._peer_value(migration.source),
                "profile": source_snapshot,
            }

            result = await self._finish_operation(
                session,
                context,
                "migrate_group",
                fingerprint,
                migration.target,
                target_profile.revision,
                before,
                after,
                True,
                resource_type="group_migration",
                rollbackable=False,
            )
            ordinal = 2
            for alias in aliases:
                self._add_audit(
                    session,
                    context,
                    ordinal,
                    "peer_alias",
                    {
                        "peer_type": alias.source_peer_type.value,
                        "peer_id": str(alias.source_peer_id),
                    },
                    {"target": self._peer_value(migration.source)},
                    {"target": self._peer_value(migration.target)},
                )
                ordinal += 1

            self._add_audit(
                session,
                context,
                ordinal,
                "peer_alias",
                self._peer_value(migration.source),
                None,
                {"target": self._peer_value(migration.target)},
            )
            return result

    async def rollback(
        self,
        context: OperationContext,
        original_operation_id: str,
    ) -> OperationResult:
        intent = {"original_operation_id": original_operation_id}

        async with self._sessions.begin() as session:
            fingerprint = self._fingerprint("rollback", context, intent)
            replay = await self._find_replay(session, context, "rollback", fingerprint)
            if replay is not None:
                return replay

            original = await session.get(
                OperationRecord,
                original_operation_id,
                with_for_update=True,
            )
            if original is None:
                raise AppearanceNotFound("operation does not exist")

            if not original.rollbackable:
                raise RollbackConflict("operation cannot be rolled back")

            reversal = await session.scalar(
                select(OperationRecord).where(
                    OperationRecord.reverts_operation_id == original_operation_id
                )
            )
            if reversal is not None:
                raise RollbackConflict("operation was already rolled back")

            audit = await session.scalar(
                select(AuditLogRecord).where(
                    AuditLogRecord.operation_id == original_operation_id,
                    AuditLogRecord.resource_type == "peer_profile",
                )
            )
            if audit is None or audit.before_value is None or audit.after_value is None:
                raise RollbackConflict("operation has no restorable profile snapshot")

            peer = self._peer_from_result(original.result)
            profile = await self._lock_profile(session, peer)
            if profile is None:
                raise RollbackConflict("profile no longer exists")

            current = await self._snapshot(session, peer)
            original_revision = int(original.result["revision"])
            if profile.revision != original_revision or current != audit.after_value:
                raise RollbackConflict("profile changed after the original operation")

            await self._clear_appearance(session, peer)
            await self._restore_snapshot(session, peer, audit.before_value)
            self._advance_revision(profile, True, True)

            return await self._finish_operation(
                session,
                context,
                "rollback",
                fingerprint,
                peer,
                profile.revision,
                current,
                audit.before_value,
                True,
                rollbackable=False,
                reverts_operation_id=original_operation_id,
            )

    async def _find_replay(
        self,
        session: AsyncSession,
        context: OperationContext,
        operation_type: str,
        fingerprint: str,
    ) -> OperationResult | None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:operation_id, 0))"),
            {"operation_id": f"operation:{context.operation_id}"},
        )
        operation = await session.get(OperationRecord, context.operation_id)
        if operation is None:
            return None

        if (
            operation.operation_type != operation_type
            or operation.request_fingerprint != fingerprint
        ):
            raise OperationConflict("operation_id was already used for a different intent")

        peer = self._peer_from_result(operation.result)
        return OperationResult(
            operation_id=context.operation_id,
            peer=peer,
            revision=int(operation.result["revision"]),
            replayed=True,
        )

    async def _finish_operation(
        self,
        session: AsyncSession,
        context: OperationContext,
        operation_type: str,
        fingerprint: str,
        peer: FluxGramPeerKey,
        revision: int,
        before: Snapshot,
        after: Snapshot,
        changed: bool,
        resource_type: str = "peer_profile",
        rollbackable: bool = True,
        reverts_operation_id: str | None = None,
    ) -> OperationResult:
        result = {
            "peer_type": peer.peer_type.value,
            "peer_id": str(peer.peer_id),
            "revision": str(revision),
        }
        session.add(
            OperationRecord(
                operation_id=context.operation_id,
                operation_type=operation_type,
                request_fingerprint=fingerprint,
                actor_type=context.actor_type.value,
                actor_id=context.actor_id,
                rollbackable=rollbackable and changed,
                reverts_operation_id=reverts_operation_id,
                result=result,
            )
        )
        await session.flush()

        if changed:
            self._add_audit(
                session,
                context,
                1,
                resource_type,
                self._peer_value(peer),
                before,
                after,
            )

        return OperationResult(
            operation_id=context.operation_id,
            peer=peer,
            revision=revision,
        )

    async def _lock_or_create_profile(
        self,
        session: AsyncSession,
        peer: FluxGramPeerKey,
    ) -> tuple[PeerProfileRecord, bool]:
        profile = await self._lock_profile(session, peer)
        if profile is not None:
            return profile, True

        alias = await session.get(PeerAliasRecord, (peer.peer_type, peer.peer_id))
        if alias is not None:
            raise OperationConflict("peer key is an alias and cannot own a profile")

        profile = PeerProfileRecord(
            peer_type=peer.peer_type,
            peer_id=peer.peer_id,
            revision=1,
        )
        session.add(profile)
        await session.flush()
        return profile, False

    async def _lock_profile(
        self,
        session: AsyncSession,
        peer: FluxGramPeerKey,
    ) -> PeerProfileRecord | None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:peer_key, 0))"),
            {"peer_key": f"peer:{peer.peer_type.value}:{peer.peer_id}"},
        )
        return await session.scalar(
            select(PeerProfileRecord)
            .where(
                PeerProfileRecord.peer_type == peer.peer_type,
                PeerProfileRecord.peer_id == peer.peer_id,
            )
            .with_for_update()
        )

    async def _resolve_alias(
        self,
        session: AsyncSession,
        peer: FluxGramPeerKey,
    ) -> FluxGramPeerKey:
        alias = await session.get(PeerAliasRecord, (peer.peer_type, peer.peer_id))
        if alias is None:
            return peer

        return FluxGramPeerKey(alias.target_peer_type, alias.target_peer_id)

    async def _snapshot(self, session: AsyncSession, peer: FluxGramPeerKey) -> Snapshot:
        badges = list(
            await session.scalars(
                select(PeerBadgeRecord)
                .where(
                    PeerBadgeRecord.peer_type == peer.peer_type,
                    PeerBadgeRecord.peer_id == peer.peer_id,
                )
                .order_by(PeerBadgeRecord.position)
            )
        )
        design = await session.get(ProfileDesignRecord, (peer.peer_type, peer.peer_id))
        return {
            "badges": [
                {
                    "badge_id": badge.badge_id,
                    "position": badge.position,
                    "expires_at": self._datetime_value(badge.expires_at),
                }
                for badge in badges
            ],
            "design": self._design_record_value(design),
        }

    async def _clear_appearance(
        self,
        session: AsyncSession,
        peer: FluxGramPeerKey,
    ) -> None:
        await session.execute(
            delete(PeerBadgeRecord).where(
                PeerBadgeRecord.peer_type == peer.peer_type,
                PeerBadgeRecord.peer_id == peer.peer_id,
            )
        )
        await session.execute(
            delete(ProfileDesignRecord).where(
                ProfileDesignRecord.peer_type == peer.peer_type,
                ProfileDesignRecord.peer_id == peer.peer_id,
            )
        )

    async def _restore_snapshot(
        self,
        session: AsyncSession,
        peer: FluxGramPeerKey,
        snapshot: Snapshot,
    ) -> None:
        for badge in snapshot["badges"]:
            expires_at = badge["expires_at"]
            session.add(
                PeerBadgeRecord(
                    peer_type=peer.peer_type,
                    peer_id=peer.peer_id,
                    badge_id=badge["badge_id"],
                    position=badge["position"],
                    expires_at=(
                        datetime.fromisoformat(expires_at) if expires_at is not None else None
                    ),
                )
            )

        design = snapshot["design"]
        if design is not None:
            session.add(
                ProfileDesignRecord(
                    peer_type=peer.peer_type,
                    peer_id=peer.peer_id,
                    **design,
                )
            )

    @staticmethod
    def _advance_revision(profile: PeerProfileRecord, existed: bool, changed: bool) -> None:
        if existed and changed:
            profile.revision += 1

    @staticmethod
    def _empty_snapshot() -> Snapshot:
        return {"badges": [], "design": None}

    @staticmethod
    def _peer_value(peer: FluxGramPeerKey) -> dict[str, str]:
        return {"peer_type": peer.peer_type.value, "peer_id": str(peer.peer_id)}

    @staticmethod
    def _peer_from_result(result: dict[str, Any]) -> FluxGramPeerKey:
        return FluxGramPeerKey(
            peer_type=PeerType(result["peer_type"]),
            peer_id=int(result["peer_id"]),
        )

    @staticmethod
    def _design_value(design: ProfileDesign) -> dict[str, str | None]:
        return {
            "design_id": design.design_id,
            "palette_id": design.palette_id,
            "pattern_id": design.pattern_id,
            "avatar_frame_id": design.avatar_frame_id,
            "badge_style_id": design.badge_style_id,
        }

    @classmethod
    def _design_record_value(
        cls,
        design: ProfileDesignRecord | None,
    ) -> dict[str, str | None] | None:
        if design is None:
            return None

        return {
            "design_id": design.design_id,
            "palette_id": design.palette_id,
            "pattern_id": design.pattern_id,
            "avatar_frame_id": design.avatar_frame_id,
            "badge_style_id": design.badge_style_id,
        }

    @staticmethod
    def _datetime_value(value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat() if value is not None else None

    @staticmethod
    def _add_audit(
        session: AsyncSession,
        context: OperationContext,
        ordinal: int,
        resource_type: str,
        resource_key: dict[str, Any],
        before: Snapshot | None,
        after: Snapshot | None,
    ) -> None:
        session.add(
            AuditLogRecord(
                operation_id=context.operation_id,
                ordinal=ordinal,
                resource_type=resource_type,
                resource_key=resource_key,
                before_value=before,
                after_value=after,
                actor_type=context.actor_type.value,
                actor_id=context.actor_id,
            )
        )

    @staticmethod
    def _fingerprint(
        operation_type: str,
        context: OperationContext,
        intent: dict[str, Any],
    ) -> str:
        value = {
            "operation_type": operation_type,
            "actor_type": context.actor_type.value,
            "actor_id": context.actor_id,
            "intent": intent,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()
