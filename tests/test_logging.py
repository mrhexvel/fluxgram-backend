import logging

import pytest
from loguru import logger

from fluxgram_backend.log_config import configure_logging


def test_standard_library_logs_are_forwarded_through_loguru(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")

    logging.getLogger("uvicorn.error").info("Application startup complete.")

    output = capsys.readouterr().err
    assert " | INFO     | " in output
    assert "test_logging:test_standard_library_logs_are_forwarded_through_loguru" in output
    assert output.rstrip().endswith(" - Application startup complete.")
    assert not output.startswith("{")


def test_direct_loguru_logs_use_calling_module(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")

    logger.info("service ready")

    output = capsys.readouterr().err
    assert " | INFO     | " in output
    assert "test_logging:test_direct_loguru_logs_use_calling_module" in output
    assert output.rstrip().endswith(" - service ready")
    assert not output.startswith("{")
