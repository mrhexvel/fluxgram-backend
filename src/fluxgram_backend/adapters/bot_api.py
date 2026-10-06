from enum import StrEnum

from fluxgram_backend.domain.peer import FluxGramPeerKey, PeerMigration, PeerType

BOT_API_CHANNEL_ID_OFFSET = 1_000_000_000_000


class BotApiChatType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"


class UnsupportedBotApiChatError(ValueError):
    pass


def peer_key_from_bot_api_chat(
    chat_type: BotApiChatType,
    chat_id: int,
    *,
    is_direct_messages: bool = False,
) -> FluxGramPeerKey:
    if not isinstance(chat_type, BotApiChatType):
        raise TypeError("chat_type must be a BotApiChatType")

    if type(chat_id) is not int:
        raise TypeError("chat_id must be an integer")

    if is_direct_messages:
        raise UnsupportedBotApiChatError("channel direct messages are not supported")

    if chat_type is BotApiChatType.PRIVATE:
        if chat_id <= 0:
            raise ValueError("private chat_id must be positive")

        return FluxGramPeerKey(PeerType.USER, chat_id)

    if chat_type is BotApiChatType.GROUP:
        if chat_id >= 0 or chat_id <= -BOT_API_CHANNEL_ID_OFFSET:
            raise ValueError("group chat_id must use the negative basic-group representation")

        return FluxGramPeerKey(PeerType.GROUP, -chat_id)

    if chat_id >= -BOT_API_CHANNEL_ID_OFFSET:
        raise ValueError("supergroup and channel chat_id must use the -100 representation")

    peer_id = -chat_id - BOT_API_CHANNEL_ID_OFFSET

    if chat_type is BotApiChatType.SUPERGROUP:
        return FluxGramPeerKey(PeerType.GROUP, peer_id)

    if chat_type is BotApiChatType.CHANNEL:
        return FluxGramPeerKey(PeerType.CHANNEL, peer_id)

    raise UnsupportedBotApiChatError(f"unsupported Bot API chat type: {chat_type}")


def group_migration_from_bot_api(
    source_chat_id: int,
    target_chat_id: int,
) -> PeerMigration:
    source = peer_key_from_bot_api_chat(BotApiChatType.GROUP, source_chat_id)
    target = peer_key_from_bot_api_chat(BotApiChatType.SUPERGROUP, target_chat_id)

    return PeerMigration(source=source, target=target)
