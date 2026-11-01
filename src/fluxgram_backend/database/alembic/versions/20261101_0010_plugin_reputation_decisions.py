from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "20261101_0010"
down_revision = "20261101_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE plugin_reputation_decision_sequence AS bigint")
    op.create_table(
        "plugin_reputation_decisions",
        sa.Column("sequence_id", sa.BigInteger(), nullable=False),
        sa.Column("decision_operation_id", sa.String(length=128), nullable=False),
        sa.Column("publication_operation_id", sa.String(length=128), nullable=False),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "developer_key_id",
            sa.String(length=69),
            server_default="",
            nullable=False,
        ),
        sa.Column("issued_at_millis", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_millis", sa.BigInteger(), nullable=False),
        sa.Column("archive_status", sa.SmallInteger(), nullable=False),
        sa.Column("key_status", sa.SmallInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("published_revision", sa.BigInteger(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("transaction_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "archive_sha256 ~ '^[0-9a-f]{64}$'",
            name="archive_sha256_format",
        ),
        sa.CheckConstraint(
            "developer_key_id = '' OR developer_key_id ~ '^p256:[0-9a-f]{64}$'",
            name="developer_key_id_format",
        ),
        sa.CheckConstraint(
            "issued_at_millis >= 0 "
            "AND expires_at_millis > issued_at_millis "
            "AND expires_at_millis - issued_at_millis <= 2592000000",
            name="validity_range",
        ),
        sa.CheckConstraint("archive_status IN (1, 2)", name="archive_status_valid"),
        sa.CheckConstraint("key_status IN (0, 1, 2)", name="key_status_valid"),
        sa.CheckConstraint(
            "(developer_key_id = '' AND key_status = 0) "
            "OR (developer_key_id <> '' AND key_status IN (1, 2))",
            name="key_status_matches_identity",
        ),
        sa.CheckConstraint(
            "publication_operation_id <> ''",
            name="publication_operation_id_non_empty",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'published', 'failed')",
            name="state_valid",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        sa.CheckConstraint(
            "lease_token IS NULL OR "
            "lease_token ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'",
            name="lease_token_format",
        ),
        sa.CheckConstraint(
            "lease_owner IS NULL OR lease_owner ~ '^[A-Za-z0-9._:-]{1,128}$'",
            name="lease_owner_format",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR last_error_code ~ '^[a-z][a-z0-9_]{0,63}$'",
            name="last_error_code_format",
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND lease_token IS NULL AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL AND published_revision IS NULL "
            "AND completed_at IS NULL) OR "
            "(state = 'claimed' AND lease_token IS NOT NULL AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND attempt_count > 0 "
            "AND last_error_code IS NULL AND published_revision IS NULL "
            "AND completed_at IS NULL) OR "
            "(state = 'published' AND lease_token IS NULL AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL AND last_error_code IS NULL "
            "AND published_revision IS NOT NULL AND published_revision >= 0 "
            "AND completed_at IS NOT NULL) OR "
            "(state = 'failed' AND lease_token IS NULL AND lease_owner IS NULL "
            "AND lease_expires_at IS NULL AND last_error_code IS NOT NULL "
            "AND published_revision IS NULL AND completed_at IS NOT NULL)",
            name="state_fields_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["decision_operation_id"],
            ["operations.operation_id"],
            name="fk_plugin_reputation_decisions_decision_operation_id_operations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "sequence_id",
            name="pk_plugin_reputation_decisions",
        ),
        sa.UniqueConstraint(
            "decision_operation_id",
            name="uq_plugin_reputation_decisions_decision_operation_id",
        ),
        sa.UniqueConstraint(
            "publication_operation_id",
            name="uq_plugin_reputation_decisions_publication_operation_id",
        ),
    )
    op.execute(
        "ALTER SEQUENCE plugin_reputation_decision_sequence "
        "OWNED BY plugin_reputation_decisions.sequence_id"
    )
    op.create_index(
        "ix_plugin_reputation_decisions_claimable",
        "plugin_reputation_decisions",
        ["available_at", "sequence_id"],
        unique=False,
        postgresql_where=sa.text("state IN ('pending', 'claimed')"),
    )
    op.create_index(
        "ix_plugin_reputation_decisions_identity_order",
        "plugin_reputation_decisions",
        ["archive_sha256", "developer_key_id", "sequence_id"],
        unique=False,
        postgresql_where=sa.text("state IN ('pending', 'claimed')"),
    )
    op.execute(
        """
        CREATE FUNCTION validate_plugin_reputation_decision_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            operation_row public.operations%ROWTYPE;
            expected_result jsonb;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'reputation_decision_identity:' || NEW.archive_sha256 || ':' ||
                    NEW.developer_key_id,
                    0
                )
            );
            NEW.sequence_id := nextval('plugin_reputation_decision_sequence');

            SELECT *
            INTO operation_row
            FROM public.operations
            WHERE operation_id = NEW.decision_operation_id
            FOR UPDATE;

            expected_result := jsonb_build_object(
                'archive_sha256', NEW.archive_sha256,
                'developer_key_id', nullif(NEW.developer_key_id, ''),
                'archive_status', NEW.archive_status,
                'key_status', NEW.key_status,
                'issued_at_millis', NEW.issued_at_millis,
                'expires_at_millis', NEW.expires_at_millis,
                'publication_operation_id', NEW.publication_operation_id
            );

            IF operation_row.operation_type IS DISTINCT FROM 'queue_plugin_reputation'
               OR operation_row.actor_type IS DISTINCT FROM 'telegram_admin'
               OR operation_row.actor_id IS NULL
               OR operation_row.rollbackable IS DISTINCT FROM false
               OR operation_row.reverts_operation_id IS NOT NULL
               OR operation_row.result IS DISTINCT FROM expected_result THEN
                RAISE EXCEPTION 'plugin reputation decision operation is invalid';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER plugin_reputation_decision_validate_insert
        BEFORE INSERT ON plugin_reputation_decisions
        FOR EACH ROW EXECUTE FUNCTION validate_plugin_reputation_decision_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION audit_plugin_reputation_decision() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO public.audit_log (
                operation_id,
                ordinal,
                resource_type,
                resource_key,
                before_value,
                after_value,
                actor_type,
                actor_id
            )
            SELECT
                NEW.decision_operation_id,
                1,
                'plugin_reputation_decision',
                jsonb_build_object(
                    'archive_sha256', NEW.archive_sha256,
                    'developer_key_id', nullif(NEW.developer_key_id, '')
                ),
                NULL,
                jsonb_build_object(
                    'publication_operation_id', NEW.publication_operation_id,
                    'archive_status', NEW.archive_status,
                    'key_status', NEW.key_status,
                    'issued_at_millis', NEW.issued_at_millis,
                    'expires_at_millis', NEW.expires_at_millis
                ),
                operation.actor_type,
                operation.actor_id
            FROM public.operations AS operation
            WHERE operation.operation_id = NEW.decision_operation_id;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER plugin_reputation_decision_audit_insert
        AFTER INSERT ON plugin_reputation_decisions
        FOR EACH ROW EXECUTE FUNCTION audit_plugin_reputation_decision()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_plugin_reputation_decision_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(
                NEW.sequence_id,
                NEW.decision_operation_id,
                NEW.publication_operation_id,
                NEW.archive_sha256,
                NEW.developer_key_id,
                NEW.issued_at_millis,
                NEW.expires_at_millis,
                NEW.archive_status,
                NEW.key_status,
                NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.sequence_id,
                OLD.decision_operation_id,
                OLD.publication_operation_id,
                OLD.archive_sha256,
                OLD.developer_key_id,
                OLD.issued_at_millis,
                OLD.expires_at_millis,
                OLD.archive_status,
                OLD.key_status,
                OLD.created_at
            ) THEN
                RAISE EXCEPTION 'plugin reputation decision intent is immutable';
            END IF;

            IF OLD.state = 'pending' THEN
                IF NEW.state <> 'claimed' OR NEW.attempt_count <> OLD.attempt_count + 1 THEN
                    RAISE EXCEPTION 'plugin reputation decision transition is invalid';
                END IF;
            ELSIF OLD.state = 'claimed' THEN
                IF NEW.state = 'claimed' THEN
                    IF NEW.lease_token = OLD.lease_token THEN
                        IF OLD.lease_expires_at <= transaction_timestamp()
                           OR NEW.lease_owner <> OLD.lease_owner
                           OR NEW.attempt_count <> OLD.attempt_count THEN
                            RAISE EXCEPTION 'plugin reputation lease renewal is invalid';
                        END IF;
                    ELSIF OLD.lease_expires_at > transaction_timestamp()
                          OR NEW.attempt_count <> OLD.attempt_count + 1 THEN
                        RAISE EXCEPTION 'plugin reputation lease reclaim is invalid';
                    END IF;
                ELSIF OLD.lease_expires_at <= transaction_timestamp()
                      OR NEW.state NOT IN ('pending', 'published', 'failed')
                      OR NEW.attempt_count <> OLD.attempt_count THEN
                    RAISE EXCEPTION 'plugin reputation decision transition is invalid';
                END IF;
            ELSE
                RAISE EXCEPTION 'completed plugin reputation decision is immutable';
            END IF;

            IF NEW.state = 'claimed'
               AND NEW.lease_expires_at <= transaction_timestamp() THEN
                RAISE EXCEPTION 'plugin reputation lease expiry is invalid';
            END IF;

            IF NEW.state = 'published' AND NOT EXISTS (
                SELECT 1
                FROM public.plugin_reputation_records AS record
                WHERE record.operation_id = NEW.publication_operation_id
                  AND record.archive_sha256 = NEW.archive_sha256
                  AND record.developer_key_id = NEW.developer_key_id
                  AND record.revision = NEW.published_revision
                  AND record.issued_at_millis = NEW.issued_at_millis
                  AND record.expires_at_millis = NEW.expires_at_millis
                  AND record.archive_status = NEW.archive_status
                  AND record.key_status = NEW.key_status
            ) THEN
                RAISE EXCEPTION 'plugin reputation publication does not match decision';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER plugin_reputation_decision_validate_update
        BEFORE UPDATE ON plugin_reputation_decisions
        FOR EACH ROW EXECUTE FUNCTION validate_plugin_reputation_decision_update()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_plugin_reputation_decision_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'plugin_reputation_decisions cannot be deleted or truncated';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER plugin_reputation_decision_reject_delete
        BEFORE DELETE ON plugin_reputation_decisions
        FOR EACH ROW EXECUTE FUNCTION reject_plugin_reputation_decision_delete()
        """
    )
    op.execute(
        """
        CREATE TRIGGER plugin_reputation_decision_reject_truncate
        BEFORE TRUNCATE ON plugin_reputation_decisions
        FOR EACH STATEMENT EXECUTE FUNCTION reject_plugin_reputation_decision_delete()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_plugin_reputation_decision_operation_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.plugin_reputation_decisions
                WHERE decision_operation_id = OLD.operation_id
            ) THEN
                RAISE EXCEPTION 'plugin reputation decision operation is immutable';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER operations_reject_plugin_reputation_decision_mutation
        BEFORE UPDATE OR DELETE ON operations
        FOR EACH ROW EXECUTE FUNCTION reject_plugin_reputation_decision_operation_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.plugin_reputation_decisions) THEN
                RAISE EXCEPTION 'cannot downgrade with plugin reputation decisions';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER operations_reject_plugin_reputation_decision_mutation ON operations"
    )
    op.execute("DROP FUNCTION reject_plugin_reputation_decision_operation_mutation()")
    op.execute(
        "DROP TRIGGER plugin_reputation_decision_reject_truncate "
        "ON plugin_reputation_decisions"
    )
    op.execute(
        "DROP TRIGGER plugin_reputation_decision_reject_delete "
        "ON plugin_reputation_decisions"
    )
    op.execute("DROP FUNCTION reject_plugin_reputation_decision_delete()")
    op.execute(
        "DROP TRIGGER plugin_reputation_decision_validate_update "
        "ON plugin_reputation_decisions"
    )
    op.execute("DROP FUNCTION validate_plugin_reputation_decision_update()")
    op.execute(
        "DROP TRIGGER plugin_reputation_decision_audit_insert "
        "ON plugin_reputation_decisions"
    )
    op.execute("DROP FUNCTION audit_plugin_reputation_decision()")
    op.execute(
        "DROP TRIGGER plugin_reputation_decision_validate_insert "
        "ON plugin_reputation_decisions"
    )
    op.execute("DROP FUNCTION validate_plugin_reputation_decision_insert()")
    op.drop_index(
        "ix_plugin_reputation_decisions_identity_order",
        table_name="plugin_reputation_decisions",
    )
    op.drop_index(
        "ix_plugin_reputation_decisions_claimable",
        table_name="plugin_reputation_decisions",
    )
    op.drop_table("plugin_reputation_decisions")
