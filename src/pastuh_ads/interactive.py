"""Интерактивный OP-блок для внешних ботов (SDK 1.2.0).

Партнёрский бот сам отправляет и редактирует свои сообщения — платформа
предоставляет контракт (задания, frozen URL, статусы, callback-click),
SDK реализует UX одного OP-сообщения: клавиатура сокращается по мере
выполнения, оплаченные click-задания остаются доступны через reopen-CTA,
tokenless destination живёт в TTL-слоте и переоткрывается без повторной
оплаты.

Архитектура:

- ``InteractiveOpBlock`` — транспортно-нейтральный движок состояния.
  Telegram-операции инжектируются callables (send/edit/delete), поэтому
  модуль работает с aiogram, pyrogram, голым Bot API HTTP — без жёсткой
  зависимости. API-вызовы идут через ``PastuhAdsClient``.
- Render fencing: у каждой сессии монотонная ``rev`` — рендер сначала
  бампит ревизию под коротким asyncio.Lock, строит клавиатуру, затем
  ВНЕ лока проверяет актуальность ревизии вплотную к вызову Telegram.
  Устаревший рендер пропускает отправку — stale-клавиатура не воскрешает
  выполненные задания (аналог revision-fencing серверной версии 3A.2).
- Никаких сетевых вызовов под локом: лок держит только мутацию
  состояния (наносекунды), HTTP/Telegram — строго снаружи.
- Convergence: повторные callback'и идемпотентны — серверная
  идемпотентность (charged=false) + локальный state merge; выполненное
  задание не возвращается на клавиатуру никогда.
- Биллинг: SDK НЕ решает, оплачено действие или нет — вердикт только
  из ответа API (charged, task_status). Reopen не содержит ни одного
  финансового вызова по построению.
- Reconcile: при временных ошибках Telegram — ограниченные повторы
  (экспоненциальный backoff, максимум ``max_render_retries``) + ленивое
  восстановление при следующем событии. Бесконечных retry нет;
  фоновая синхронизация — батч на бота с адаптивным расписанием
  и остановкой (см. ниже), а не polling каждой сессии.
- Restart: ``dump_state()``/``restore_state()`` сериализуют сессии
  (dict — партнёр может держать их в Redis/памяти), ``resume()``
  перечитывает status и перестраивает клавиатуру — process restart
  безопасен.

Callback-data формат (принадлежит боту партнёра, НЕ платформе):
    ``paop:open:{task_id}``    — оплачиваемый клик (bot_tracked_click /
                               bot_click): серверный transition через API.
    ``paop:reopen:{task_id}``  — повторное открытие оплаченного
                               destination: слот без финансового вызова.
    ``paop:check``             — ручной «Проверить» из СТАРЫХ клавиатур
                               (SDK < следующего релиза): роутинг
                               сохранён для совместимости, в новой
                               клавиатуре кнопки нет — проверка
                               event-driven.

Event-driven (UX-контракт v2): кнопки «Проверить» нет. Подтверждение
заданий выполняет сервер (chat_member / chat_join_request /
BotStartEvent / presence-проба), а блок узнаёт об этом двумя путями:

- piggyback на наблюдаемых событиях пользователя: повторный ``open()``
  и любой ``paop:*`` callback вызывают ``POST /check`` не чаще, чем раз
  в ``check_min_interval_seconds`` (default 20) — подписка, случившаяся
  между событиями, подтягивается без ручной кнопки;
- фоновая адаптивная синхронизация (1.3.2, ``auto_sync=True`` по
  умолчанию): ОДИН цикл на блок (бота) батчем ``POST /op-sessions/sync``
  (≤50 сессий за запрос, не чаще ``sync_min_interval_seconds``) по
  расписанию от последнего действия пользователя — 2с первую минуту,
  5с до 3 минут, 15с до 10 минут, затем остановка до следующего
  события. Останавливается на completion/expiry, backoff на 429/сбоях,
  на старом сервере (404) — откат на ``GET /status``. Legacy
  ``reconcile_interval_seconds`` работает только при ``auto_sync=False``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .client import PastuhAdsClient
from .exceptions import NotFoundError, PastuhAdsError, RateLimitError
from .models import OpTask, SessionStatusResponse
from .observation import user_observation_from_user

logger = logging.getLogger(__name__)

# Action types, открывающиеся прямой Telegram-ссылкой в один тап:
# серверный переход не является доказательством — верификация идёт
# по chat_member / chat_join_request / BotStartEvent / presence-пробе.
_ONE_TAP_ACTIONS = frozenset({
    "subscription_confirmed",
    "join_request_submitted",
    "bot_start_confirmed",
    "bot_new_user_verified",
})

# Action types с серверным transition — callback обязателен:
# клик здесь — оплачиваемое действие (V3.3), переход вне наблюдения.
_PAID_CALLBACK_ACTIONS = frozenset({"bot_tracked_click", "bot_click"})

# Reopen-CTA существует только для оплаченных click-заданий.
_REOPENABLE_ACTIONS = _PAID_CALLBACK_ACTIONS

_MAIN_TEXT = "Чтобы продолжить, подпишитесь на ресурсы."
_COMPLETED_TEXT = "🎉 Все задания выполнены. Доступ открыт!"
_SLOT_TEXT = "Перейдите по кнопке и нажмите «Запустить»."
_SLOT_BUTTON_TEXT = "Запустить бота"
_MAX_BUTTON_TEXT = 64  # лимит подписи inline-кнопки Telegram, байт

# Адаптивное расписание фоновой синхронизации: (сек. с последнего
# действия пользователя, интервал). Дальше последней границы — стоп
# до следующего события (open/callback/resume).
_SYNC_PHASES: tuple[tuple[float, float], ...] = ((60.0, 2.0), (180.0, 5.0), (600.0, 15.0))
# После 10 минут без действий пользователя polling НЕ прекращается:
# редкий idle-poll до expiry сессии — подписка «вернувшегося молча»
# пользователя всё равно снимает слот. Ограничение нагрузки: 120с —
# 1 батч-запрос на 50 сессий в 2 минуты (≤0.42 rps на 1000 сессий).
_SYNC_IDLE_INTERVAL = 120.0
_SYNC_IDLE_GIVEUP = 7200.0  # аварийная остановка, если expires_at неизвестен/бит
_SYNC_JITTER = 0.2  # ±20% — рассинхронизация стада после рестарта/ backoff'а
_SYNC_BATCH = 50
_SYNC_FIRST_DELAY = 1.0  # первый sync после действия пользователя
_SYNC_MIN_INTERVAL_FLOOR = 1.0  # нижняя граница интервала батч-запросов блока
_SYNC_FALLBACK_MIN_INTERVAL = 5.0  # GET /status на старом сервере — реже
_SYNC_FALLBACK_PER_TICK = 3
_STATE_RETENTION_SECONDS = 3600.0  # истёкшие блоки держим час (reopen/ответы), затем удаляем
_RETRY_AFTER_RE = re.compile(r"retry after (\d+)", re.IGNORECASE)

CB_OPEN = "paop:open:"
CB_REOPEN = "paop:reopen:"
CB_CHECK = "paop:check"

SendMessage = Callable[[int, str, list[list[dict[str, str]]]], Awaitable[int]]
EditMarkup = Callable[[int, int, list[list[dict[str, str]]]], Awaitable[None]]
EditText = Callable[[int, int, str, list[list[dict[str, str]]]], Awaitable[None]]
DeleteMessage = Callable[[int, int], Awaitable[None]]
AnswerCallback = Callable[[str], Awaitable[None]]
ErrorHook = Callable[[str, BaseException], None]


def _clip(text: str, limit: int = _MAX_BUTTON_TEXT) -> str:
    """Обрезка подписи кнопки под лимит Telegram (~64 байта)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    clipped = encoded[: limit - 3].decode("utf-8", errors="ignore")
    return clipped.rstrip() + "…"


