from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261007_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

peer_type = postgresql.ENUM("user", "group", "channel", name="peer_type", create_type=False)


def upgrade() -> None:
    postgresql.ENUM("user", "group", "channel", name="peer_type").create(
        op.get_bind(),
        checkfirst=True,
    )

    op.create_table(
        "badge_definitions",
        sa.Column("badge_id", sa.String(length=64), nullable=False),
        sa.Column("custom_emoji_document_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=False),
        sa.Column("particles", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "badge_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="badge_id_format",
        ),
        sa.CheckConstraint(
            "custom_emoji_document_id > 0",
            name="document_id_positive",
        ),
        sa.CheckConstraint("length(title) > 0", name="title_not_empty"),
        sa.PrimaryKeyConstraint("badge_id", name="pk_badge_definitions"),
    )
    op.create_table(
        "peer_profiles",
        sa.Column("peer_type", peer_type, nullable=False),
        sa.Column("peer_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "peer_id > 0 AND peer_id <= 4503599627370495",
            name="peer_id_range",
        ),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        sa.PrimaryKeyConstraint("peer_type", "peer_id", name="pk_peer_profiles"),
    )
    op.create_table(
        "operations",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("operation_type", sa.String(length=32), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("rollbackable", sa.Boolean(), nullable=False),
        sa.Column("reverts_operation_id", sa.String(length=128), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(actor_type = 'telegram_admin' AND actor_id IS NOT NULL) OR "
            "(actor_type = 'system' AND actor_id IS NULL)",
            name="actor_valid",
        ),
        sa.ForeignKeyConstraint(
            ["reverts_operation_id"],
            ["operations.operation_id"],
            name="fk_operations_reverts_operation_id_operations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("operation_id", name="pk_operations"),
        sa.UniqueConstraint(
            "reverts_operation_id",
            name="uq_operations_reverts_operation_id",
        ),
    )
    op.create_table(
        "peer_badges",
        sa.Column("peer_type", peer_type, nullable=False),
        sa.Column("peer_id", sa.BigInteger(), nullable=False),
        sa.Column("badge_id", sa.String(length=64), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "position >= 0 AND position < 8",
            name="position_range",
        ),
        sa.ForeignKeyConstraint(
            ["badge_id"],
            ["badge_definitions.badge_id"],
            name="fk_peer_badges_badge_id_badge_definitions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["peer_type", "peer_id"],
            ["peer_profiles.peer_type", "peer_profiles.peer_id"],
            name="fk_peer_badges_peer_type_peer_profiles",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "peer_type",
            "peer_id",
            "badge_id",
            name="pk_peer_badges",
        ),
        sa.UniqueConstraint(
            "peer_type",
            "peer_id",
            "position",
            name="uq_peer_badges_position",
        ),
    )
    op.create_table(
        "profile_designs",
        sa.Column("peer_type", peer_type, nullable=False),
        sa.Column("peer_id", sa.BigInteger(), nullable=False),
        sa.Column("design_id", sa.String(length=64), nullable=True),
        sa.Column("palette_id", sa.String(length=64), nullable=True),
        sa.Column("pattern_id", sa.String(length=64), nullable=True),
        sa.Column("avatar_frame_id", sa.String(length=64), nullable=True),
        sa.Column("badge_style_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "num_nonnulls(design_id, palette_id, pattern_id, avatar_frame_id, badge_style_id) > 0",
            name="has_value",
        ),
        sa.CheckConstraint(
            "design_id IS NULL OR design_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="design_id_format",
        ),
        sa.CheckConstraint(
            "palette_id IS NULL OR palette_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="palette_id_format",
        ),
        sa.CheckConstraint(
            "pattern_id IS NULL OR pattern_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="pattern_id_format",
        ),
        sa.CheckConstraint(
            "avatar_frame_id IS NULL OR avatar_frame_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="avatar_frame_id_format",
        ),
        sa.CheckConstraint(
            "badge_style_id IS NULL OR badge_style_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="badge_style_id_format",
        ),
        sa.ForeignKeyConstraint(
            ["peer_type", "peer_id"],
            ["peer_profiles.peer_type", "peer_profiles.peer_id"],
            name="fk_profile_designs_peer_type_peer_profiles",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("peer_type", "peer_id", name="pk_profile_designs"),
    )
    op.create_table(
        "peer_aliases",
        sa.Column("source_peer_type", peer_type, nullable=False),
        sa.Column("source_peer_id", sa.BigInteger(), nullable=False),
        sa.Column("target_peer_type", peer_type, nullable=False),
        sa.Column("target_peer_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_peer_type = 'group' AND target_peer_type = 'group'",
            name="groups_only",
        ),
        sa.CheckConstraint(
            "source_peer_type <> target_peer_type OR source_peer_id <> target_peer_id",
            name="source_differs",
        ),
        sa.CheckConstraint(
            "source_peer_id > 0 AND source_peer_id <= 4503599627370495",
            name="source_peer_id_range",
        ),
        sa.ForeignKeyConstraint(
            ["target_peer_type", "target_peer_id"],
            ["peer_profiles.peer_type", "peer_profiles.peer_id"],
            name="fk_peer_aliases_target_peer_type_peer_profiles",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "source_peer_type",
            "source_peer_id",
            name="pk_peer_aliases",
        ),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_key", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("before_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(actor_type = 'telegram_admin' AND actor_id IS NOT NULL) OR "
            "(actor_type = 'system' AND actor_id IS NULL)",
            name="actor_valid",
        ),
        sa.CheckConstraint("ordinal > 0", name="ordinal_positive"),
        sa.CheckConstraint(
            "(before_value IS NOT NULL OR after_value IS NOT NULL) "
            "AND before_value IS DISTINCT FROM after_value",
            name="values_differ",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_audit_log_operation_id_operations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        sa.UniqueConstraint("operation_id", "ordinal", name="uq_audit_log_ordinal"),
    )

    op.execute(
        """
        CREATE FUNCTION reject_audit_log_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_reject_update_delete
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_reject_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_log_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER audit_log_reject_truncate ON audit_log")
    op.execute("DROP TRIGGER audit_log_reject_update_delete ON audit_log")
    op.execute("DROP FUNCTION reject_audit_log_mutation()")
    op.drop_table("audit_log")
    op.drop_table("peer_aliases")
    op.drop_table("profile_designs")
    op.drop_table("peer_badges")
    op.drop_table("operations")
    op.drop_table("peer_profiles")
    op.drop_table("badge_definitions")
    peer_type.drop(op.get_bind(), checkfirst=True)
