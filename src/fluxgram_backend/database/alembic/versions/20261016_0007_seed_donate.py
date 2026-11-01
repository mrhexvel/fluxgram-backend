from collections.abc import Sequence

from alembic import op

revision = "20261016_0007"
down_revision = "20261015_0006"
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
            'donate',
            5350701541898628901,
            'Поддерживает разработку FluxGram',
            'за что получил уникальный значок.',
            true
        )
        ON CONFLICT (badge_id) DO NOTHING
        """
    )


def downgrade() -> None:
    pass
