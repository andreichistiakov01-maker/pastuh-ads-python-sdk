"""Асинхронный клиент Pastuh Ads Partner API.

Базовый URL: ``{base}/api/partner/v1`` — передаётся целиком в ``base_url``.

Авторизация: ``Authorization: Bearer opk_<40 символов>`` — ключ привязан
к паре (partner, source). В запросах, где принимается ``source_id``,
он обязан совпадать с источником ключа — иначе 403 ``source_not_allowed``.

Retry: только безопасные случаи — таймауты, сетевые ошибки и 502/503/504
для GET и идемпотентных POST (op-list переиспользует открытую сессию,
shown/click/bot-start confirm идемпотентны на бэкенде). ``POST /payouts``
НЕ идемпотентен и никогда не ретраится.

API-ключ нигде не логируется и не попадает в исключения.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ._version import __version__

from .exceptions import (
    ApiConnectionError,
    ApiTimeoutError,
    AuthenticationError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PastuhAdsError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from .models import (
    BotStartConfirmResponse,
    CallbackClickResponse,
    ClickResponse,
    MeResponse,
    OpListResponse,
    OpTask,
    PartnerInfo,
    Payout,
    Placement,
    PayoutsSummary,
    PremiumClaimStatus,
    PremiumVerificationInfo,
    PremiumVerificationResponse,
    SessionStatusResponse,
    ShownResponse,
    SourceInfo,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUSES = frozenset({502, 503, 504})
_STATUS_TO_ERROR: dict[int, type[PastuhAdsError]] = {
    401: AuthenticationError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    429: RateLimitError,
}


class PastuhAdsClient:
    """Async-клиент для ``/api/partner/v1``.

    Args:
        base_url: Полный базовый URL API, например
            ``https://pastuhads.com/api/partner/v1``.
        api_key: Partner API key формата ``opk_...``.
        source_id: ID источника трафика (целое число), привязанный к ключу.
        placement_id: ID плейсмента источника по умолчанию
            (можно передать явно в :meth:`get_op_list`).
        timeout: Таймаут запроса в секундах.
        max_retries: Число повторных попыток для безопасных запросов.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        source_id: int | str | None = None,
        placement_id: int | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        if not base_url:
            raise ValueError("base_url обязателен")
        if not api_key:
            raise ValueError("api_key обязателен")

        self._source_id = int(source_id) if source_id is not None else None
        self._placement_id = placement_id
        self._max_retries = max(0, max_retries)

        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                # Версионированный UA позволяет платформе верифицировать
                # фактическую версию SDK у партнёра (access-логи).
                "User-Agent": f"pastuh-ads/{__version__}",
            },
        )

    async def __aenter__(self) -> "PastuhAdsClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Закрыть HTTP-клиент (освободить соединения)."""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # GET /me — информация о партнёре, источнике и выплатах
    # ------------------------------------------------------------------

    async def get_me(self) -> MeResponse:
        """GET /me — партнёр, источник, баланс, сводка выплат."""
        data = await self._request("GET", "/me")
        partner = data.get("partner") or {}
        source = data.get("source") or {}
        payouts = data.get("payouts") or {}
        return MeResponse(
            partner=PartnerInfo(
                id=int(partner.get("id") or 0),
                status=str(partner.get("status") or ""),
                balance=float(partner.get("balance") or 0),
                pending_balance=float(partner.get("pending_balance") or 0),
                available_balance=float(partner.get("available_balance") or 0),
                open_fraud_flags=int(partner.get("open_fraud_flags") or 0),
                raw=partner,
            ),
            source=SourceInfo(
                id=int(source.get("id") or 0),
                username=source.get("username"),
                status=str(source.get("status") or ""),
                quality_level=source.get("quality_level"),
                raw=source,
            ),
            payouts=PayoutsSummary(
                pending_count=int(payouts.get("pending_count") or 0),
                paid_total=float(payouts.get("paid_total") or 0),
                raw=payouts,
            ),
            raw=data,
        )

    # ------------------------------------------------------------------
    # OP-сессии: op-list / shown / check / status
    # ------------------------------------------------------------------

    async def get_op_list(
        self,
        telegram_user_id: int | str,
        *,
        placement_id: int | None = None,
        source_id: int | None = None,
        external_session_id: str | None = None,
        user_language: str | None = None,
        meta: dict[str, Any] | None = None,
        user_observation: dict[str, Any] | None = None,
        capabilities: list[str] | None = None,
    ) -> OpListResponse:
        """POST /op-list — открыть (или переиспользовать) OP-сессию.

        Сервер возвращает открытую сессию для того же пользователя,
        если она ещё не истекла — повторный вызов безопасен.

        Обязательные поля бэкенда: ``source_id``, ``placement_id``,
        ``telegram_user_id``. ``source_id`` обязан совпадать с источником
        API-ключа, иначе 403.

        ``user_observation`` — Premium SDK: заявление партнёра о статусе
        ``is_premium`` из его собственных Telegram-апдейтов (см.
        :func:`pastuh_ads.user_observation_from_user`). Принимается только
        для партнёров с флагом trusted_premium_observer; итог обработки
        возвращается в ``response.premium_claim.status``.

        ``capabilities`` — feature-gate новых CTA-режимов. Передайте
        ``["callback_cta"]``, если бот умеет: (1) рендерить для
        bot_tracked_click callback-кнопку ``task.cta_callback_data``;
        (2) по callback вызывать :func:`register_callback_click`;
        (3) показывать нативную url-кнопку на ``task.target_url``.
        Без capability задания приходят с ``open_mode="url"`` — прежний
        контракт: url-кнопка на ``task.url`` (server-hop /r/{token}).
        """
        resolved_source = source_id if source_id is not None else self._source_id
        resolved_placement = placement_id if placement_id is not None else self._placement_id
        if resolved_source is None:
            raise ValueError("source_id не задан ни в клиенте, ни в вызове")
        if resolved_placement is None:
            raise ValueError("placement_id не задан ни в клиенте, ни в вызове")

        body: dict[str, Any] = {
            "source_id": int(resolved_source),
            "placement_id": int(resolved_placement),
            "telegram_user_id": int(telegram_user_id),
        }
        if external_session_id:
            body["external_session_id"] = external_session_id
        if user_language:
            body["user_language"] = user_language
        if meta:
            body["meta"] = meta
        if user_observation is not None:
            body["user_observation"] = user_observation
        if capabilities is not None:
            body["capabilities"] = capabilities

        data = await self._request("POST", "/op-list", json=body, idempotent=True)

        premium_verification = data.get("premium_verification")
        premium_claim = data.get("premium_claim")
        negotiated = data.get("negotiated_capabilities")
        return OpListResponse(
            op_session_id=int(data["op_session_id"]),
            requested_slots=int(data.get("requested_slots") or 0),
            filled_slots=int(data.get("filled_slots") or 0),
            access_allowed=bool(data.get("access_allowed")),
            expires_at=data.get("expires_at"),
            tasks=[self._task(t) for t in data.get("tasks") or []],
            premium_verification=(
                PremiumVerificationInfo(
                    url=str(premium_verification.get("url") or ""),
                    expires_at=premium_verification.get("expires_at"),
                    raw=premium_verification,
                )
                if isinstance(premium_verification, dict)
                else None
            ),
            premium_claim=(
                PremiumClaimStatus(
                    status=str(premium_claim.get("status") or ""),
                    raw=premium_claim,
                )
                if isinstance(premium_claim, dict)
                else None
            ),
            negotiated_capabilities=[
                str(c) for c in negotiated if isinstance(c, str)
            ] if isinstance(negotiated, list) else [],
            raw=data,
        )

    async def verify_premium(
        self, telegram_user_id: int | str, init_data: str
    ) -> PremiumVerificationResponse:
        """POST /premium-verification — зафиксировать is_premium из initData.

        ``init_data`` — подписанный WebApp initData из Mini App сервисного
        бота платформы. ``user.id`` внутри обязан совпадать с
        ``telegram_user_id``. Идемпотентно: ничего не резервирует, только
        записывает наблюдение. Это fallback-канал на случай, когда
        ``user_observation`` недоступен или партнёр не доверенный.
        """
        data = await self._request(
            "POST",
            "/premium-verification",
            json={
                "telegram_user_id": int(telegram_user_id),
                "init_data": init_data,
            },
            idempotent=True,
        )
        return PremiumVerificationResponse(
            verified=bool(data.get("verified")),
            telegram_user_id=int(data.get("telegram_user_id") or 0),
            is_premium=data.get("is_premium"),
            observed_at=data.get("observed_at"),
            raw=data,
        )

    async def mark_shown(self, op_session_id: int) -> ShownResponse:
        """POST /op-sessions/{id}/shown — отметить показ OP-блока.

        Идемпотентно: повторный вызов просто вернёт текущий статус.
        """
        data = await self._request(
            "POST", f"/op-sessions/{int(op_session_id)}/shown", idempotent=True
        )
        return ShownResponse(
            op_session_id=int(data["op_session_id"]),
            session_status=str(data.get("session_status") or ""),
            raw=data,
        )

    async def check_session(self, op_session_id: int) -> SessionStatusResponse:
        """POST /op-sessions/{id}/check — проверить выполнение заданий.

        Запускает серверную верификацию (подписка/бот-старт) и возвращает
        актуальный статус. Истёкшая сессия → 409 ``session_expired``.
        """
        data = await self._request(
            "POST", f"/op-sessions/{int(op_session_id)}/check", idempotent=True
        )
        return self._session_status(data)

    async def get_session_status(self, op_session_id: int) -> SessionStatusResponse:
        """GET /op-sessions/{id}/status — текущий статус без повторной проверки."""
        data = await self._request("GET", f"/op-sessions/{int(op_session_id)}/status")
        return self._session_status(data)

    async def register_click(
        self, task_id: int, telegram_user_id: int | str
    ) -> ClickResponse:
        """POST /tasks/{id}/click — зафиксировать клик по заданию.

        Идемпотентно: повторный клик не создаёт второго события
        (фиксируется как duplicate на стороне фрод-мониторинга).
        """
        data = await self._request(
            "POST",
            f"/tasks/{int(task_id)}/click",
            json={"telegram_user_id": int(telegram_user_id)},
            idempotent=True,
        )
        return ClickResponse(
            task_id=int(data["task_id"]),
            task_status=str(data.get("task_status") or ""),
            verification_status=data.get("verification_status"),
            raw=data,
        )

    async def register_callback_click(
        self, task_id: int, telegram_user_id: int | str
    ) -> CallbackClickResponse:
        """POST /tasks/{id}/callback-click — фиксация нажатия callback-CTA.

        Только для bot_tracked_click с ``open_mode="callback"`` (op-list
        запрошен с ``capabilities=["callback_cta"]``). Вызовите этот метод
        в обработчике callback_query вашей кнопки «Открыть бота», затем
        ответьте на callback (answerCallbackQuery БЕЗ url — переход в
        чужой бот через answerCallbackQuery(url) Telegram отклоняет
        URL_INVALID) и отправьте сообщение с нативной inline url-кнопкой
        «Перейти в бота» на ``response.target_url``.

        Идемпотентно: повторный callback вернёт ``charged=False`` и не
        создаст второго начисления — второй тап по t.me-кнопке тоже не
        является оплачиваемым событием (оно — первый подтверждённый клик).
        """
        data = await self._request(
            "POST",
            f"/tasks/{int(task_id)}/callback-click",
            json={"telegram_user_id": int(telegram_user_id)},
            idempotent=True,
        )
        return CallbackClickResponse(
            task_id=int(data["task_id"]),
            task_status=str(data.get("task_status") or ""),
            verification_status=data.get("verification_status"),
            charged=bool(data.get("charged")),
            target_url=data.get("target_url"),
            raw=data,
        )

    # ------------------------------------------------------------------
    # POST /bot-start/confirm — подтверждение /start по deep-link payload
    # ------------------------------------------------------------------

    async def confirm_bot_start(
        self,
        telegram_user_id: int,
        payload: str,
        *,
        bot_username: str | None = None,
        telegram_bot_id: int | None = None,
        event: str | None = None,
    ) -> BotStartConfirmResponse:
        """POST /bot-start/confirm — подтвердить запуск бота.

        Вызывать ТОЛЬКО для ``/start`` с подписанным payload ``bs1.*``
        из deep-link. Обычный ``/start`` без payload НЕ подтверждается —
        сервер вернёт 422 ``ignored``.

        Для поиска integration нужен ``bot_username`` ИЛИ ``telegram_bot_id``.
        Идемпотентно: повтор → ``status="duplicate"``, ``is_unique=False``.
        """
        if bot_username is None and telegram_bot_id is None:
            raise ValueError("Нужен bot_username или telegram_bot_id")

        body: dict[str, Any] = {
            "telegram_user_id": int(telegram_user_id),
            "payload": payload,
        }
        if bot_username is not None:
            body["bot_username"] = bot_username.lstrip("@")
        if telegram_bot_id is not None:
            body["telegram_bot_id"] = int(telegram_bot_id)
        if event is not None:
            body["event"] = event

        data = await self._request("POST", "/bot-start/confirm", json=body, idempotent=True)
        return BotStartConfirmResponse(
            event_id=int(data.get("event_id") or 0),
            status=str(data.get("status") or ""),
            is_unique=bool(data.get("is_unique")),
            message=data.get("message"),
            raw=data,
        )

    async def list_placements(self) -> list[Placement]:
        """GET /placements — плейсменты источника, привязанного к ключу.

        Удобно, чтобы узнать ``placement_id`` для :meth:`get_op_list`
        без захода в кабинет. Отдаёт только плейсменты текущего
        источника — чужие не показывает.
        """
        data = await self._request("GET", "/placements")
        return [
            Placement(
                id=int(p.get("id") or 0),
                name=p.get("name"),
                type=p.get("type"),
                source_id=int(p.get("source_id") or 0),
                slots_count=int(p.get("slots_count") or 0),
                status=str(p.get("status") or ""),
                raw=p,
            )
            for p in data.get("placements") or []
        ]

    # ------------------------------------------------------------------
    # Payouts (опционально — если выплаты нужны в интеграции)
    # ------------------------------------------------------------------

    async def list_payouts(self) -> list[Payout]:
        """GET /payouts — список выплат партнёра."""
        data = await self._request("GET", "/payouts")
        return [self._payout(p) for p in data.get("payouts") or []]

    async def request_payout(
        self, amount: float, *, meta: dict[str, Any] | None = None
    ) -> Payout:
        """POST /payouts — создать запрос на выплату.

        ВНИМАНИЕ: НЕ идемпотентно — повторный вызов создаст вторую
        заявку. Клиент не ретраит этот запрос автоматически.
        """
        body: dict[str, Any] = {"amount": amount}
        if meta:
            body["meta"] = meta
        data = await self._request("POST", "/payouts", json=body)
        return self._payout(data["payout"])

    async def cancel_payout(self, payout_id: int) -> Payout:
        """POST /payouts/{id}/cancel — отменить pending-заявку."""
        data = await self._request("POST", f"/payouts/{int(payout_id)}/cancel")
        return self._payout(data["payout"])

    # ------------------------------------------------------------------
    # HTTP layer
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        attempts = 1 + (self._max_retries if (idempotent or method == "GET") else 0)
        last_exc: PastuhAdsError | None = None

        for attempt in range(attempts):
            try:
                response = await self._http.request(method, path, json=json)
            except httpx.TimeoutException as exc:
                last_exc = ApiTimeoutError(
                    f"Таймаут запроса {method} {path}: {type(exc).__name__}"
                )
            except httpx.TransportError as exc:
                last_exc = ApiConnectionError(
                    f"Сетевая ошибка {method} {path}: {type(exc).__name__}"
                )
            else:
                if response.status_code < 400:
                    return self._parse_ok(response)
                if response.status_code in _RETRYABLE_STATUSES:
                    last_exc = ServerError(
                        self._error_message(response),
                        status_code=response.status_code,
                        error_code=self._error_code(response),
                    )
                else:
                    raise self._map_error(response)

            if attempt < attempts - 1:
                await asyncio.sleep(0.5 * (2**attempt))
                logger.debug(
                    "retry %s %s после ошибки (попытка %d/%d)",
                    method, path, attempt + 1, attempts,
                )

        assert last_exc is not None
        raise last_exc

    def _parse_ok(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ServerError(
                "API вернул невалидный JSON",
                status_code=response.status_code,
            ) from exc
        if not isinstance(data, dict):
            raise ServerError(
                "API вернул неожиданный формат ответа",
                status_code=response.status_code,
            )
        return data

    def _map_error(self, response: httpx.Response) -> PastuhAdsError:
        error_code = self._error_code(response)
        message = self._error_message(response)
        details = self._error_details(response)

        exc_cls = _STATUS_TO_ERROR.get(response.status_code)
        if exc_cls is not None:
            return exc_cls(
                message,
                status_code=response.status_code,
                error_code=error_code,
                details=details,
            )
        if response.status_code >= 500:
            return ServerError(
                message,
                status_code=response.status_code,
                error_code=error_code,
                details=details,
            )
        return PastuhAdsError(
            message,
            status_code=response.status_code,
            error_code=error_code,
            details=details,
        )

    @staticmethod
    def _error_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _error_code(self, response: httpx.Response) -> str | None:
        code = self._error_payload(response).get("error")
        return str(code) if isinstance(code, str) and code else None

    def _error_message(self, response: httpx.Response) -> str:
        message = self._error_payload(response).get("message")
        if isinstance(message, str) and message:
            return message
        return f"HTTP {response.status_code}"

    def _error_details(self, response: httpx.Response) -> dict[str, Any] | None:
        details = self._error_payload(response).get("details")
        return details if isinstance(details, dict) else None

    # ------------------------------------------------------------------
    # Mappers
    # ------------------------------------------------------------------

    @staticmethod
    def _task(raw: dict[str, Any]) -> OpTask:
        return OpTask(
            task_id=int(raw.get("task_id") or 0),
            slot=int(raw.get("slot") or 0),
            advertised_resource_id=raw.get("advertised_resource_id"),
            target_type=raw.get("target_type"),
            action_type=raw.get("action_type"),
            title=raw.get("title"),
            username=raw.get("username"),
            url=raw.get("url"),
            instruction_text=raw.get("instruction_text"),
            action_button_text=raw.get("action_button_text"),
            verification_hint=raw.get("verification_hint"),
            tracking_token=raw.get("tracking_token"),
            status=raw.get("status"),
            verification_status=raw.get("verification_status"),
            open_mode=raw.get("open_mode"),
            target_url=raw.get("target_url"),
            cta_callback_data=raw.get("cta_callback_data"),
            raw=raw,
        )

    def _session_status(self, data: dict[str, Any]) -> SessionStatusResponse:
        return SessionStatusResponse(
            op_session_id=int(data["op_session_id"]),
            session_status=str(data.get("session_status") or ""),
            access_allowed=bool(data.get("access_allowed")),
            requested_slots=int(data.get("requested_slots") or 0),
            filled_slots=int(data.get("filled_slots") or 0),
            expires_at=data.get("expires_at"),
            tasks=[self._task(t) for t in data.get("tasks") or []],
            incomplete_tasks=[self._task(t) for t in data.get("incomplete_tasks") or []],
            raw=data,
        )

    @staticmethod
    def _payout(raw: dict[str, Any]) -> Payout:
        return Payout(
            id=int(raw.get("id") or 0),
            amount=float(raw.get("amount") or 0),
            status=str(raw.get("status") or ""),
            requested_at=raw.get("requested_at"),
            processed_at=raw.get("processed_at"),
            meta=raw.get("meta") or {},
            raw=raw,
        )
