from fluxgram_backend.domain.operations import AuditEntry, ProfileSummary
from fluxgram_backend.domain.peer import FluxGramPeerKey, PeerType

NEWLINE = "\n"


def parse_user_peer(value: str | None) -> FluxGramPeerKey | None:
    if value is None:
        return None

    try:
        return FluxGramPeerKey(PeerType.USER, int(value.strip()))
    except ValueError:
        return None


def format_profile(peer: FluxGramPeerKey, profile: ProfileSummary | None) -> str:
    if profile is None:
        return f"Профиль пользователя {peer.peer_id} не найден."

    badges = ", ".join(badge.badge_id for badge in profile.badges) or "нет"
    design = "нет"
    if profile.design is not None:
        values = (
            profile.design.design_id,
            profile.design.palette_id,
            profile.design.pattern_id,
            profile.design.avatar_frame_id,
            profile.design.badge_style_id,
        )
        design = ", ".join(value for value in values if value is not None)

    return NEWLINE.join(
        (
            f"Пользователь: {profile.peer.peer_id}",
            f"Revision: {profile.revision}",
            f"Бейджи: {badges}",
            f"Дизайн: {design}",
        )
    )


def format_history(peer: FluxGramPeerKey, history: tuple[AuditEntry, ...]) -> str:
    if not history:
        return f"История пользователя {peer.peer_id} пуста."

    lines = [f"История пользователя {peer.peer_id}:"]
    lines.extend(
        f"{entry.operation_id}: {entry.operation_type} ({entry.occurred_at.isoformat()})"
        for entry in history
    )
    return NEWLINE.join(lines)
