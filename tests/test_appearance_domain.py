from datetime import UTC, datetime

import pytest

from fluxgram_backend.domain.appearance import BadgeAssignment, BadgeDefinition, ProfileDesign
from fluxgram_backend.domain.operations import ActorType, OperationContext


def test_badge_definition_validates_public_contract_limits() -> None:
    definition = BadgeDefinition(
        badge_id="founder",
        custom_emoji_document_id=123,
        title="Founder",
    )

    assert definition.badge_id == "founder"

    with pytest.raises(ValueError):
        BadgeDefinition(
            badge_id="Invalid ID",
            custom_emoji_document_id=123,
            title="Founder",
        )


def test_badge_assignment_requires_position_and_aware_expiry() -> None:
    assignment = BadgeAssignment(
        badge_id="founder",
        position=7,
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert assignment.position == 7

    with pytest.raises(ValueError):
        BadgeAssignment(badge_id="founder", position=8)

    with pytest.raises(ValueError):
        BadgeAssignment(
            badge_id="founder",
            position=0,
            expires_at=datetime(2027, 1, 1),
        )


def test_profile_design_requires_at_least_one_stable_identifier() -> None:
    assert ProfileDesign(palette_id="midnight").palette_id == "midnight"

    with pytest.raises(ValueError):
        ProfileDesign()

    with pytest.raises(ValueError):
        ProfileDesign(design_id="invalid value")


def test_operation_actor_invariants() -> None:
    admin = OperationContext("telegram:update:1", ActorType.TELEGRAM_ADMIN, 42)
    system = OperationContext("system:migration:1", ActorType.SYSTEM, None)

    assert admin.actor_id == 42
    assert system.actor_id is None

    with pytest.raises(ValueError):
        OperationContext("telegram:update:2", ActorType.TELEGRAM_ADMIN, None)

    with pytest.raises(ValueError):
        OperationContext("system:migration:2", ActorType.SYSTEM, 42)
