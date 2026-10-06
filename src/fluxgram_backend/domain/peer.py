from dataclasses import dataclass
from enum import StrEnum

MAX_TELEGRAM_ID = (1 << 52) - 1


class PeerType(StrEnum):
    USER = "user"
    GROUP = "group"
    CHANNEL = "channel"


@dataclass(frozen=True, slots=True)
class FluxGramPeerKey:
    peer_type: PeerType
    peer_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.peer_type, PeerType):
            raise TypeError("peer_type must be a PeerType")

        if type(self.peer_id) is not int:
            raise TypeError("peer_id must be an integer")

        if self.peer_id <= 0:
            raise ValueError("peer_id must be positive")

        if self.peer_id > MAX_TELEGRAM_ID:
            raise ValueError("peer_id must fit within Telegram's 52-bit identifier range")


@dataclass(frozen=True, slots=True)
class PeerMigration:
    source: FluxGramPeerKey
    target: FluxGramPeerKey

    def __post_init__(self) -> None:
        if self.source.peer_type is not PeerType.GROUP:
            raise ValueError("migration source must be a group")

        if self.target.peer_type is not PeerType.GROUP:
            raise ValueError("migration target must be a group")

        if self.source == self.target:
            raise ValueError("migration source and target must differ")
