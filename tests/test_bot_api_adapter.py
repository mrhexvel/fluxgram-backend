import pytest

from fluxgram_backend.adapters.bot_api import (
    BotApiChatType,
    UnsupportedBotApiChatError,
    group_migration_from_bot_api,
    peer_key_from_bot_api_chat,
)
from fluxgram_backend.domain.peer import FluxGramPeerKey, PeerType


@pytest.mark.parametrize(
    ("chat_type", "chat_id", "expected"),
    [
        (BotApiChatType.PRIVATE, 123456789, FluxGramPeerKey(PeerType.USER, 123456789)),
        (BotApiChatType.GROUP, -123456789, FluxGramPeerKey(PeerType.GROUP, 123456789)),
        (
            BotApiChatType.SUPERGROUP,
            -1001234567890,
            FluxGramPeerKey(PeerType.GROUP, 1234567890),
        ),
        (
            BotApiChatType.CHANNEL,
            -1001234567890,
            FluxGramPeerKey(PeerType.CHANNEL, 1234567890),
        ),
    ],
)
def test_bot_api_chat_mapping(
    chat_type: BotApiChatType,
    chat_id: int,
    expected: FluxGramPeerKey,
) -> None:
    assert peer_key_from_bot_api_chat(chat_type, chat_id) == expected


@pytest.mark.parametrize(
    ("chat_type", "chat_id"),
    [
        (BotApiChatType.PRIVATE, -123),
        (BotApiChatType.GROUP, 123),
        (BotApiChatType.GROUP, -1001234567890),
        (BotApiChatType.SUPERGROUP, -123),
        (BotApiChatType.CHANNEL, -123),
    ],
)
def test_bot_api_chat_mapping_rejects_mismatched_representation(
    chat_type: BotApiChatType,
    chat_id: int,
) -> None:
    with pytest.raises(ValueError):
        peer_key_from_bot_api_chat(chat_type, chat_id)


def test_channel_direct_messages_are_not_treated_as_users() -> None:
    with pytest.raises(UnsupportedBotApiChatError):
        peer_key_from_bot_api_chat(
            BotApiChatType.PRIVATE,
            123456789,
            is_direct_messages=True,
        )


def test_group_migration_maps_basic_group_to_supergroup() -> None:
    migration = group_migration_from_bot_api(
        source_chat_id=-123456789,
        target_chat_id=-1001234567890,
    )

    assert migration.source == FluxGramPeerKey(PeerType.GROUP, 123456789)
    assert migration.target == FluxGramPeerKey(PeerType.GROUP, 1234567890)
