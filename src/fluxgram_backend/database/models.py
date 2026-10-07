from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fluxgram_backend.database.metadata import Base
from fluxgram_backend.domain.peer import PeerType

PEER_TYPE_ENUM = Enum(
    PeerType,
    name="peer_type",
    values_callable=lambda values: [value.value for value in values],
)


class BadgeDefinitionRecord(Base):
    __tablename__ = "badge_definitions"
    __table_args__ = (
        CheckConstraint("badge_id ~ '^[a-z0-9][a-z0-9_-]*$'", name="badge_id_format"),
        CheckConstraint("custom_emoji_document_id > 0", name="document_id_positive"),
        CheckConstraint("length(title) > 0", name="title_not_empty"),
    )

    badge_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    custom_emoji_document_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")
    particles: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.transaction_timestamp(),
    )


class PeerProfileRecord(Base):
    __tablename__ = "peer_profiles"
    __table_args__ = (
        CheckConstraint("peer_id > 0 AND peer_id <= 4503599627370495", name="peer_id_range"),
        CheckConstraint("revision > 0", name="revision_positive"),
    )

    peer_type: Mapped[PeerType] = mapped_column(PEER_TYPE_ENUM, primary_key=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.transaction_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.transaction_timestamp(),
        onupdate=func.transaction_timestamp(),
    )


class PeerBadgeRecord(Base):
    __tablename__ = "peer_badges"
    __table_args__ = (
        ForeignKeyConstraint(
            ["peer_type", "peer_id"],
            ["peer_profiles.peer_type", "peer_profiles.peer_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("peer_type", "peer_id", "position", name="uq_peer_badges_position"),
        CheckConstraint("position >= 0 AND position < 8", name="position_range"),
    )

    peer_type: Mapped[PeerType] = mapped_column(PEER_TYPE_ENUM, primary_key=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    badge_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("badge_definitions.badge_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProfileDesignRecord(Base):
    __tablename__ = "profile_designs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["peer_type", "peer_id"],
            ["peer_profiles.peer_type", "peer_profiles.peer_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "num_nonnulls(design_id, palette_id, pattern_id, avatar_frame_id, badge_style_id) > 0",
            name="has_value",
        ),
        CheckConstraint(
            "design_id IS NULL OR design_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="design_id_format",
        ),
        CheckConstraint(
            "palette_id IS NULL OR palette_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="palette_id_format",
        ),
        CheckConstraint(
            "pattern_id IS NULL OR pattern_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="pattern_id_format",
        ),
        CheckConstraint(
            "avatar_frame_id IS NULL OR avatar_frame_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="avatar_frame_id_format",
        ),
        CheckConstraint(
            "badge_style_id IS NULL OR badge_style_id ~ '^[a-z0-9][a-z0-9_-]*$'",
            name="badge_style_id_format",
        ),
    )

    peer_type: Mapped[PeerType] = mapped_column(PEER_TYPE_ENUM, primary_key=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    design_id: Mapped[str | None] = mapped_column(String(64))
    palette_id: Mapped[str | None] = mapped_column(String(64))
    pattern_id: Mapped[str | None] = mapped_column(String(64))
    avatar_frame_id: Mapped[str | None] = mapped_column(String(64))
    badge_style_id: Mapped[str | None] = mapped_column(String(64))


class PeerAliasRecord(Base):
    __tablename__ = "peer_aliases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["target_peer_type", "target_peer_id"],
            ["peer_profiles.peer_type", "peer_profiles.peer_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "source_peer_type = 'group' AND target_peer_type = 'group'",
            name="groups_only",
        ),
        CheckConstraint(
            "source_peer_type <> target_peer_type OR source_peer_id <> target_peer_id",
            name="source_differs",
        ),
        CheckConstraint(
            "source_peer_id > 0 AND source_peer_id <= 4503599627370495",
            name="source_peer_id_range",
        ),
    )

    source_peer_type: Mapped[PeerType] = mapped_column(PEER_TYPE_ENUM, primary_key=True)
    source_peer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    target_peer_type: Mapped[PeerType] = mapped_column(PEER_TYPE_ENUM)
    target_peer_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.transaction_timestamp(),
    )


class OperationRecord(Base):
    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint(
            "(actor_type = 'telegram_admin' AND actor_id IS NOT NULL) OR "
            "(actor_type = 'system' AND actor_id IS NULL)",
            name="actor_valid",
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation_type: Mapped[str] = mapped_column(String(32))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    rollbackable: Mapped[bool] = mapped_column(Boolean)
    reverts_operation_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("operations.operation_id", ondelete="RESTRICT"),
        unique=True,
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.transaction_timestamp(),
    )


class AuditLogRecord(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        UniqueConstraint("operation_id", "ordinal", name="uq_audit_log_ordinal"),
        CheckConstraint("ordinal > 0", name="ordinal_positive"),
        CheckConstraint(
            "(before_value IS NOT NULL OR after_value IS NOT NULL) "
            "AND before_value IS DISTINCT FROM after_value",
            name="values_differ",
        ),
        CheckConstraint(
            "(actor_type = 'telegram_admin' AND actor_id IS NOT NULL) OR "
            "(actor_type = 'system' AND actor_id IS NULL)",
            name="actor_valid",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("operations.operation_id", ondelete="RESTRICT"),
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger)
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_key: Mapped[dict[str, Any]] = mapped_column(JSONB)
    before_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.transaction_timestamp(),
    )
