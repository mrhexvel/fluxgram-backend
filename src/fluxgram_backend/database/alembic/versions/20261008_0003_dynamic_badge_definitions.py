from collections.abc import Sequence

from alembic import op

revision = "20261008_0003"
down_revision = "20261007_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        WITH changed AS (
            UPDATE badge_definitions
            SET custom_emoji_document_id = 5350701541898628901
            WHERE badge_id = 'developer'
              AND custom_emoji_document_id IS DISTINCT FROM 5350701541898628901
            RETURNING badge_id
        )
        UPDATE peer_profiles AS profile
        SET revision = profile.revision + 1,
            updated_at = transaction_timestamp()
        WHERE EXISTS (
            SELECT 1
            FROM peer_badges AS badge
            JOIN changed ON changed.badge_id = badge.badge_id
            WHERE badge.peer_type = profile.peer_type
              AND badge.peer_id = profile.peer_id
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION bump_badge_definition_profile_revisions()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            UPDATE peer_profiles AS profile
            SET revision = profile.revision + 1,
                updated_at = transaction_timestamp()
            WHERE EXISTS (
                SELECT 1
                FROM peer_badges AS badge
                WHERE badge.peer_type = profile.peer_type
                  AND badge.peer_id = profile.peer_id
                  AND badge.badge_id = NEW.badge_id
            );
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER badge_definition_profile_revisions
        AFTER UPDATE OF custom_emoji_document_id, title, description, particles
        ON badge_definitions
        FOR EACH ROW
        WHEN (
            OLD.custom_emoji_document_id IS DISTINCT FROM NEW.custom_emoji_document_id
            OR OLD.title IS DISTINCT FROM NEW.title
            OR OLD.description IS DISTINCT FROM NEW.description
            OR OLD.particles IS DISTINCT FROM NEW.particles
        )
        EXECUTE FUNCTION bump_badge_definition_profile_revisions()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER badge_definition_profile_revisions ON badge_definitions"
    )
    op.execute("DROP FUNCTION bump_badge_definition_profile_revisions()")
    op.execute(
        """
        WITH changed AS (
            UPDATE badge_definitions
            SET custom_emoji_document_id = 5373141891321699086
            WHERE badge_id = 'developer'
              AND custom_emoji_document_id IS DISTINCT FROM 5373141891321699086
            RETURNING badge_id
        )
        UPDATE peer_profiles AS profile
        SET revision = profile.revision + 1,
            updated_at = transaction_timestamp()
        WHERE EXISTS (
            SELECT 1
            FROM peer_badges AS badge
            JOIN changed ON changed.badge_id = badge.badge_id
            WHERE badge.peer_type = profile.peer_type
              AND badge.peer_id = profile.peer_id
        )
        """
    )
