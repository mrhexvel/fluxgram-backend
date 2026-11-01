from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "20261101_0008"
down_revision = "20261016_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plugin_reputation_records",
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "developer_key_id",
            sa.String(length=69),
            server_default="",
            nullable=False,
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("issued_at_millis", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_millis", sa.BigInteger(), nullable=False),
        sa.Column("archive_status", sa.SmallInteger(), nullable=False),
        sa.Column("key_status", sa.SmallInteger(), nullable=False),
        sa.Column("authority_key_id", sa.String(length=69), nullable=False),
        sa.Column("response", sa.LargeBinary(length=128), nullable=False),
        sa.Column("envelope", sa.LargeBinary(length=512), nullable=False),
        sa.Column("operation_id", sa.String(length=128), nullable=False),
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
        sa.CheckConstraint("revision >= 0", name="revision_non_negative"),
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
            "authority_key_id ~ '^p256:[0-9a-f]{64}$'",
            name="authority_key_id_format",
        ),
        sa.CheckConstraint(
            "octet_length(response) > 0 AND octet_length(response) <= 128",
            name="response_size",
        ),
        sa.CheckConstraint(
            "octet_length(envelope) > 0 AND octet_length(envelope) <= 512",
            name="envelope_size",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.operation_id"],
            name="fk_plugin_reputation_records_operation_id_operations",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "archive_sha256",
            "developer_key_id",
            "revision",
            name="pk_plugin_reputation_records",
        ),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_plugin_reputation_records_operation_id",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION validate_plugin_reputation_record() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            previous_revision bigint;
            operation_kind text;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(NEW.archive_sha256 || ':' || NEW.developer_key_id, 0)
            );

            SELECT max(revision)
            INTO previous_revision
            FROM public.plugin_reputation_records
            WHERE archive_sha256 = NEW.archive_sha256
              AND developer_key_id = NEW.developer_key_id;

            IF previous_revision IS NULL AND NEW.revision <> 0 THEN
                RAISE EXCEPTION 'first plugin reputation revision must be zero';
            END IF;
            IF previous_revision IS NOT NULL AND NEW.revision <> previous_revision + 1 THEN
                RAISE EXCEPTION 'plugin reputation revision must be sequential';
            END IF;

            SELECT operation_type
            INTO operation_kind
            FROM public.operations
            WHERE operation_id = NEW.operation_id;

            IF operation_kind IS DISTINCT FROM 'publish_plugin_reputation' THEN
                RAISE EXCEPTION 'plugin reputation operation type is invalid';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER plugin_reputation_validate_insert
        BEFORE INSERT ON plugin_reputation_records
        FOR EACH ROW EXECUTE FUNCTION validate_plugin_reputation_record()
        """
    )
    op.execute(
        """
        CREATE FUNCTION audit_plugin_reputation_record() RETURNS trigger
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
                NEW.operation_id,
                1,
                'plugin_reputation',
                jsonb_build_object(
                    'archive_sha256', NEW.archive_sha256,
                    'developer_key_id', nullif(NEW.developer_key_id, '')
                ),
                NULL,
                jsonb_build_object(
                    'revision', NEW.revision,
                    'archive_status', NEW.archive_status,
                    'key_status', NEW.key_status,
                    'authority_key_id', NEW.authority_key_id,
                    'issued_at_millis', NEW.issued_at_millis,
                    'expires_at_millis', NEW.expires_at_millis,
                    'response_sha256', encode(sha256(NEW.response), 'hex'),
                    'envelope_sha256', encode(sha256(NEW.envelope), 'hex')
                ),
                operation.actor_type,
                operation.actor_id
            FROM public.operations AS operation
            WHERE operation.operation_id = NEW.operation_id;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER plugin_reputation_audit_insert
        AFTER INSERT ON plugin_reputation_records
        FOR EACH ROW EXECUTE FUNCTION audit_plugin_reputation_record()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_plugin_reputation_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'plugin_reputation_records is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER plugin_reputation_reject_update_delete
        BEFORE UPDATE OR DELETE ON plugin_reputation_records
        FOR EACH ROW EXECUTE FUNCTION reject_plugin_reputation_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER plugin_reputation_reject_truncate
        BEFORE TRUNCATE ON plugin_reputation_records
        FOR EACH STATEMENT EXECUTE FUNCTION reject_plugin_reputation_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_plugin_reputation_operation_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.plugin_reputation_records
                WHERE operation_id = OLD.operation_id
            ) THEN
                RAISE EXCEPTION 'published plugin reputation operation is immutable';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER operations_reject_plugin_reputation_mutation
        BEFORE UPDATE OR DELETE ON operations
        FOR EACH ROW EXECUTE FUNCTION reject_plugin_reputation_operation_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.plugin_reputation_records) THEN
                RAISE EXCEPTION 'cannot downgrade with published plugin reputation records';
            END IF;
        END;
        $$
        """
    )
    op.execute("DROP TRIGGER operations_reject_plugin_reputation_mutation ON operations")
    op.execute("DROP FUNCTION reject_plugin_reputation_operation_mutation()")
    op.execute(
        "DROP TRIGGER plugin_reputation_reject_truncate ON plugin_reputation_records"
    )
    op.execute(
        "DROP TRIGGER plugin_reputation_reject_update_delete ON plugin_reputation_records"
    )
    op.execute("DROP FUNCTION reject_plugin_reputation_mutation()")
    op.execute("DROP TRIGGER plugin_reputation_audit_insert ON plugin_reputation_records")
    op.execute("DROP FUNCTION audit_plugin_reputation_record()")
    op.execute("DROP TRIGGER plugin_reputation_validate_insert ON plugin_reputation_records")
    op.execute("DROP FUNCTION validate_plugin_reputation_record()")
    op.drop_table("plugin_reputation_records")
