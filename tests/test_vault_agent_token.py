import asyncio
import os
from pathlib import Path

import pytest

from fluxgram_backend.adapters.vault_agent_token import (
    VaultAgentTokenFileError,
    VaultAgentTokenFileProvider,
)


def _write_token(path: Path, value: bytes, mode: int = 0o600) -> None:
    path.write_bytes(value)
    path.chmod(mode)


@pytest.mark.parametrize("terminator", [b"", b"\n", b"\r\n"])
@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_token_provider_reads_bounded_agent_file(
    tmp_path: Path,
    terminator: bytes,
    mode: int,
) -> None:
    path = tmp_path / "token"
    _write_token(path, b"hvs.production-token" + terminator, mode)

    token = asyncio.run(VaultAgentTokenFileProvider(path)())

    assert token == "hvs.production-token"


def test_token_provider_observes_atomic_agent_rotation(tmp_path: Path) -> None:
    path = tmp_path / "token"
    replacement = tmp_path / "replacement"
    provider = VaultAgentTokenFileProvider(path)
    _write_token(path, b"hvs.first")

    assert asyncio.run(provider()) == "hvs.first"

    _write_token(replacement, b"hvs.second")
    os.replace(replacement, path)

    assert asyncio.run(provider()) == "hvs.second"


@pytest.mark.parametrize("mode", [0o440, 0o601, 0o610, 0o620, 0o640, 0o700])
def test_token_provider_rejects_unsafe_permissions(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "token"
    _write_token(path, b"hvs.secret", mode)

    with pytest.raises(VaultAgentTokenFileError, match="invalid"):
        asyncio.run(VaultAgentTokenFileProvider(path)())


def test_token_provider_rejects_symlink_and_hard_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    symlink = tmp_path / "symlink"
    hard_link = tmp_path / "hard-link"
    _write_token(target, b"hvs.secret")
    symlink.symlink_to(target)
    os.link(target, hard_link)

    with pytest.raises(VaultAgentTokenFileError):
        asyncio.run(VaultAgentTokenFileProvider(symlink)())
    with pytest.raises(VaultAgentTokenFileError):
        asyncio.run(VaultAgentTokenFileProvider(hard_link)())


def test_token_provider_rejects_non_regular_file_without_blocking(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    fifo = tmp_path / "fifo"
    directory.mkdir()
    os.mkfifo(fifo, 0o600)

    with pytest.raises(VaultAgentTokenFileError):
        asyncio.run(VaultAgentTokenFileProvider(directory)())
    with pytest.raises(VaultAgentTokenFileError):
        asyncio.run(VaultAgentTokenFileProvider(fifo)())


@pytest.mark.parametrize(
    "value",
    [b"", b"contains space", b"line-one\nline-two", b"non-ascii-\xff", b"nul-\x00"],
)
def test_token_provider_rejects_invalid_token(tmp_path: Path, value: bytes) -> None:
    path = tmp_path / "token"
    _write_token(path, value)

    with pytest.raises(VaultAgentTokenFileError, match="invalid token"):
        asyncio.run(VaultAgentTokenFileProvider(path)())


def test_token_provider_rejects_oversized_token_without_disclosing_it(tmp_path: Path) -> None:
    path = tmp_path / "token"
    secret = b"s" * 8193
    _write_token(path, secret)

    with pytest.raises(VaultAgentTokenFileError) as captured:
        asyncio.run(VaultAgentTokenFileProvider(path)())

    assert secret.decode() not in str(captured.value)


def test_token_provider_requires_absolute_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        VaultAgentTokenFileProvider(Path("token"))