@dataclass
class _BlockState:
    """Внутреннее состояние одного OP-блока (одна OP-сессия ↔ один чат)."""

    op_session_id: int
    telegram_user_id: int
    chat_id: int
    message_id: int | None = None
    expires_at: datetime | None = None
    tasks: dict[int, OpTask] = field(default_factory=dict)
    incomplete_ids: set[int] = field(default_factory=set)
    completed: bool = False
    access_allowed: bool = False
    paid_done_ids: set[int] = field(default_factory=set)
    # Задания, которые сервер хоть раз вернул завершёнными. Серверный
    # переход в complete терминален (taskGrantsAccess монотонен), поэтому
    # повторного появления быть не должно даже при гонке stale-ответов.
    server_done_ids: set[int] = field(default_factory=set)
    # Destination-строки: settlement подтверждён — задача показывается
    # прямой URL-кнопкой В ТОМ ЖЕ сообщении на ИСХОДНОЙ позиции до expiry.
    # НЕ удаляется sync/completion: деньги списаны в момент тапа, а бот
    # пользователем ещё не открыт — доступ к оплаченному destination
    # гарантирован финансовым контрактом. (answerCallbackQuery url для
    # third-party ботов официально не поддерживается Telegram — прямое
    # открытие невозможно, два нажатия зафиксированы честно.)
    destination_ids: set[int] = field(default_factory=set)
    # Concurrent-tap guard: settlement задачи уже в полёте — второй
    # одновременный тап не порождает дублирующий запрос (серверная
    # идемпотентность charged=false остаётся последней линией).
    open_inflight: set[int] = field(default_factory=set)
    # Вытесненный блок: render/resend полностью закрыт — ни один in-flight
    # sync/retry не перестроит клавиатуру после снятия (race-proof).
    retired: bool = False
    rev: int = 0
    slot_ref: tuple[int, int] | None = None  # (chat_id, message_id)
    shown_marked: bool = False
    render_failures: int = 0
    last_check_mono: float = 0.0  # piggyback /check throttle (monotonic)
    sync_active: bool = False  # участвует в фоновой синхронизации
    completion_notified: bool = False  # on_completed уже вызван (exactly-once)
    last_activity_mono: float = 0.0  # последнее действие пользователя
    next_sync_mono: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    slot_task: asyncio.Task | None = None
    reconcile_task: asyncio.Task | None = None

    def destination_url(self, task: OpTask) -> str | None:
        """Прямой адресат для слота/reopen: target_url (callback-контракт)
        или прямой t.me url. /r/{token} в слот не кладём."""
        for url in (task.target_url, task.url):
            if url and url.startswith("https://t.me/"):
                return url
        return None


