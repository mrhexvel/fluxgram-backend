import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from fluxgram_backend.reputation.protocol import ReputationQuery, TransportFrame
from fluxgram_backend.reputation.verification import verify_signed_record


@dataclass(frozen=True, slots=True)
class SignedReputationRecord:
    response: bytes
    envelope: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "response", bytes(self.response))
        object.__setattr__(self, "envelope", bytes(self.envelope))


class ReputationRecordReader(Protocol):
    async def get_record(self, query: ReputationQuery) -> SignedReputationRecord | None: ...


class PluginReputationService:
    def __init__(
        self,
        reader: ReputationRecordReader,
        authority_key_id: str,
        clock_millis: Callable[[], int] | None = None,
    ) -> None:
        self._reader = reader
        self._authority_key_id = authority_key_id
        self._clock_millis = clock_millis or (lambda: time.time_ns() // 1_000_000)

    async def lookup(self, query: ReputationQuery) -> bytes | None:
        record = await self._reader.get_record(query)
        if record is None:
            return None

        verify_signed_record(
            query,
            record.response,
            record.envelope,
            self._authority_key_id,
            self._clock_millis(),
        )
        return TransportFrame(record.response, record.envelope).encode()
