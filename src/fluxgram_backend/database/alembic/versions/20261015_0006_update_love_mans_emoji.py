from collections.abc import Sequence

from alembic import op

revision = "20261015_0006"
down_revision = "20261015_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE badge_definitions
        SET custom_emoji_document_id = 5373141891321699086
        WHERE badge_id = 'love_mans'
          AND custom_emoji_document_id <> 5373141891321699086
        """
    )


def downgrade() -> None:
    pass
