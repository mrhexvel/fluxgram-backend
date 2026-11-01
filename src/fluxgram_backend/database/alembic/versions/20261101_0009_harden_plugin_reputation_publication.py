"""Harden plugin reputation publication.

Revision ID: 20261101_0009
Revises: 20261101_0008
Create Date: 2026-11-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261101_0009"
down_revision: str | None = "20261101_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "LOCK TABLE public.plugin_reputation_records IN SHARE ROW EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.plugin_reputation_records AS reputation
                JOIN public.operations AS operation
                  ON operation.operation_id = reputation.operation_id
                WHERE operation.operation_type IS DISTINCT FROM 'publish_plugin_reputation'
                   OR operation.result IS DISTINCT FROM jsonb_build_object(
                       'archive_sha256', reputation.archive_sha256,
                       'developer_key_id', nullif(reputation.developer_key_id, ''),
                       'revision', reputation.revision
                   )
                   OR operation.rollbackable IS DISTINCT FROM false
                   OR operation.reverts_operation_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'existing plugin reputation operation is invalid';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_plugin_reputation_record() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            previous_revision bigint;
            operation_kind text;
            operation_result jsonb;
            operation_rollbackable boolean;
            operation_reverts text;
            expected_result jsonb;
        BEGIN
            PERFORM pg_advisory_xact_lock(
                hashtextextended(
                    'reputation_identity:' || NEW.archive_sha256 || ':' || NEW.developer_key_id,
                    0
                )
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

            SELECT operation_type, result, rollbackable, reverts_operation_id
            INTO operation_kind, operation_result, operation_rollbackable, operation_reverts
            FROM public.operations
            WHERE operation_id = NEW.operation_id
            FOR UPDATE;

            expected_result := jsonb_build_object(
                'archive_sha256', NEW.archive_sha256,
                'developer_key_id', nullif(NEW.developer_key_id, ''),
                'revision', NEW.revision
            );

            IF operation_kind IS DISTINCT FROM 'publish_plugin_reputation'
               OR operation_result IS DISTINCT FROM expected_result
               OR operation_rollbackable IS DISTINCT FROM false
               OR operation_reverts IS NOT NULL THEN
                RAISE EXCEPTION 'plugin reputation operation is invalid';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.plugin_reputation_records) THEN
                RAISE EXCEPTION 'cannot downgrade plugin reputation publication with records';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION validate_plugin_reputation_record() RETURNS trigger
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
