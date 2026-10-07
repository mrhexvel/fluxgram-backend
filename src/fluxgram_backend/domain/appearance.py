import re
from dataclasses import dataclass
from datetime import datetime

STABLE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def validate_stable_id(value: str, field: str) -> None:
    if len(value) > 64 or STABLE_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a stable identifier")


@dataclass(frozen=True, slots=True)
class BadgeDefinition:
    badge_id: str
    custom_emoji_document_id: int
    title: str
    description: str = ""
    particles: bool = False

    def __post_init__(self) -> None:
        validate_stable_id(self.badge_id, "badge_id")

        if self.custom_emoji_document_id <= 0:
            raise ValueError("custom_emoji_document_id must be positive")

        if not self.title or len(self.title) > 64:
            raise ValueError("title must contain between 1 and 64 characters")

        if len(self.description) > 256:
            raise ValueError("description must contain at most 256 characters")


@dataclass(frozen=True, slots=True)
class BadgeAssignment:
    badge_id: str
    position: int
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_stable_id(self.badge_id, "badge_id")

        if not 0 <= self.position < 8:
            raise ValueError("position must be between 0 and 7")

        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")


@dataclass(frozen=True, slots=True)
class ProfileDesign:
    design_id: str | None = None
    palette_id: str | None = None
    pattern_id: str | None = None
    avatar_frame_id: str | None = None
    badge_style_id: str | None = None

    def __post_init__(self) -> None:
        values = {
            "design_id": self.design_id,
            "palette_id": self.palette_id,
            "pattern_id": self.pattern_id,
            "avatar_frame_id": self.avatar_frame_id,
            "badge_style_id": self.badge_style_id,
        }

        if all(value is None for value in values.values()):
            raise ValueError("design must contain at least one identifier")

        for field, value in values.items():
            if value is not None:
                validate_stable_id(value, field)
