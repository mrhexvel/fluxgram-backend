import pytest

from fluxgram_backend.domain.peer import (
    MAX_TELEGRAM_ID,
    FluxGramPeerKey,
    PeerMigration,
    PeerType,
)


def test_peer_key_preserves_type_namespace() -> None:
    user = FluxGramPeerKey(PeerType.USER, 123)
    group = FluxGramPeerKey(PeerType.GROUP, 123)
    channel = FluxGramPeerKey(PeerType.CHANNEL, 123)

    assert len({user, group, channel}) == 3


@pytest.mark.parametrize("peer_id", [0, -1, MAX_TELEGRAM_ID + 1])
def test_peer_key_rejects_out_of_range_ids(peer_id: int) -> None:
    with pytest.raises(ValueError):
        FluxGramPeerKey(PeerType.USER, peer_id)


def test_peer_key_rejects_boolean_id() -> None:
    with pytest.raises(TypeError):
        FluxGramPeerKey(PeerType.USER, True)


def test_migration_requires_two_different_groups() -> None:
    group = FluxGramPeerKey(PeerType.GROUP, 100)
    user = FluxGramPeerKey(PeerType.USER, 200)

    with pytest.raises(ValueError):
        PeerMigration(source=user, target=group)

    with pytest.raises(ValueError):
        PeerMigration(source=group, target=group)
