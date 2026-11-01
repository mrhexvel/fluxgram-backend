import asyncio
import os
from pathlib import Path

from fluxgram_backend.adapters.secure_file import SecureFileError, read_bounded_regular_file
from fluxgram_backend.adapters.vault_transit import MAX_VAULT_TOKEN_LENGTH

TOKEN_FILE_MODES = frozenset((0o400, 0o600))


class VaultAgentTokenFileError(RuntimeError):
    pass


class VaultAgentTokenFileProvider:
    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Vault Agent token file path must be absolute")

        self._path = path

    async def __call__(self) -> str:
        return await asyncio.to_thread(self._read)

    def _read(self) -> str:
        try:
            encoded = read_bounded_regular_file(
                self._path,
                MAX_VAULT_TOKEN_LENGTH + 2,
                TOKEN_FILE_MODES,
                frozenset((os.geteuid(),)),
            )
        except SecureFileError as error:
            raise VaultAgentTokenFileError("Vault Agent token file is invalid") from error

        if encoded.endswith(b"\r\n"):
            encoded = encoded[:-2]
        elif encoded.endswith(b"\n"):
            encoded = encoded[:-1]

        if not 1 <= len(encoded) <= MAX_VAULT_TOKEN_LENGTH or any(
            value < 0x21 or value > 0x7E for value in encoded
        ):
            raise VaultAgentTokenFileError("Vault Agent token file contains an invalid token")

        return encoded.decode("ascii")
