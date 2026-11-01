import json
from pathlib import Path

import pytest

from fluxgram_backend.database.models import PluginReputationRecord
from fluxgram_backend.database.plugin_reputation import signed_record_from_row
from fluxgram_backend.reputation.protocol import ProtocolError

ROOT = Path(__file__).resolve().parents[1]


def test_stored_record_metadata_must_match_signed_bytes() -> None:
    golden = json.loads(
        (ROOT / "fixtures/plugin-reputation/v1/golden.json").read_text(encoding="utf-8")
    )
    record = PluginReputationRecord(
        archive_sha256=golden["archive_sha256"],
        developer_key_id=golden["developer_key_id"],
        revision=7,
        issued_at_millis=1_700_000_000_000,
        expires_at_millis=1_700_086_400_000,
        archive_status=1,
        key_status=1,
        authority_key_id=golden["authority_key_id"],
        response=bytes.fromhex(golden["response"]),
        envelope=bytes.fromhex(golden["envelope"]),
        operation_id="test:matching",
    )

    signed = signed_record_from_row(record)

    assert signed.response == bytes.fromhex(golden["response"])
    assert signed.envelope == bytes.fromhex(golden["envelope"])

    record.revision = 6

    with pytest.raises(ProtocolError, match="metadata"):
        signed_record_from_row(record)