class InteractiveOpBlock:
    """Движок интерактивного OP-блока поверх ``PastuhAdsClient``.

    Args:
        client: сконфигурированный ``PastuhAdsClient``.
        send_message: ``async (chat_id, text, inline_keyboard) -> message_id``.
        edit_markup: ``async (chat_id, message_id, inline_keyboard) -> None``.
        delete_message: ``async (chat_id, message_id) -> None``.
        edit_text: опционально ``async (chat_id, message_id, text,
            inline_keyboard) -> None`` — для смены текста при completion;
            без него текст остаётся стартовым (деградация безопасная).
        slot_ttl_seconds: TTL временного slot-сообщения (default 15).
        max_render_retries: число дополнительных попыток рендера после
            временной ошибки Telegram (backoff 2/5/15с), затем — ленивое
            восстановление при следующем событии.
        reconcile_interval_seconds: legacy фон-refresh через
            ``get_session_status`` (1.3.1). Действует только при
            ``auto_sync=False`` — иначе его заменяет адаптивная синхронизация.
        auto_sync: фоновая адаптивная синхронизация активных сессий
            (default True): выполненные слоты исчезают без «Проверить»,
            повторного /start и действий пользователя. False — поведение
            1.3.1.
        sync_min_interval_seconds: минимальный интервал между батч-запросами
            синхронизации одного блока (default 2.0, не меньше 1.0).
        check_min_interval_seconds: минимальный интервал между piggyback
            ``POST /check`` на наблюдаемых событиях пользователя
            (default 20). 0 — piggyback-проверки выключены.
        on_error: опциональный hook ``(context, exception)`` — для метрик;
            исключения не пробрасываются пользователю API.
        on_completed: опциональный callback ``(telegram_user_id,
            op_session_id)`` — sync или async. Вызывается ровно один раз
            на сессию в момент, когда все задания подтверждены и доступ
            открыт (любым путём: фоновая синхронизация, /check,
            callback-click, первичный op-list). Это completion event
            партнёрского бота: в нём уместно продолжить исходный сценарий
            пользователя (например, начать обработку ссылки, ради которой
            показывался ОП) — повторный /start не требуется. Исключения
            изолированы: ошибка hook'а не влияет на OP-механику. После
            рестарта: если сессия завершилась, пока бот был выключен,
            callback сработает один раз при resume (флаг exactly-once
            персистится в ``dump_state``).
    """

    def __init__(
        self,
        client: PastuhAdsClient,
        *,
        send_message: SendMessage,
        edit_markup: EditMarkup,
        delete_message: DeleteMessage,
        edit_text: EditText | None = None,
        slot_ttl_seconds: float = 15.0,
        max_render_retries: int = 3,
        reconcile_interval_seconds: float | None = None,
        check_min_interval_seconds: float = 20.0,
        on_error: ErrorHook | None = None,
        on_completed: Callable[[int, int], Any] | None = None,
        auto_sync: bool = True,
        sync_min_interval_seconds: float = 2.0,
        single_message_ux: bool = True,
        diagnostic_hook: Callable[[dict[str, Any]], None] | None = None,
        try_direct_url_answer: bool = False,
    ) -> None:
        self._client = client
        self._send = send_message
        self._edit_markup = edit_markup
        self._edit_text = edit_text
        self._delete = delete_message
        self._slot_ttl = max(0.05, float(slot_ttl_seconds))
        self._max_render_retries = max(0, int(max_render_retries))
        self._reconcile_interval = reconcile_interval_seconds
        self._check_min_interval = max(0.0, float(check_min_interval_seconds))
        self._on_error = on_error
        self._on_completed = on_completed
        self._auto_sync = bool(auto_sync)
        self._sync_min_interval = float(sync_min_interval_seconds)
        self._single_message_ux = bool(single_message_ux)
        self._diagnostic_hook = diagnostic_hook
        # Experimental: answerCallbackQuery(url) для third-party advertiser
        # bots официально НЕ поддерживается Telegram (deep links только к
        # собственному боту/игре). False — transform-only UX (два тапа).
        self._try_direct_url_answer = bool(try_direct_url_answer)
        self._sync_task: asyncio.Task | None = None
        self._sync_batch_supported = True
        self._sync_failures = 0
        self._sync_pause_until = 0.0
        # Ключ состояния — telegram_user_id: одна живая OP-сессия на
        # пользователя у партнёра (сервер переиспользует открытую).
        self._states: dict[int, _BlockState] = {}

    # ------------------------------------------------------------------
    # Открытие OP-блока
    # ------------------------------------------------------------------

    async def _retire_state(self, state: _BlockState) -> None:
        """Снять клавиатуру с вытесняемого OP-сообщения (best-effort).

        Сервер выдал новую сессию или пользователь перешёл в другой чат:
        прежнее сообщение больше не трекается — без очистки его кнопки
        («Запустить бота» и др.) остались бы активными вечно, хотя слот
        уже не должен показываться. Идемпотентно: повторное снятие —
        «message not modified», ошибки только в report. Текст не трогаем:
        edit_text может быть не подключён партнёром, а пустая клавиатура
        уже исключает нажатия.
        """
        state.retired = True
        state.sync_active = False
        if state.reconcile_task is not None and not state.reconcile_task.done():
            state.reconcile_task.cancel()
        if state.message_id is None:
            return
        try:
            await self._edit_markup(state.chat_id, state.message_id, [])
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            self._report("retire", exc)

    async def open(
        self,
        telegram_user_id: int,
        chat_id: int,
        *,
        user: Any = None,
        user_language: str | None = None,
        meta: dict[str, Any] | None = None,
        include_gender: bool = False,
        extra_capabilities: list[str] | None = None,
    ) -> int:
        """Открыть (или переиспользовать) OP-блок для пользователя.

        ``user`` — полный Telegram User апдейта: собирает
        ``user_observation`` (is_premium; gender — только при явном
        ``include_gender=True``, классификатор без opt-in не вызывается).
        Живая сессия для того же пользователя НЕ дублируется — повторный
        вызов перерисовывает существующее сообщение.

        Returns:
            message_id основного OP-сообщения.
        """
        self._prune_states()
        state = self._states.get(telegram_user_id)
        if state is not None and state.chat_id != chat_id:
            # Другой чат — новая сессия. Старый state вытесняется: клавиатура
            # прежнего сообщения снимается (никаких осиротевших CTA),
            # slot-task доживает свой TTL по ref-match guard.
            await self._retire_state(state)
            state = None

        capabilities = ["callback_cta", "interactive_op"]
        if extra_capabilities:
            capabilities.extend(extra_capabilities)

        observation = user_observation_from_user(user, include_gender=include_gender)
        op_list = await self._client.get_op_list(
            telegram_user_id,
            user_language=user_language,
            meta=meta,
            user_observation=observation,
            capabilities=capabilities,
        )

        if state is not None and state.op_session_id != op_list.op_session_id:
            # Сервер выдал НОВУЮ сессию: прежний блок вытесняется. Его
            # сообщение больше не трекается — клавиатуру снимаем, иначе
            # осиротевшие кнопки («Запустить бота») остаются активными
            # после вытеснения. Начинаем с чистого блока; слот доживает TTL.
            await self._retire_state(state)
            state = None

        if state is None:
            state = _BlockState(
                op_session_id=op_list.op_session_id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
            )
            self._states[telegram_user_id] = state

        state.op_session_id = op_list.op_session_id
        state.expires_at = _parse_dt(op_list.expires_at)
        self._merge_tasks(state, op_list.tasks, op_list.access_allowed)

        if state.message_id is None:
            text, keyboard = self._build(state)
            try:
                state.message_id = await self._send(state.chat_id, text, keyboard)
            except Exception as exc:  # noqa: BLE001 — Telegram callables
                self._report("open.send", exc)
                raise
            await self._ensure_shown(state)
        else:
            # Повторный /start той же сессии: не плодим сообщения —
            # обновляем текущее (и доотправляем /shown, если ранее сорвался).
            # Событие пользователя: throttled /check подтягивает уже
            # случившееся подтверждение (подписка между событиями).
            await self._maybe_check(state)
            await self._render(state)

        if self._auto_sync:
            self._touch(state)
        elif self._reconcile_interval is not None:
            self._ensure_reconcile(state)

        return state.message_id

    # ------------------------------------------------------------------
    # Callback-роутинг: paop:open / paop:reopen / paop:check
    # ------------------------------------------------------------------

    async def handle_callback(
        self,
        telegram_user_id: int,
        chat_id: int,
        data: str,
        *,
        answer: AnswerCallback | None = None,
    ) -> bool:
        """Обработать callback_query кнопки OP-блока.

        ``data`` — ``callback_query.data`` внешнего бота. ``answer`` —
        callable ответа на callback (aiogram ``callback.answer``);
        вызывается только при необходимости текстового ответа.

        Returns:
            ``True`` если callback наш и обработан, ``False`` если data
            не относится к OP-блоку (чужой callback партнёрского бота).
        """
        if not isinstance(data, str) or not data.startswith("paop:"):
            return False

        state = self._states.get(telegram_user_id)
        if state is None:
            await _answer(answer, "Сессия заданий не найдена. Отправьте /start заново.")
            return True
        if state.chat_id != chat_id:
            await _answer(answer, "Это устаревшая клавиатура из другого диалога.")
            return True
        self._diag("callback.received", state, data=data[:40])

        if data == CB_CHECK:
            await self._handle_check(state, answer)
            return True

        # Любой paop-callback — наблюдаемое событие пользователя:
        # throttled /check запускаем В ФОНЕ — он не является частью
        # critical path оплачиваемого перехода: settlement и render
        # не зависят от его результата, а ожидание добавляло бы
        # полный RTT Partner API к пользовательской задержке.
        self._spawn(state, self._maybe_check_safe(state))

        # Действие пользователя — фоновая синхронизация в быстрой фазе.
        self._touch(state)
        for prefix, handler in ((CB_OPEN, self._handle_open), (CB_REOPEN, self._handle_reopen)):
            if data.startswith(prefix):
                raw_id = data[len(prefix):]
                if raw_id.isdigit() and int(raw_id) > 0:
                    await handler(state, int(raw_id), answer)
                return True
        return True

    async def _handle_open(
        self,
        state: _BlockState,
        task_id: int,
        answer: AnswerCallback | None,
    ) -> None:
        """Оплачиваемый переход: серверная фиксация клика → TTL-слот.

        Финансовый исход определяет ТОЛЬКО серверный ответ: без
        подтверждённого перехода destination не показываем и задание
        локально не закрываем.
        """
        task = state.tasks.get(task_id)
        if task is None or task_id not in state.incomplete_ids:
            await _answer(answer, "Задание уже выполнено или не найдено.")
            await self._render(state)
            return
        if task_id in state.open_inflight:
            await _answer(answer, "Переход уже обрабатывается…")
            return

        state.open_inflight.add(task_id)
        try:
            await self._handle_open_inner(state, task, task_id, answer)
        finally:
            state.open_inflight.discard(task_id)

    async def _handle_open_inner(
        self,
        state: _BlockState,
        task: OpTask,
        task_id: int,
        answer: AnswerCallback | None,
    ) -> None:
        try:
            if task.action_type == "bot_tracked_click":
                self._diag("open.api_start", state, task_id=task_id)
                response = await self._client.register_callback_click(
                    task_id, state.telegram_user_id
                )
                self._diag("open.api_end", state, task_id=task_id,
                           charged=getattr(response, "charged", None))
                if response.target_url:
                    task.target_url = response.target_url
                # charged=False — уже оплаченный клик: повторная оплаты
                # нет ни по коду (идемпотентный endpoint), ни по состоянию.
                self._complete_task(state, task)
            elif task.action_type == "bot_click":
                # Существующий контракт click/verification: фиксируем
                # клик, верификацию подхватывает status-синхронизация.
                self._diag("open.api_start", state, task_id=task_id)
                await self._client.register_click(task_id, state.telegram_user_id)
                self._diag("open.api_end", state, task_id=task_id)
                await self._sync_status(state, trigger_check=True)
                if self._single_message_ux and task_id in state.incomplete_ids:
                    # Верификация ещё не подтверждена — пользователю нужно
                    # реально открыть бота: строка трансформируется в прямую
                    # URL-кнопку на месте. После server-side верификации
                    # она исчезнет (доступ уже получен — см. _complete_task).
                    url = state.destination_url(task)
                    if url is not None:
                        async with state.lock:
                            state.destination_ids.add(task.task_id)
                    if answer is not None:
                        await _answer(answer, "Нажмите кнопку ещё раз, чтобы открыть бота.")
                    await self._render(state)
                    return
            else:
                await _answer(answer, "Для этого задания переход не требует подтверждения.")
                return
        except PastuhAdsError as exc:
            # 422/409/410 и т.п. (чужой user, истёкшая сессия, уже
            # невалидная задача) — задание не завершаем, клавиатуру
            # пересобираем из актуального статуса.
            self._report("open.api", exc)
            await _answer(answer, f"Переход не засчитан: {exc.message}")
            with contextlib.suppress(PastuhAdsError):
                await self._sync_status(state, trigger_check=False)
            await self._render(state)
            return

        if self._single_message_ux:
            if task.action_type == "bot_tracked_click":
                await self._open_direct(state, task, answer)
            else:
                # bot_click, верифицированный до render (повторный тап):
                # пользователь уже в боте — слот не нужен, перерисовка.
                await self._render(state)
            return

        if not await self._send_slot(state, task):
            await _answer(answer, "Ссылка временно недоступна — попробуйте ещё раз.")
        await self._render(state)

    async def _open_direct(
        self,
        state: _BlockState,
        task: OpTask,
        answer: AnswerCallback | None,
    ) -> None:
        """Single-message UX: кнопка ТРАНСФОРМИРУЕТСЯ в прямую URL-кнопку
        на исходной позиции того же сообщения.

        Официальное ограничение Telegram: answerCallbackQuery(url) принимает
        только deep links к СОБСТВЕННОМУ боту или его игре — third-party
        advertiser bot открыть через callback answer нельзя. Поэтому UX
        честно двухтаповый: (1) тап по callback-кнопке — settlement,
        кнопка становится прямой t.me-ссылкой на месте; (2) тап по ней —
        нативное открытие рекламируемого бота. Никаких slot-сообщений,
        reopen-строк и воскрешений. Destination-строка живёт до expiry —
        деньги списаны при первом тапе, доступ гарантирован.

        Settlement уже подтверждён сервером (first-click-wins, повтор
        безопасен). answer-toast и render независимы — параллельно.
        """
        url = state.destination_url(task)
        if url is not None:
            async with state.lock:
                state.destination_ids.add(task.task_id)

        # Experimental: прямой url-answer выключен по умолчанию — third-party
        # официально не поддерживается. Включён — одна попытка, отказ
        # не меняет итоговую клавиатуру (destination-строка уже на месте).
        async def _answer_side() -> None:
            if answer is None:
                return
            if self._try_direct_url_answer and url is not None:
                if await self._try_answer_url(answer, url, state, task):
                    return
            await _answer(answer, "Переход засчитан — нажмите кнопку ещё раз, чтобы открыть бота.")

        answer_task = asyncio.ensure_future(_answer_side())
        render_task = asyncio.ensure_future(self._render(state))
        await asyncio.gather(answer_task, render_task, return_exceptions=True)

        self._diag("open.settled", state, task_id=task.task_id,
                   destination_row=url is not None)

    async def _try_answer_url(
        self,
        answer: AnswerCallback,
        url: str,
        state: _BlockState,
        task: OpTask,
    ) -> bool:
        """answerCallbackQuery(url=...) — прямое открытие целевого бота.

        Bot API: url открывается клиентом пользователя; для t.me-ссылок
        — нативный переход без браузера. Официально документировано для
        ссылок вида t.me/your_bot?start=... — поведение для third-party
        ботов подтверждается real-device QA (см. changelog 1.3.4).
        Возвращает False если callable не принимает url или Telegram
        отклонил запрос — вызывающий код включает fallback.
        """
        self._diag("answer_url.start", state, task_id=task.task_id)
        try:
            result = answer(url=url)  # type: ignore[call-arg]
            if inspect.isawaitable(result):
                await result
        except TypeError:
            # callable партнёра не принимает kwarg url — старая интеграция
            self._diag("answer_url.unsupported", state, task_id=task.task_id)
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — Telegram rejection
            self._report("answer_url", exc)
            self._diag("answer_url.rejected", state, task_id=task.task_id)
            return False
        self._diag("answer_url.ok", state, task_id=task.task_id)
        return True

    async def _handle_reopen(
        self,
        state: _BlockState,
        task_id: int,
        answer: AnswerCallback | None,
    ) -> None:
        """Reopen оплаченного destination — БЕЗ финансового вызова.

        Проверки: задание принадлежит сессии, завершено, сессия не
        истекла, пользователь/чат — владелец (уже проверены роутером).
        Повторная оплата исключена по построению — здесь нет ни одного
        вызова Partner API.
        """
        task = state.tasks.get(task_id)
        if task is None or task.action_type not in _REOPENABLE_ACTIONS:
            await _answer(answer, "Повторное открытие для этого задания недоступно.")
            return
        if task_id in state.incomplete_ids:
            await _answer(answer, "Задание ещё не оплачено — откройте его кнопкой выше.")
            await self._render(state)
            return
        if self._expired(state):
            await _answer(answer, "Сессия истекла — отправьте /start заново.")
            return

        if not await self._send_slot(state, task):
            await _answer(answer, "Ссылка временно недоступна — попробуйте ещё раз.")

    async def _handle_check(
        self,
        state: _BlockState,
        answer: AnswerCallback | None,
    ) -> None:
        """Ручной «Проверить» из СТАРОЙ клавиатуры (compat).

        В новых клавиатурах кнопки нет, но callback всё ещё может прийти
        с клавиатур, отрендеренных прежними версиями — контракт
        сохраняем: серверная проверка + пересборка.
        """
        await _answer(answer, "Проверяем выполнение…")
        state.last_check_mono = time.monotonic()
        try:
            await self._sync_status(state, trigger_check=True)
        except PastuhAdsError as exc:
            self._report("check.api", exc)
            if exc.error_code == "session_expired":
                await _answer(answer, "Сессия истекла — отправьте /start заново.")
            else:
                await _answer(answer, "Проверка временно недоступна — попробуйте позже.")
            return
        await self._render(state)

    # ------------------------------------------------------------------
    # Sync с сервером + merge состояния
    # ------------------------------------------------------------------

    async def _sync_status(self, state: _BlockState, *, trigger_check: bool) -> None:
        if trigger_check:
            status = await self._client.check_session(state.op_session_id)
        else:
            status = await self._client.get_session_status(state.op_session_id)
        self._merge_status(state, status)

    async def _maybe_check(self, state: _BlockState) -> None:
        """Throttled ``POST /check`` на наблюдаемом событии пользователя.

        Не чаще ``check_min_interval_seconds`` на блок, только для живой
        незавершённой сессии. Ошибки — в ``_report``: событие пользователя
        не падает из-за фоновой проверки. Рендер — на вызывающей стороне.
        """
        if self._check_min_interval <= 0 or state.completed:
            return
        now = time.monotonic()
        if state.last_check_mono > 0 and now - state.last_check_mono < self._check_min_interval:
            return
        state.last_check_mono = now
        try:
            await self._sync_status(state, trigger_check=True)
        except Exception as exc:  # noqa: BLE001 — фоновая проба не
            # должна ронять пользовательское событие ни при каком сбое.
            self._report("maybe_check", exc)

    def _merge_status(self, state: _BlockState, status: SessionStatusResponse) -> None:
        state.expires_at = _parse_dt(status.expires_at) or state.expires_at
        state.access_allowed = status.access_allowed
        self._merge_tasks(state, status.tasks, status.access_allowed,
                          incomplete_ids={t.task_id for t in status.incomplete_tasks})

    def _merge_tasks(
        self,
        state: _BlockState,
        tasks: list[OpTask],
        access_allowed: bool,
        *,
        incomplete_ids: set[int] | None = None,
    ) -> None:
        """Монотонный merge: выполненное задание НЕ воскресает.

        Локально оплаченное (по ответу callback-click, ``paid_done_ids``)
        задание остаётся завершённым, даже если status по гонке вернул
        его в incomplete — пользователь не увидит вернувшуюся кнопку
        оплаченного задания. Остальные задания следуют серверному
        вердикту: он авторитетен для обеих сторон перехода.
        """
        by_id = {t.task_id: t for t in tasks if t.task_id}
        state.tasks.update(by_id)
        # Монотонность: задание, присутствующее в ответе, но НЕ в его
        # incomplete-списке, сервер считает завершённым — фиксируем
        # терминально. Stale-ответ (in-flight до серверного confirm)
        # больше не воскрешает кнопку.
        if incomplete_ids is not None:
            state.server_done_ids.update(
                i for i in by_id if i not in incomplete_ids
            )
        done = state.paid_done_ids | state.server_done_ids
        if incomplete_ids is None:
            # op-list: все выданные задания незавершённые.
            state.incomplete_ids = set(by_id) - done
        else:
            state.incomplete_ids = {
                i for i in incomplete_ids
                if i in state.tasks and i not in done
            }
        state.completed = access_allowed or not state.incomplete_ids
        state.access_allowed = state.access_allowed or access_allowed
        self._notify_completed(state)

    def _complete_task(self, state: _BlockState, task: OpTask) -> None:
        state.paid_done_ids.add(task.task_id)
        state.incomplete_ids.discard(task.task_id)
        if task.action_type == "bot_click":
            # Верификация bot_click завершена — значит, пользователь уже
            # запустил бота (presence доказан сервером): доступ получен,
            # destination-строка больше не нужна и исчезает. Отличие от
            # bot_tracked_click, где settlement при тапе предшествует
            # реальному открытию — там строка живёт до expiry.
            state.destination_ids.discard(task.task_id)
        if not state.incomplete_ids:
            state.completed = True
            state.access_allowed = True
        self._notify_completed(state)

    # ------------------------------------------------------------------
    # Клавиатура и текст
    # ------------------------------------------------------------------

    def _build(self, state: _BlockState) -> tuple[str, list[list[dict[str, str]]]]:
        rows: list[list[dict[str, str]]] = []
        ordered = sorted(state.tasks.values(), key=lambda t: (t.slot, t.task_id))

        if state.completed:
            # Completion: текст «Доступ открыт». В single-message UX
            # destination-строки СОХРАНЯЮТСЯ — completion не уничтожает
            # последний доступный переход: settlement списал деньги при
            # тапе, доступ к боту гарантирован до expiry. Legacy — reopen.
            if self._single_message_ux:
                rows.extend(self._destination_rows(state, ordered))
            else:
                rows.extend(self._reopen_rows(state, ordered))
            return _COMPLETED_TEXT, rows

        destination_rendered: set[int] = set()
        for task in ordered:
            if self._single_message_ux and task.task_id in state.destination_ids:
                # Позиционная трансформация: строка остаётся на исходном
                # месте, превращаясь в прямую URL-кнопку — пользователь
                # видит изменение того же элемента, а не новый ряд снизу.
                url = state.destination_url(task)
                if url is not None:
                    rows.append([{"text": _clip(task.action_button_text or "Запустить бота"), "url": url}])
                    destination_rendered.add(task.task_id)
                continue
            if task.task_id not in state.incomplete_ids:
                continue
            button = self._task_button(task)
            if button is not None:
                rows.append([button])

        if self._single_message_ux:
            # destination-строки вне ordered (edge: задача ушла из status)
            # — всё равно не теряем доступ: доотрисовка внизу.
            rows.extend(self._destination_rows(state, ordered, skip=destination_rendered))
        else:
            rows.extend(self._reopen_rows(state, ordered))
        # UX-контракт v2: кнопки «Проверить выполнение» нет — проверка
        # event-driven (server events → /check piggyback на событиях
        # пользователя → опциональный reconcile-цикл). paop:check из
        # старых клавиатур по-прежнему роутится в handle_callback.
        return _MAIN_TEXT, rows

    def _task_button(self, task: OpTask) -> dict[str, str] | None:
        text = _clip(task.action_button_text or "Открыть")
        if task.action_type in _ONE_TAP_ACTIONS and task.url:
            return {"text": text, "url": task.url}
        if task.action_type in _PAID_CALLBACK_ACTIONS:
            if task.open_mode == "callback" or task.action_type == "bot_click":
                return {"text": text, "callback_data": f"{CB_OPEN}{task.task_id}"}
            # Legacy open_mode='url' (capability не принята / флаг OFF):
            # прежний контракт — url-кнопка на task.url (/r/{token}).
            if task.url:
                return {"text": text, "url": task.url}
            return None
        # Внешние сайты и прочие — прежняя url-кнопка.
        if task.url:
            return {"text": text, "url": task.url}
        return {"text": text, "callback_data": f"{CB_OPEN}{task.task_id}"}

    def _reopen_rows(
        self, state: _BlockState, ordered: list[OpTask]
    ) -> list[list[dict[str, str]]]:
        rows: list[list[dict[str, str]]] = []
        for task in ordered:
            if task.action_type not in _REOPENABLE_ACTIONS:
                continue
            if task.task_id in state.incomplete_ids:
                continue
            if state.destination_url(task) is None:
                continue
            # Нейтральная подпись: имя/username ресурса не показываем.
            rows.append([{
                "text": "🔁 Запустить бота",
                "callback_data": f"{CB_REOPEN}{task.task_id}",
            }])
        return rows

    def _destination_rows(
        self,
        state: _BlockState,
        ordered: list[OpTask],
        skip: set[int] | None = None,
    ) -> list[list[dict[str, str]]]:
        """Прямые URL-кнопки оплаченных destination в ОСНОВНОМ сообщении.

        Это НЕ reopen-строки и НЕ slot-сообщения: кнопка — прямая
        t.me-ссылка (нативное открытие), живёт в том же OP-сообщении до
        expiry. Settlement подтверждён — доступ является финансовым
        обязательством: sync и completion строку не убирают.
        """
        rows: list[list[dict[str, str]]] = []
        skip = skip or set()
        for task in ordered:
            if task.task_id in skip:
                continue
            if task.task_id not in state.destination_ids:
                continue
            url = state.destination_url(task)
            if url is None:
                continue
            rows.append([{"text": _clip(task.action_button_text or "Запустить бота"), "url": url}])
        return rows

    # ------------------------------------------------------------------
    # Render с revision-fencing
    # ------------------------------------------------------------------

    async def _render(self, state: _BlockState) -> None:
        """Перерисовать основное сообщение идемпотентно.

        Бамп ревизии и сборка — под коротким локом (наносекунды), сам
        вызов Telegram — снаружи. Перед вызовом проверяем, что ревизия
        не устарела: конкурирующий рендер с большей rev подавляет наш —
        последняя запись всегда отражает самое свежее состояние.
        """
        if state.retired:
            return  # вытесненный блок: клавиатура не перестраивается никогда

        async with state.lock:
            state.rev += 1
            rev = state.rev
            text, keyboard = self._build(state)
            message_id = state.message_id

        if message_id is None or rev != state.rev:
            return

        self._diag("render.start", state, rev=rev)
        try:
            if state.completed and self._edit_text is not None:
                await self._edit_text(state.chat_id, message_id, text, keyboard)
            else:
                await self._edit_markup(state.chat_id, message_id, keyboard)
            state.render_failures = 0
            self._diag("render.end", state, rev=rev)
        except Exception as exc:  # noqa: BLE001 — Telegram callables
            lowered = str(exc).lower()
            if "not modified" in lowered:
                # Сообщение актуально — сессия показана: доотправляем /shown.
                state.render_failures = 0
                await self._ensure_shown(state)
                return
            if _is_gone_message_error(lowered):
                # Основное сообщение удалено — пересоздаём один раз.
                async with state.lock:
                    if state.message_id != message_id:
                        return  # параллельный рендер уже пересоздал
                    state.message_id = None
                await self._resend(state)
                return
            self._report("render", exc)
            state.render_failures += 1
            self._schedule_render_retry(state, _retry_after_seconds(exc))
            return
        await self._ensure_shown(state)

    async def _resend(self, state: _BlockState) -> None:
        if state.retired:
            return
        async with state.lock:
            state.rev += 1
            rev = state.rev
            text, keyboard = self._build(state)
        if rev != state.rev:
            return
        try:
            state.message_id = await self._send(state.chat_id, text, keyboard)
            state.render_failures = 0
        except Exception as exc:  # noqa: BLE001
            self._report("resend", exc)
            return
        await self._ensure_shown(state)

    async def _ensure_shown(self, state: _BlockState) -> None:
        """Отметить сессию показанной — только после того, как основное
        сообщение реально отображено (send/edit успешны либо «not modified»).

        Ошибка ``mark_shown`` не фатальна: флаг остаётся снятым и вызов
        повторится при следующем успешном рендере — сессия не зависает
        в ``created``, когда пользователь блок видел.
        """
        if state.shown_marked:
            return
        try:
            await self._client.mark_shown(state.op_session_id)
        except PastuhAdsError as exc:
            self._report("shown", exc)
            return
        state.shown_marked = True

    def _schedule_render_retry(self, state: _BlockState, retry_after: float | None = None) -> None:
        """Ограниченное восстановление после временной ошибки: экспоненциальный
        backoff, максимум max_render_retries попыток — дальше ленивая
        reconcile при следующем пользовательском событии. Telegram 429
        (``retry after N``) — ждём ровно указанное время (≤ 60с)."""
        if state.render_failures > self._max_render_retries:
            return
        delay = min(2.0 * (state.render_failures ** 2), 30.0)
        if retry_after is not None:
            delay = min(max(delay, retry_after + 0.5), 60.0)
        attempt_rev = state.rev

        async def _retry() -> None:
            try:
                await asyncio.sleep(delay)
                if state.rev != attempt_rev:
                    return  # более свежий рендер уже случился/идёт
                await self._render(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._report("render.retry", exc)

        self._spawn(state, _retry())

    # ------------------------------------------------------------------
    # Временный destination-слот (TTL, ref-match guard)
    # ------------------------------------------------------------------

    async def _send_slot(self, state: _BlockState, task: OpTask) -> bool:
        """Слот с прямой t.me-ссылкой и отложенным удалением по TTL."""
        url = state.destination_url(task)
        if url is None:
            return False

        # OP V2: нейтральный слот — имя бота не влияет на решение.
        # Маршрутизация остаётся уникальной по URL (token в ссылке).
        keyboard = [[{"text": _SLOT_BUTTON_TEXT, "url": url}]]
        try:
            message_id = await self._send(state.chat_id, _SLOT_TEXT, keyboard)
        except Exception as exc:  # noqa: BLE001
            self._report("slot.send", exc)
            return False

        ref = (state.chat_id, message_id)
        async with state.lock:
            old_ref = state.slot_ref
            state.slot_ref = ref
        if state.slot_task is not None and not state.slot_task.done():
            state.slot_task.cancel()
        state.slot_task = self._spawn(state, self._slot_ttl_watch(state, ref))

        # Заменённый слот удаляем сразу: его ref устарел, собственный
        # TTL-job нового ref за старый не отвечает — без удаления старый
        # destination-маячок остался бы висеть вечно. Удаляем только
        # сообщения, которые сами отправляли (ref наш).
        if old_ref is not None and old_ref != ref:
            self._spawn(state, self._delete_quiet(old_ref))
        return True

    async def _delete_quiet(self, ref: tuple[int, int]) -> None:
        try:
            await self._delete(ref[0], ref[1])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            lowered = str(exc).lower()
            if "not found" not in lowered and "can't be deleted" not in lowered:
                self._report("slot.supersede_delete", exc)

    async def _slot_ttl_watch(self, state: _BlockState, ref: tuple[int, int]) -> None:
        """Удалить слот через TTL — только если ref всё ещё текущий.

        Новый слот меняет ref → старый TTL-job ничего не трогает
        (ref-match guard, аналог серверного DeleteHostedOpSlotMessageJob).
        """
        try:
            await asyncio.sleep(self._slot_ttl)
        except asyncio.CancelledError:
            return

        async with state.lock:
            if state.slot_ref != ref:
                return  # слот заменён — этот job устарел
            state.slot_ref = None

        for attempt in (1, 2):
            try:
                await self._delete(ref[0], ref[1])
                return
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                lowered = str(exc).lower()
                if "not found" in lowered or "can't be deleted" in lowered:
                    return  # уже удалён пользователем — принято
                if attempt == 1:
                    await asyncio.sleep(2.0)
                else:
                    self._report("slot.delete", exc)

    # ------------------------------------------------------------------
    # Мягкий reconcile (опциональный, не polling-шторм)
    # ------------------------------------------------------------------

    def _ensure_reconcile(self, state: _BlockState) -> None:
        if state.reconcile_task is not None and not state.reconcile_task.done():
            return
        state.reconcile_task = self._spawn(state, self._reconcile_loop(state))

    async def _reconcile_loop(self, state: _BlockState) -> None:
        interval = self._reconcile_interval
        assert interval is not None
        cycles = 0
        try:
            while not self._expired(state) and cycles < 240:
                await asyncio.sleep(interval)
                cycles += 1
                try:
                    await self._sync_status(state, trigger_check=False)
                    await self._render(state)
                    if state.completed:
                        return  # финальное состояние уже показано
                except PastuhAdsError as exc:
                    self._report("reconcile.status", exc)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Restart: dump/restore/resume
    # ------------------------------------------------------------------

    def dump_state(self) -> dict[str, Any]:
        """Сериализовать состояние блоков для сохранения партнёром
        (Redis/файл). После рестарта: ``restore_state`` + ``resume``."""
        return {
            "version": 1,
            "blocks": [
                {
                    "op_session_id": s.op_session_id,
                    "telegram_user_id": s.telegram_user_id,
                    "chat_id": s.chat_id,
                    "message_id": s.message_id,
                    "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                    "completed": s.completed,
                    "completion_notified": s.completion_notified,
                    "access_allowed": s.access_allowed,
                    "shown_marked": s.shown_marked,
                    "tasks": [_task_to_dict(t) for t in s.tasks.values()],
                    "incomplete_ids": sorted(s.incomplete_ids),
                    "paid_done_ids": sorted(s.paid_done_ids),
                    "server_done_ids": sorted(s.server_done_ids),
                    "destination_ids": sorted(s.destination_ids),
                }
                for s in self._states.values()
            ],
        }

    def restore_state(self, data: dict[str, Any]) -> None:
        """Восстановить состояние из ``dump_state`` (без сетевых вызовов)."""
        for block in (data or {}).get("blocks") or []:
            try:
                state = _BlockState(
                    op_session_id=int(block["op_session_id"]),
                    telegram_user_id=int(block["telegram_user_id"]),
                    chat_id=int(block["chat_id"]),
                    message_id=block.get("message_id"),
                    expires_at=_parse_dt(block.get("expires_at")),
                    tasks={},
                    incomplete_ids={int(i) for i in block.get("incomplete_ids") or []},
                    paid_done_ids={int(i) for i in block.get("paid_done_ids") or []},
                    server_done_ids={int(i) for i in block.get("server_done_ids") or []},
                    destination_ids={int(i) for i in block.get("destination_ids") or []},
                    completed=bool(block.get("completed")),
                    # Дампы без флага (первые версии 1.3.2): False —
                    # completion, случившийся в простое бота, будет
                    # доставлен один раз при resume.
                    completion_notified=bool(block.get("completion_notified", False)),
                    access_allowed=bool(block.get("access_allowed")),
                    shown_marked=bool(block.get("shown_marked", True)),
                )
                for raw in block.get("tasks") or []:
                    task = _task_from_dict(raw)
                    if task is not None:
                        state.tasks[task.task_id] = task
            except (KeyError, TypeError, ValueError):
                continue
            self._states[state.telegram_user_id] = state

    async def resume(self, telegram_user_id: int | None = None) -> None:
        """После рестарта: перечитать status и перестроить клавиатуру.

        Без ``telegram_user_id`` — для всех восстановленных блоков.
        Ошибки одного блока не мешают остальным; удалённое основное
        сообщение пересоздаётся рендером автоматически.
        """
        targets = (
            [self._states[telegram_user_id]]
            if telegram_user_id is not None and telegram_user_id in self._states
            else list(self._states.values()) if telegram_user_id is None else []
        )
        for state in targets:
            try:
                if self._expired(state):
                    continue
                await self._sync_status(state, trigger_check=False)
                await self._render(state)
                if self._auto_sync:
                    self._touch(state)
            except (PastuhAdsError, Exception) as exc:  # noqa: BLE001
                self._report("resume", exc)

    async def aclose(self) -> None:
        """Отменить фоновые задачи (slot TTL, retry, reconcile, sync)."""
        pending = [t for s in self._states.values()
                   for t in (s.slot_task, s.reconcile_task) if t is not None]
        if self._sync_task is not None:
            pending.append(self._sync_task)
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._sync_task = None

    # ------------------------------------------------------------------
    # Фоновая адаптивная синхронизация (1.3.2)
    # ------------------------------------------------------------------

    def _touch(self, state: _BlockState, *, delay: float | None = None) -> None:
        """Действие пользователя: блок в быструю фазу, ближайший sync через
        ``delay``. Один общий цикл на блок — повторы не плодят циклов."""
        if not self._auto_sync or state.completed or self._expired(state):
            return
        now = time.monotonic()
        state.last_activity_mono = now
        due = now + (_SYNC_FIRST_DELAY if delay is None else delay)
        state.next_sync_mono = min(state.next_sync_mono, due) if state.sync_active else due
        state.sync_active = True
        self._ensure_sync_loop()

    def _ensure_sync_loop(self) -> None:
        if self._sync_task is not None and not self._sync_task.done():
            return
        try:
            self._sync_task = asyncio.ensure_future(self._sync_loop())
        except RuntimeError:
            self._sync_task = None  # нет запущенного event loop

    @staticmethod
    def _sync_interval(state: _BlockState, now: float) -> float | None:
        """Интервал синхронизации по давности последнего действия.

        Фазы: 2с → 5с → 15с, далее редкий idle-poll (120с) до expiry
        сессии. None — только после аварийного giveup (2ч без событий)
        либо когда цикл должен остановиться по completed/expired.
        """
        idle = now - state.last_activity_mono
        for limit, interval in _SYNC_PHASES:
            if idle < limit:
                return interval
        if idle < _SYNC_IDLE_GIVEUP:
            return _SYNC_IDLE_INTERVAL
        return None

    async def _sync_loop(self) -> None:
        try:
            while True:
                now = time.monotonic()
                if now < self._sync_pause_until:
                    await asyncio.sleep(self._sync_pause_until - now)
                    continue
                active: list[_BlockState] = []
                for state in list(self._states.values()):
                    if not state.sync_active:
                        continue
                    if state.completed or self._expired(state) or self._sync_interval(state, now) is None:
                        state.sync_active = False
                        continue
                    active.append(state)
                if not active:
                    return
                due = sorted((s for s in active if s.next_sync_mono <= now),
                             key=lambda s: s.next_sync_mono)
                if not due:
                    wake = min(s.next_sync_mono for s in active)
                    await asyncio.sleep(max(0.05, wake - now))
                    continue
                await self._sync_due(due)
                gap = max(_SYNC_MIN_INTERVAL_FLOOR, self._sync_min_interval)
                await asyncio.sleep(gap if len(due) <= _SYNC_BATCH else _SYNC_MIN_INTERVAL_FLOOR)
        except asyncio.CancelledError:
            return

    async def _sync_due(self, due: list[_BlockState]) -> None:
        now = time.monotonic()
        try:
            if self._sync_batch_supported:
                batch = due[:_SYNC_BATCH]
                for state in batch:
                    interval = self._sync_interval(state, now) or 15.0
                    state.next_sync_mono = now + self._jittered(interval)
                try:
                    response = await self._client.sync_sessions([s.op_session_id for s in batch])
                except NotFoundError:
                    # Сервер без /op-sessions/sync — GET /status реже и порциями.
                    self._sync_batch_supported = False
                    await self._sync_individually(batch)
                else:
                    by_id = {item.op_session_id: item for item in response.sessions}
                    unknown = set(response.unknown_session_ids)
                    for state in batch:
                        if state.op_session_id in unknown:
                            state.sync_active = False
                            continue
                        item = by_id.get(state.op_session_id)
                        if item is not None and self._apply_snapshot(
                            state, item.incomplete_task_ids, item.access_allowed,
                            item.expires_at, item.session_status,
                        ):
                            self._spawn(state, self._render(state))
            else:
                await self._sync_individually(due[:_SYNC_FALLBACK_PER_TICK])
        except RateLimitError as exc:
            self._sync_backoff(exc, base=5.0, cap=60.0)
            return
        except PastuhAdsError as exc:
            self._sync_backoff(exc, base=2.0, cap=30.0)
            return
        except Exception as exc:  # noqa: BLE001 — фоновый цикл не должен умирать
            self._sync_backoff(exc, base=2.0, cap=30.0)
            return
        self._sync_failures = 0

    async def _sync_individually(self, states: list[_BlockState]) -> None:
        now = time.monotonic()
        for state in states:
            base = max(self._sync_interval(state, now) or 15.0, _SYNC_FALLBACK_MIN_INTERVAL)
            state.next_sync_mono = now + self._jittered(base)
            try:
                status = await self._client.get_session_status(state.op_session_id)
            except RateLimitError:
                raise
            except PastuhAdsError as exc:
                if exc.error_code == "session_expired" or exc.status_code in (403, 404, 409):
                    state.sync_active = False
                    continue
                raise
            if self._apply_snapshot(
                state, [t.task_id for t in status.incomplete_tasks],
                status.access_allowed, status.expires_at, status.session_status,
            ):
                self._spawn(state, self._render(state))

    @staticmethod
    def _jittered(interval: float) -> float:
        return interval * random.uniform(1.0 - _SYNC_JITTER, 1.0 + _SYNC_JITTER)

    def _sync_backoff(self, exc: BaseException, *, base: float, cap: float) -> None:
        self._report("sync", exc)
        self._sync_failures += 1
        delay = min(cap, base * (2 ** (self._sync_failures - 1)))
        # Серверный Retry-After (429) — авторитетный минимум паузы.
        hint = getattr(exc, "retry_after", None)
        if hint:
            delay = max(delay, float(hint))
        self._sync_pause_until = time.monotonic() + delay

    def _apply_snapshot(
        self,
        state: _BlockState,
        incomplete_task_ids: list[int],
        access_allowed: bool,
        expires_at: str | None,
        session_status: str,
    ) -> bool:
        """Монотонный merge снимка: задания только убывают — выполненное
        никогда не возвращается на клавиатуру. True — нужно перерисовать."""
        before = (frozenset(state.incomplete_ids), state.completed)
        state.expires_at = _parse_dt(expires_at) or state.expires_at
        server_incomplete = {int(i) for i in incomplete_task_ids}
        # Терминальная фиксация серверных завершений: известные задания
        # вне incomplete-списка выполнены навсегда.
        state.server_done_ids.update(
            i for i in state.incomplete_ids if i not in server_incomplete
        )
        done = state.paid_done_ids | state.server_done_ids
        state.incomplete_ids = {
            i for i in state.incomplete_ids
            if i in server_incomplete and i not in done
        }
        state.access_allowed = state.access_allowed or bool(access_allowed)
        state.completed = state.access_allowed or not state.incomplete_ids
        if state.completed or session_status in ("expired", "failed"):
            state.sync_active = False
        self._notify_completed(state)
        return before != (frozenset(state.incomplete_ids), state.completed)

    def _notify_completed(self, state: _BlockState) -> None:
        """Exactly-once completion event партнёра: срабатывает при первом
        переходе сессии в completed из любого пути обнаружения."""
        if not state.completed or state.completion_notified:
            return
        state.completion_notified = True
        if self._on_completed is None:
            return
        self._spawn(state, self._run_completed(state))

    async def _run_completed(self, state: _BlockState) -> None:
        try:
            result = self._on_completed(state.telegram_user_id, state.op_session_id)  # type: ignore[misc]
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 — hook партнёра не ломает OP
            self._report("on_completed", exc)

    def _prune_states(self) -> None:
        """Удалить давно истёкшие блоки — без роста памяти у долгоживущего бота."""
        now = datetime.now(timezone.utc).timestamp()
        stale = [
            uid for uid, s in self._states.items()
            if s.expires_at is not None
            and now - s.expires_at.timestamp() > _STATE_RETENTION_SECONDS
            and (s.slot_task is None or s.slot_task.done())
        ]
        for uid in stale:
            state = self._states.pop(uid)
            if state.reconcile_task is not None and not state.reconcile_task.done():
                state.reconcile_task.cancel()

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------

    def _expired(self, state: _BlockState) -> bool:
        return (
            state.expires_at is not None
            and state.expires_at <= datetime.now(timezone.utc)
        )

    async def _maybe_check_safe(self, state: _BlockState) -> None:
        """Фоновый piggyback /check — изолирован от critical path."""
        try:
            await self._maybe_check(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — фон, ошибки в метрики
            self._report("maybe_check.bg", exc)

    def _diag(self, event: str, state: _BlockState, **extra: Any) -> None:
        """Diagnostic timing hook — monotonic clock, корреляция по сессии.

        События: callback.received, open.api_start/api_end, answer_url.*,
        render.start/end, settle.* — партнёр (Boom) подключает hook и
        получает точный latency breakdown без переписки интеграции.
        Исключения hook'а изолированы — не влияют на OP-механику.
        """
        hook = self._diagnostic_hook
        if hook is None:
            return
        payload = {
            "event": event,
            "mono": time.monotonic(),
            "op_session_id": state.op_session_id,
            "telegram_user_id": state.telegram_user_id,
            "message_id": state.message_id,
            **extra,
        }
        try:
            hook(payload)
        except Exception:  # noqa: BLE001 — диагностика не ломает механику
            pass

    def _report(self, context: str, exc: BaseException) -> None:
        # Логируем только тип ошибки — без chat_id/user_id payload.
        logger.warning("InteractiveOpBlock %s failed: %s", context, type(exc).__name__)
        if self._on_error is not None:
            with contextlib.suppress(Exception):
                self._on_error(context, exc)

    def _spawn(self, state: _BlockState, coro) -> asyncio.Task:  # noqa: ANN001
        task = asyncio.ensure_future(coro)

        def _watch(finished: asyncio.Task) -> None:
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                self._report("task", exc)

        task.add_done_callback(_watch)
        return task


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Telegram 429: aiogram ``TelegramRetryAfter.retry_after`` или текст
    ``Too Many Requests: retry after N``."""
    value = getattr(exc, "retry_after", None)
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    match = _RETRY_AFTER_RE.search(str(exc))
    return float(match.group(1)) if match else None


def _is_gone_message_error(lowered: str) -> bool:
    return any(
        needle in lowered
        for needle in (
            "message to edit not found",
            "message can't be edited",
            "message identifier is not specified",
            "message_id_is_invalid",
        )
    )


async def _answer(answer: AnswerCallback | None, text: str) -> None:
    if answer is None:
        return
    with contextlib.suppress(Exception):
        await answer(text)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _task_to_dict(task: OpTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "slot": task.slot,
        "advertised_resource_id": task.advertised_resource_id,
        "target_type": task.target_type,
        "action_type": task.action_type,
        "title": task.title,
        "username": task.username,
        "url": task.url,
        "action_button_text": task.action_button_text,
        "open_mode": task.open_mode,
        "target_url": task.target_url,
        "cta_callback_data": task.cta_callback_data,
        "status": task.status,
        "verification_status": task.verification_status,
    }


def _task_from_dict(raw: dict[str, Any]) -> OpTask | None:
    try:
        return OpTask(
            task_id=int(raw["task_id"]),
            slot=int(raw.get("slot") or 0),
            advertised_resource_id=raw.get("advertised_resource_id"),
            target_type=raw.get("target_type"),
            action_type=raw.get("action_type"),
            title=raw.get("title"),
            username=raw.get("username"),
            url=raw.get("url"),
            instruction_text=None,
            action_button_text=raw.get("action_button_text"),
            verification_hint=None,
            open_mode=raw.get("open_mode"),
            target_url=raw.get("target_url"),
            cta_callback_data=raw.get("cta_callback_data"),
            status=raw.get("status"),
            verification_status=raw.get("verification_status"),
            raw=dict(raw),
        )
    except (KeyError, TypeError, ValueError):
        return None
