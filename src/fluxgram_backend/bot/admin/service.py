from typing import Protocol

from fluxgram_backend.application.plugin_reputation_decisions import ReputationDecision
from fluxgram_backend.application.plugin_reputation_publisher import ReputationPublication
from fluxgram_backend.domain.appearance import BadgeAssignment, BadgeDefinition
from fluxgram_backend.domain.operations import (
    AuditEntry,
    OperationContext,
    OperationResult,
    ProfileSummary,
)
from fluxgram_backend.domain.peer import FluxGramPeerKey


class AdminAppearanceService(Protocol):
    async def list_badge_definitions(self) -> tuple[BadgeDefinition, ...]: ...

    async def get_profile(self, peer: FluxGramPeerKey) -> ProfileSummary | None: ...

    async def get_history(
        self,
        peer: FluxGramPeerKey,
        limit: int = 10,
    ) -> tuple[AuditEntry, ...]: ...

    async def assign_badge(
        self,
        context: OperationContext,
        peer: FluxGramPeerKey,
        assignment: BadgeAssignment,
    ) -> OperationResult: ...

    async def remove_badge(
        self,
        context: OperationContext,
        peer: FluxGramPeerKey,
        badge_id: str,
    ) -> OperationResult: ...

    async def rollback(
        self,
        context: OperationContext,
        original_operation_id: str,
    ) -> OperationResult: ...


class AdminReputationDecisionService(Protocol):
    async def enqueue(
        self,
        context: OperationContext,
        publication: ReputationPublication,
    ) -> ReputationDecision: ...
