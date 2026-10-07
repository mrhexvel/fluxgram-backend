from dataclasses import dataclass
from enum import StrEnum

from fluxgram_backend.domain.peer import FluxGramPeerKey


class ActorType(StrEnum):
    TELEGRAM_ADMIN = "telegram_admin"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class OperationContext:
    operation_id: str
    actor_type: ActorType
    actor_id: int | None

    def __post_init__(self) -> None:
        if not self.operation_id or len(self.operation_id) > 128:
            raise ValueError("operation_id must contain between 1 and 128 characters")

        if self.actor_type is ActorType.TELEGRAM_ADMIN:
            if self.actor_id is None or self.actor_id <= 0:
                raise ValueError("telegram_admin operations require a positive actor_id")
        elif self.actor_id is not None:
            raise ValueError("system operations must not contain actor_id")


@dataclass(frozen=True, slots=True)
class OperationResult:
    operation_id: str
    peer: FluxGramPeerKey
    revision: int
    replayed: bool = False


class AppearanceOperationError(Exception):
    pass


class OperationConflict(AppearanceOperationError):
    pass


class AppearanceNotFound(AppearanceOperationError):
    pass


class RollbackConflict(AppearanceOperationError):
    pass
