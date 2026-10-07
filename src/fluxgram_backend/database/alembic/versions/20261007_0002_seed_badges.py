from collections.abc import Sequence

from alembic import op

revision = "20261007_0002"
down_revision = "20261007_0001"
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
            'developer',
            5373141891321699086,
            'FluxGram Developer',
            'Develops and maintains FluxGram.',
            true
        )
        ON CONFLICT (badge_id) DO NOTHING
        """
    )


def downgrade() -> None:
    pass
