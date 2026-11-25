"""Модели ответов Pastuh Ads Partner API.

Поля соответствуют фактическим ответам ``/api/partner/v1/*``
(Laravel: app/Http/Controllers/Api/Partner/*). Лёгкие dataclasses —
без pydantic, чтобы у клиента был минимум зависимостей.

Неизвестные поля из ответа сохраняются в ``raw`` — если API
вернёт новое поле, оно не потеряется.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PartnerInfo:
    """Блок ``partner`` из GET /me."""

    id: int
    status: str
    balance: float
    pending_balance: float
    available_balance: float
    open_fraud_flags: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class SourceInfo:
    """Блок ``source`` из GET /me."""

    id: int
    username: str | None
    status: str
    quality_level: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class PayoutsSummary:
    """Блок ``payouts`` из GET /me."""

    pending_count: int
    paid_total: float
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class MeResponse:
    """Ответ GET /api/partner/v1/me."""

    partner: PartnerInfo
    source: SourceInfo
    payouts: PayoutsSummary
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class OpTask:
    """Элемент ``tasks[]`` / ``channels[]`` / ``incomplete_tasks[]``.

    ``tracking_token`` присутствует только для action_type ==
    ``bot_start_confirmed`` — это подписанный payload ``bs1.*``
    для deep-link ``/start``.
    """

    task_id: int
    slot: int
    advertised_resource_id: int | None
    target_type: str | None
    action_type: str | None
    title: str | None
    username: str | None
    url: str | None
    instruction_text: str | None
    action_button_text: str | None
    verification_hint: str | None
    tracking_token: str | None = None
    status: str | None = None
    verification_status: str | None = None
    # Callback-CTA (bot_tracked_click, SDK ≥1.1):
    #   open_mode="url"      — legacy: показывайте url-кнопку на task.url
    #                          (server-hop /r/{token}, старый контракт);
    #   open_mode="callback" — рендерьте callback-кнопку cta_callback_data;
    #                          на callback → POST /tasks/{id}/callback-click
    #                          и покажите нативную кнопку на target_url.
    open_mode: str | None = None
    target_url: str | None = None
    cta_callback_data: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class PremiumVerificationInfo:
    """Блок ``premium_verification`` из op-list (Premium V2 fallback).

    Присутствует, когда в пуле есть Premium-only кампании, а статус
    пользователя недостоверен. ``url`` — подписанная deep-link на
    сервисный бот платформы: после /start повторный op-list выдаёт
    Premium-задания. ``None`` — подтверждение не требуется.
    """

    url: str
    expires_at: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class PremiumClaimStatus:
    """Блок ``premium_claim`` из op-list — итог обработки user_observation.

    ``status``:
      - ``applied`` — claim принят (партнёр в списке trusted_premium_observer);
      - ``ignored_untrusted`` — партнёр не доверенный, claim отброшен;
      - ``ignored_trusted_observation`` — у платформы уже есть свежее
        наблюдение от самого Telegram, claim не нужен;
      - ``not_sent`` — user_observation не передавался.
    """

    status: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class OpListResponse:
    """Ответ POST /api/partner/v1/op-list."""

    op_session_id: int
    requested_slots: int
    filled_slots: int
    access_allowed: bool
    expires_at: str | None
    tasks: list[OpTask]
    premium_verification: PremiumVerificationInfo | None = None
    premium_claim: PremiumClaimStatus | None = None
    # SDK 1.2.0: capabilities, которые сервер реально принял для этой
    # сессии ('callback_cta' всегда по декларации; 'interactive_op' —
    # только при включённом external_op_block_interactive_enabled).
    negotiated_capabilities: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class PremiumVerificationResponse:
    """Ответ POST /api/partner/v1/premium-verification.

    Фиксирует достоверное наблюдение is_premium из подписанного
    WebApp initData — альтернатива deep-link, когда партнёр уже
    запускает Mini App нашего сервисного бота.
    """

    verified: bool
    telegram_user_id: int
    is_premium: bool | None
    observed_at: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ShownResponse:
    """Ответ POST /op-sessions/{id}/shown."""

    op_session_id: int
    session_status: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class SessionStatusResponse:
    """Ответ POST /op-sessions/{id}/check и GET /op-sessions/{id}/status.

    ``access_allowed=True`` — все обязательные задания выполнены,
    контент можно открывать. ``incomplete_tasks`` — что ещё не выполнено.
    """

    op_session_id: int
    session_status: str
    access_allowed: bool
    requested_slots: int
    filled_slots: int
    expires_at: str | None
    tasks: list[OpTask]
    incomplete_tasks: list[OpTask]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class SessionSyncItem:
    """Компактный снимок одной сессии из POST /op-sessions/sync (1.3.2)."""

    op_session_id: int
    session_status: str
    access_allowed: bool
    expires_at: str | None
    completed_task_ids: list[int]
    incomplete_task_ids: list[int]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class SessionSyncResponse:
    """Ответ POST /op-sessions/sync: снимки + id, которые сервер не знает
    (чужие/удалённые) — их синхронизацию SDK прекращает."""

    sessions: list[SessionSyncItem]
    unknown_session_ids: list[int]
    server_time: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ClickResponse:
    """Ответ POST /tasks/{id}/click."""

    task_id: int
    task_status: str
    verification_status: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class CallbackClickResponse:
    """Ответ POST /tasks/{id}/callback-click (bot_tracked_click, SDK ≥1.1).

    ``charged=True`` — этот вызов создал оплачиваемую конверсию
    (первый подтверждённый клик при глобальной уникальности user × bot).
    ``charged=False`` — повторный callback или уже конвертированный
    пользователь: начисление не дублируется. ``target_url`` — нативный
    https://t.me/<bot> для кнопки «Перейти в бота».
    """

    task_id: int
    task_status: str
    verification_status: str | None
    charged: bool
    target_url: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class BotStartConfirmResponse:
    """Ответ POST /bot-start/confirm (200 — confirmed или duplicate).

    ``is_unique=True`` — старт засчитан впервые (начисление произошло).
    ``is_unique=False`` + ``status="duplicate"`` — повторный старт,
    начисление не дублируется.
    """

    event_id: int
    status: str
    is_unique: bool
    message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Placement:
    """Плейсмент источника из GET /placements.

    ``id`` — это и есть ``placement_id``, который нужен для op-list.
    ``status``: active / inactive / archived.
    """

    id: int
    name: str | None
    type: str | None
    source_id: int
    slots_count: int
    status: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Payout:
    """Элемент ``payouts[]`` / ответ создания и отмены выплаты."""

    id: int
    amount: float
    status: str
    requested_at: str | None
    processed_at: str | None
    meta: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
