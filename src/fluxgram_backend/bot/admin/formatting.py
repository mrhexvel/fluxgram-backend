import re
from datetime import UTC, datetime

from fluxgram_backend.application.plugin_reputation_publisher import ReputationPublication
from fluxgram_backend.domain.operations import AuditEntry, ProfileSummary
from fluxgram_backend.domain.peer import FluxGramPeerKey, PeerType

NEWLINE = "\n"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DEVELOPER_KEY_PATTERN = re.compile(r"^p256:[0-9a-f]{64}$")


def parse_user_peer(value: str | None) -> FluxGramPeerKey | None:
    if value is None:
        return None

    try:
        return FluxGramPeerKey(PeerType.USER, int(value.strip()))
    except ValueError:
        return None


def parse_archive_sha256(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value.strip().lower()
    return normalized if SHA256_PATTERN.fullmatch(normalized) is not None else None


def parse_developer_key_id(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value.strip().lower()
    return normalized if DEVELOPER_KEY_PATTERN.fullmatch(normalized) is not None else None


def format_reputation_decision(publication: ReputationPublication, days: int) -> str:
    archive_status = (
        "Нет известного отзыва"
        if int(publication.archive_status) == 1
        else "Вредоносный"
    )
    if publication.query.developer_key_id is None:
        developer_key = "не указан"
        key_status = "не применяется"
    else:
        developer_key = f"<code>{publication.query.developer_key_id}</code>"
        key_status = "Нет известного отзыва" if int(publication.key_status) == 1 else "Отозван"

    expires_at = datetime.fromtimestamp(publication.expires_at_millis / 1000, UTC)
    validity = "1 день" if days == 1 else f"{days} дней"
    lines = [
        "<b>Подтверждение решения</b>",
        "",
        "<b>Пакет</b>",
        "SHA-256:",
        f"<code>{publication.query.archive_sha256}</code>",
        f"Ключ разработчика: {developer_key}",
        "",
        "<b>Решение</b>",
        f"Архив: <b>{archive_status}</b>",
        f"Ключ: <b>{key_status}</b>",
        f"Актуальность записи: {validity}",
        f"До: {expires_at.strftime('%d.%m.%Y, %H:%M UTC')}",
        "",
        "<b>Важно</b>",
    ]
    if publication.archive_status.value == 2 or publication.key_status.value == 2:
        lines.append(
            "Блокировка не истекает: она сохранится до нового разрешающего решения."
        )
    else:
        lines.append(
            "Отсутствие известного отзыва не подтверждает безопасность пакета. "
            "После указанной даты запись перестанет быть актуальной."
        )

    return NEWLINE.join(lines)


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
