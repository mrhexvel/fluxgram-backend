import json
from collections.abc import Hashable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
from pydantic import ValidationError

from fluxgram_backend.contracts.appearance_v1 import (
    BatchProfileRequest,
    BatchProfileResponse,
    PeerKeyDto,
    SingleProfileResponse,
)
from fluxgram_backend.domain.peer import MAX_TELEGRAM_ID, FluxGramPeerKey, PeerType

ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "openapi" / "appearance-v1.yaml"
FIXTURE_PATH = ROOT / "fixtures" / "appearance" / "v1"


def load_json(name: str) -> dict[str, Any]:
    with (FIXTURE_PATH / name).open(encoding="utf-8") as source:
        value = json.load(source)

    if not isinstance(value, dict):
        raise TypeError(f"fixture {name} must contain an object")

    return value


def load_openapi() -> dict[str, Any]:
    with OPENAPI_PATH.open(encoding="utf-8") as source:
        value = yaml.safe_load(source)

    if not isinstance(value, dict):
        raise TypeError("OpenAPI document must contain an object")

    return value


def resolve_pointer(document: dict[str, Any], pointer: str) -> Any:
    value: Any = document

    for token in pointer.removeprefix("#/").split("/"):
        value = value[token.replace("~1", "/").replace("~0", "~")]

    return value


def dereference(value: Any, document: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        reference = value.get("$ref")

        if isinstance(reference, str) and reference.startswith("#/"):
            return dereference(resolve_pointer(document, reference), document)

        return {key: dereference(item, document) for key, item in value.items()}

    if isinstance(value, list):
        return [dereference(item, document) for item in value]

    return value


def validate_fixture(name: str, schema_name: str) -> dict[str, Any]:
    document = load_openapi()
    schema = document["components"]["schemas"][schema_name]
    resolved_schema = dereference(schema, document)
    fixture = load_json(name)

    validator = Draft202012Validator(
        resolved_schema,
        format_checker=FormatChecker(),
    )
    validator.validate(fixture)

    return fixture


def test_openapi_document_is_valid() -> None:
    document = cast(Mapping[Hashable, Any], load_openapi())

    validate(document)


@pytest.mark.parametrize("name", ["profile-found.json", "profile-missing.json"])
def test_single_profile_fixtures_match_contract(name: str) -> None:
    fixture = validate_fixture(name, "SingleProfileResponse")

    SingleProfileResponse.model_validate(fixture)


def test_batch_request_fixture_matches_contract() -> None:
    fixture = validate_fixture("batch-request.json", "BatchProfileRequest")

    BatchProfileRequest.model_validate(fixture)


def test_batch_response_fixture_matches_contract() -> None:
    fixture = validate_fixture("batch-mixed.json", "BatchProfileResponse")

    BatchProfileResponse.model_validate(fixture)


def test_peer_key_dto_round_trip() -> None:
    peer = FluxGramPeerKey(PeerType.CHANNEL, 1234567890)

    assert PeerKeyDto.from_domain(peer).to_domain() == peer


def test_peer_key_dto_rejects_id_outside_telegram_range() -> None:
    with pytest.raises(ValidationError):
        PeerKeyDto(peer_type=PeerType.USER, peer_id=str(MAX_TELEGRAM_ID + 1))


def test_batch_request_rejects_duplicate_peers() -> None:
    peer = {"peer_type": "user", "peer_id": "123"}

    with pytest.raises(ValidationError):
        BatchProfileRequest.model_validate({"peers": [peer, peer]})


def test_contract_rejects_expiry_without_timezone() -> None:
    fixture = load_json("profile-missing.json")
    fixture["result"]["expires_at"] = "2026-10-08T12:00:00"

    with pytest.raises(ValidationError):
        SingleProfileResponse.model_validate(fixture)


def test_batch_response_requires_a_result() -> None:
    with pytest.raises(ValidationError):
        BatchProfileResponse.model_validate(
            {
                "schema_version": 1,
                "minimum_renderer_version": 1,
                "results": [],
            }
        )
