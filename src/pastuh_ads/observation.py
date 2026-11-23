"""Автоматический сбор user_observation для Premium SDK.

Партнёрский бот уже получает от Telegram полные ``User``-объекты
в каждом апдейте (``message.from_user`` в aiogram, ``update.message.from_user``
в pyrogram и т.д.). SDK упаковывает ``is_premium`` в ``user_observation`` —
и платформа получает Premium-статус БЕЗ дополнительных действий от
пользователя.

СЕМАНТИКА ПОЛНОГО USER (Telegram Bot API):

Поле ``User.is_premium`` в Bot API опционально и фактически True-only —
Telegram присылает его только для Premium-аккаунтов. Поэтому для ПОЛНОГО
User-объекта из реального апдейта:

- ``is_premium=True``  → ``{"is_premium": True}``
- ``is_premium=False`` → ``{"is_premium": False}``
- ``is_premium=None``  → ``{"is_premium": False}`` (поле не пришло = не Premium)

«Полный» определяется по обязательным полям Bot API User: ``id``,
``is_bot``, ``first_name``. aiogram 3 ``types.User`` их всегда содержит.

Для ЧАСТИЧНОГО или неоднозначного объекта (нет обязательных полей User,
либо объект вообще не имеет слота ``is_premium``) возвращается ``None`` —
наблюдение не отправляется. Мы никогда не фабрикуем статус и не трактуем
произвольный неполный dict как полный Telegram User.

ВАЖНО — модель доверия: ``user_observation`` — это заявление партнёра, а не
криптографическое доказательство. Платформа принимает claim только от
партнёров с флагом ``trusted_premium_observer`` (ставит админ). Без флага
поле игнорируется (``premium_claim.status == "ignored_untrusted"``) и
остаётся fallback-верификация через ``premium_verification.url``.
"""

from __future__ import annotations

from typing import Any

# Обязательные поля Bot API User: без них объект считаем частичным.
_REQUIRED_USER_KEYS = ("id", "is_bot", "first_name")


def user_observation_from_user(
    user: Any,
    *,
    include_gender: bool = False,
) -> dict[str, Any] | None:
    """Собрать ``user_observation`` из Telegram User-объекта.

    Args:
        user: ПОЛНЫЙ объект пользователя Telegram — aiogram ``types.User``,
            pyrogram ``User``, сырой dict из Bot API со всеми обязательными
            полями (``id``, ``is_bot``, ``first_name``) или любой объект
            с этими атрибутами и слотом ``is_premium``.
        include_gender: SDK 1.2.0 — ОПЦИЯ, выключена по умолчанию.
            Только при явном ``True`` добавляется локальный gender-claim
            (``male``/``female``/``unknown``), вычисленный в процессе
            бота из first_name/last_name/username. При ``False``
            классификатор не импортируется и не вызывается вообще —
            поведение побайтово как у SDK 1.1.0 (только ``is_premium``).
            Сырые поля НИКУДА не отправляются — только вердикт. Сервер
            применяет его лишь для партнёров с флагом
            ``trusted_gender_observer`` при включённой
            ``gender_segmentation_enabled`` — недоверенный партнёр
            ничего не меняет этим полем.

    Returns:
        ``{"is_premium": bool[, "gender": str]}`` для полного User
        (отсутствующее ``is_premium`` трактуется как non-Premium — см.
        семантику выше), либо ``None`` для частичного/неоднозначного
        объекта.

    Пример (aiogram 3)::

        op_list = await ads.get_op_list(
            telegram_user_id=message.from_user.id,
            user_observation=user_observation_from_user(message.from_user),
        )
    """
    if user is None:
        return None

    if isinstance(user, dict):
        if not all(key in user for key in _REQUIRED_USER_KEYS):
            return None  # частичный dict — не полный Telegram User
        observation: dict[str, Any] = {"is_premium": bool(user.get("is_premium"))}
    else:
        # Объектный доступ (aiogram types.User и совместимые адаптеры):
        # полный User обязан иметь id/is_bot/first_name И слот is_premium
        # (у aiogram он существует и равен None, когда поле не прислал).
        if not all(hasattr(user, key) for key in _REQUIRED_USER_KEYS):
            return None
        if not hasattr(user, "is_premium"):
            return None
        observation = {"is_premium": bool(user.is_premium)}

    if include_gender:
        # Lazy import: без явного opt-in модуль классификации даже не
        # загружается — gender OFF означает «классификатор не вызывается»,
        # а не «вызвали и выкинули результат». gender_from_user никогда
        # не бросает — 'unknown' при любой ошибке. Имена/username
        # остаются внутри процесса бота.
        from .gender import gender_from_user

        observation["gender"] = gender_from_user(user)

    return observation
