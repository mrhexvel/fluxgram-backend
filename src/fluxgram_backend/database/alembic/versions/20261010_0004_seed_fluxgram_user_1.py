from collections.abc import Sequence

from alembic import op

revision = "20261010_0004"
down_revision = "20261008_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO badge_definitions (
            badge_id,
            custom_emoji_document_id,
            title,
            description,
            particles
        ) VALUES (
            'fluxgram_user_1',
            5350701541898628901,
            'FluxGram User #1',
            '',
            true
        )
        ON CONFLICT (badge_id) DO NOTHING
        """
    )


def downgrade() -> None:
    pass
