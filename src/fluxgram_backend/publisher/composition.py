import os
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from fluxgram_backend.adapters.secure_file import SecureFileError, read_bounded_regular_file
from fluxgram_backend.adapters.vault_agent_token import VaultAgentTokenFileProvider
from fluxgram_backend.adapters.vault_transit import VaultTransitAuthoritySigner
from fluxgram_backend.application.plugin_reputation_decisions import PluginReputationDecisionQueue
from fluxgram_backend.application.plugin_reputation_publisher import PluginReputationPublisher
from fluxgram_backend.database.connection import Database
from fluxgram_backend.publisher.settings import PluginReputationPublisherSettings

MAX_VAULT_CA_BYTES = 1024 * 1024
CA_FILE_MODES = frozenset((0o400, 0o440, 0o444, 0o600, 0o640, 0o644))


@dataclass(frozen=True, slots=True)
class PluginReputationPublisherRuntime:
    queue: PluginReputationDecisionQueue
    publisher: PluginReputationPublisher


@asynccontextmanager
async def open_plugin_reputation_publisher(
    database_url: str,
    settings: PluginReputationPublisherSettings,
) -> AsyncIterator[PluginReputationPublisher]:
    async with open_plugin_reputation_publisher_runtime(database_url, settings) as runtime:
        yield runtime.publisher


@asynccontextmanager
async def open_plugin_reputation_publisher_runtime(
    database_url: str,
    settings: PluginReputationPublisherSettings,
) -> AsyncIterator[PluginReputationPublisherRuntime]:
    try:
        ca_data = read_bounded_regular_file(
            settings.vault_ca_file,
            MAX_VAULT_CA_BYTES,
            CA_FILE_MODES,
            frozenset((0, os.geteuid())),
        ).decode("ascii")
    except (SecureFileError, UnicodeDecodeError) as error:
        raise RuntimeError("Vault CA file is invalid") from error

    tls = ssl.create_default_context(cadata=ca_data)
    if tls.verify_mode != ssl.CERT_REQUIRED or not tls.check_hostname:
        raise RuntimeError("Vault TLS context does not verify certificates and hostnames")

    timeout = httpx.Timeout(settings.vault_signer_timeout_seconds)
    limits = httpx.Limits(max_connections=2, max_keepalive_connections=1)
    database = Database(database_url)
    try:
        async with httpx.AsyncClient(
            verify=tls,
            follow_redirects=False,
            trust_env=False,
            http2=False,
            timeout=timeout,
            limits=limits,
        ) as client:
            signer = VaultTransitAuthoritySigner(
                client,
                settings.vault_address,
                settings.vault_transit_mount,
                settings.vault_transit_key_name,
                settings.vault_transit_key_version,
                VaultAgentTokenFileProvider(settings.vault_agent_token_file),
                settings.vault_namespace,
            )
            yield PluginReputationPublisherRuntime(
                PluginReputationDecisionQueue(database.sessions),
                PluginReputationPublisher(
                    database.sessions,
                    signer,
                    settings.plugin_reputation_authority_key_id,
                    settings.vault_signer_timeout_seconds,
                ),
            )
    finally:
        await database.close()
