from pathlib import Path

import pytest

from fluxgram_backend.publisher import __main__ as publisher_main


def test_publisher_entrypoint_reports_field_names_without_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "DATABASE_URL",
        "DATABASE_URL_FILE",
        "PLUGIN_REPUTATION_AUTHORITY_KEY_ID",
        "PLUGIN_REPUTATION_WORKER_ID",
        "VAULT_ADDRESS",
        "VAULT_TRANSIT_MOUNT",
        "VAULT_TRANSIT_KEY_NAME",
        "VAULT_TRANSIT_KEY_VERSION",
        "VAULT_AGENT_TOKEN_FILE",
        "VAULT_CA_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    assert publisher_main.main() == 2
    error = capsys.readouterr().err
    assert "DATABASE_URL" in error
    assert "PLUGIN_REPUTATION_AUTHORITY_KEY_ID" not in error
    assert "input_value" not in error
