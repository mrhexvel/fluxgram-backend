from collections.abc import Coroutine
from typing import Any

import pytest

from fluxgram_backend import cli
from fluxgram_backend.settings import Settings


def test_webhook_set_returns_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failed_webhook(_: Settings) -> Coroutine[Any, Any, bool]:
        async def result() -> bool:
            return False

        return result()

    monkeypatch.setattr(cli, "set_webhook", failed_webhook)

    assert cli.main(["webhook", "set"]) == 1
    assert capsys.readouterr().out == '{"ok":false}\n'
