from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from fluxgram_backend.database.metadata import Base


def test_appearance_schema_contains_expected_tables() -> None:
    assert set(Base.metadata.tables) == {
        "audit_log",
        "badge_definitions",
        "operations",
        "peer_aliases",
        "peer_badges",
        "peer_profiles",
        "plugin_reputation_records",
        "profile_designs",
    }


def test_peer_badges_enforce_profile_reference_and_position() -> None:
    table = Base.metadata.tables["peer_badges"]
    foreign_keys = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]
    unique_constraints = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    check_constraints = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]

    assert any(len(constraint.columns) == 2 for constraint in foreign_keys)
    assert any(len(constraint.columns) == 3 for constraint in unique_constraints)
    assert any("position < 8" in str(constraint.sqltext) for constraint in check_constraints)


def test_operations_allow_only_one_rollback() -> None:
    table = Base.metadata.tables["operations"]
    constraints = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    assert any(
        [column.name for column in constraint.columns] == ["reverts_operation_id"]
        for constraint in constraints
    )


def test_plugin_reputation_schema_is_bounded_and_operation_scoped() -> None:
    table = Base.metadata.tables["plugin_reputation_records"]
    checks = [
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    foreign_keys = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    ]
    unique_constraints = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    assert any("revision >= 0" in value for value in checks)
    assert any("octet_length(response)" in value for value in checks)
    assert any("octet_length(envelope)" in value for value in checks)
    assert any("key_status" in value and "developer_key_id" in value for value in checks)
    assert any(
        [column.name for column in constraint.columns] == ["operation_id"]
        for constraint in foreign_keys
    )
    assert any(
        [column.name for column in constraint.columns] == ["operation_id"]
        for constraint in unique_constraints
    )
