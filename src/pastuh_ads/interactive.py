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
  восстановление при следующем событии. Бесконечных retry и
  массового polling нет: опциональный ``reconcile_interval_seconds``
  по умолчанию выключен.
- Restart: ``dump_state()``/``restore_state()`` сериализуют сессии
  (dict — партнёр может держать их в Redis/памяти), ``resume()``
  перечитывает status и перестраивает клавиатуру — process restart
  безопасен.

Callback-data формат (принадлежит боту партнёра, НЕ платформе):
    ``paop:open:{task_id}``    — оплачиваемый клик (bot_tracked_click /
                               bot_click): серверный transition через API.
    ``paop:reopen:{task_id}``  — повторное открытие оплаченного
                               destination: слот без финансового вызова.
    ``paop:check``             — «Проверить выполнение»: POST /check.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .client import PastuhAdsClient
from .exceptions import PastuhAdsError
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
_CHECK_ALL_TEXT = "✅ Проверить выполнение"
_SLOT_TEXT_FALLBACK = "Нажмите кнопку ниже, чтобы перейти:"
_SLOT_BUTTON_TEXT = "Перейти"
_MAX_BUTTON_TEXT = 64  # лимит подписи inline-кнопки Telegram, байт

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
    rev: int = 0
    slot_ref: tuple[int, int] | None = None  # (chat_id, message_id)
    shown_marked: bool = False
    render_failures: int = 0
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
        reconcile_interval_seconds: опциональный мягкий фон-refresh через
            ``get_session_status`` (по умолчанию None = выключен). Это НЕ
            массовый polling — задача живёт только пока активна сессия.
        on_error: опциональный hook ``(context, exception)`` — для метрик;
            исключения не пробрасываются пользователю API.
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
        on_error: ErrorHook | None = None,
    ) -> None:
        self._client = client
        self._send = send_message
        self._edit_markup = edit_markup
        self._edit_text = edit_text
        self._delete = delete_message
        self._slot_ttl = max(0.05, float(slot_ttl_seconds))
        self._max_render_retries = max(0, int(max_render_retries))
        self._reconcile_interval = reconcile_interval_seconds
        self._on_error = on_error
        # Ключ состояния — telegram_user_id: одна живая OP-сессия на
        # пользователя у партнёра (сервер переиспользует открытую).
        self._states: dict[int, _BlockState] = {}

    # ------------------------------------------------------------------
    # Открытие OP-блока
    # ------------------------------------------------------------------

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
        state = self._states.get(telegram_user_id)
        if state is not None and state.chat_id != chat_id:
            # Другой чат — новая сессия. Старый state вытесняется: его
            # reconcile-цикл отменяем, чтобы устаревшие рендеры не шли
            # в прежний диалог. Slot-task доживает свой TTL и сам
            # убирает слот по ref-match guard — трогать его не нужно.
            if state.reconcile_task is not None and not state.reconcile_task.done():
                state.reconcile_task.cancel()
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
            # Сервер выдал НОВУЮ сессию: сохранённый message_id и флаги
            # принадлежат блоку ПРЕЖНЕЙ сессии — редактировать его нельзя
            # (сообщение ушло бы вглубь истории, а /shown новой сессии
            # никогда бы не отправился). Начинаем с чистого блока;
            # reconcile старого state отменяем, слот доживает TTL.
            if state.reconcile_task is not None and not state.reconcile_task.done():
                state.reconcile_task.cancel()
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
            await self._render(state)

        if self._reconcile_interval is not None:
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

        if data == CB_CHECK:
            await self._handle_check(state, answer)
            return True

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

        try:
            if task.action_type == "bot_tracked_click":
                response = await self._client.register_callback_click(
                    task_id, state.telegram_user_id
                )
                if response.target_url:
                    task.target_url = response.target_url
                # charged=False — уже оплаченный клик: повторная оплаты
                # нет ни по коду (идемпотентный endpoint), ни по состоянию.
                self._complete_task(state, task)
            elif task.action_type == "bot_click":
                # Существующий контракт click/verification: фиксируем
                # клик, верификацию подхватывает status-синхронизация.
                await self._client.register_click(task_id, state.telegram_user_id)
                await self._sync_status(state, trigger_check=True)
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

        if not await self._send_slot(state, task):
            await _answer(answer, "Ссылка временно недоступна — попробуйте ещё раз.")
        await self._render(state)

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
        """«Проверить выполнение»: серверная проверка + пересборка."""
        await _answer(answer, "Проверяем выполнение…")
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
        if incomplete_ids is None:
            # op-list: все выданные задания незавершённые.
            state.incomplete_ids = set(by_id) - state.paid_done_ids
        else:
            state.incomplete_ids = {
                i for i in incomplete_ids
                if i in state.tasks and i not in state.paid_done_ids
            }
        state.completed = access_allowed or not state.incomplete_ids
        state.access_allowed = state.access_allowed or access_allowed

    def _complete_task(self, state: _BlockState, task: OpTask) -> None:
        state.paid_done_ids.add(task.task_id)
        state.incomplete_ids.discard(task.task_id)
        if not state.incomplete_ids:
            state.completed = True
            state.access_allowed = True

    # ------------------------------------------------------------------
    # Клавиатура и текст
    # ------------------------------------------------------------------

    def _build(self, state: _BlockState) -> tuple[str, list[list[dict[str, str]]]]:
        rows: list[list[dict[str, str]]] = []
        ordered = sorted(state.tasks.values(), key=lambda t: (t.slot, t.task_id))

        if state.completed:
            # Completion: текст «Доступ открыт», только reopen-CTA —
            # главное сообщение НЕ удаляется.
            rows.extend(self._reopen_rows(state, ordered))
            return _COMPLETED_TEXT, rows

        for task in ordered:
            if task.task_id not in state.incomplete_ids:
                continue
            button = self._task_button(task)
            if button is not None:
                rows.append([button])

        rows.extend(self._reopen_rows(state, ordered))
        rows.append([{"text": _CHECK_ALL_TEXT, "callback_data": CB_CHECK}])
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
            label = f"@{task.username}" if task.username else (task.title or "бота")
            rows.append([{
                "text": _clip(f"🔁 Открыть {label}"),
                "callback_data": f"{CB_REOPEN}{task.task_id}",
            }])
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
        async with state.lock:
            state.rev += 1
            rev = state.rev
            text, keyboard = self._build(state)
            message_id = state.message_id

        if message_id is None or rev != state.rev:
            return

        try:
            if state.completed and self._edit_text is not None:
                await self._edit_text(state.chat_id, message_id, text, keyboard)
            else:
                await self._edit_markup(state.chat_id, message_id, keyboard)
            state.render_failures = 0
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
            self._schedule_render_retry(state)
            return
        await self._ensure_shown(state)

    async def _resend(self, state: _BlockState) -> None:
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

    def _schedule_render_retry(self, state: _BlockState) -> None:
        """Ограниченное восстановление после временной ошибки: экспоненциальный
        backoff, максимум max_render_retries попыток — дальше ленивая
        reconcile при следующем пользовательском событии."""
        if state.render_failures > self._max_render_retries:
            return
        delay = min(2.0 * (state.render_failures ** 2), 30.0)
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

        # Макет-эквивалент: слот зовёт по имени бота, когда оно известно —
        # «Перейдите в @bot и нажмите «Запустить».» / «Перейти в @bot».
        username = (task.username or "").lstrip("@")
        text = (
            f"Перейдите в @{username} и нажмите «Запустить»."
            if username else _SLOT_TEXT_FALLBACK
        )
        label = _clip(f"Перейти в @{username}" if username else _SLOT_BUTTON_TEXT)
        keyboard = [[{"text": label, "url": url}]]
        try:
            message_id = await self._send(state.chat_id, text, keyboard)
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
                    "access_allowed": s.access_allowed,
                    "shown_marked": s.shown_marked,
                    "tasks": [_task_to_dict(t) for t in s.tasks.values()],
                    "incomplete_ids": sorted(s.incomplete_ids),
                    "paid_done_ids": sorted(s.paid_done_ids),
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
                    completed=bool(block.get("completed")),
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
            except (PastuhAdsError, Exception) as exc:  # noqa: BLE001
                self._report("resume", exc)

    async def aclose(self) -> None:
        """Отменить фоновые задачи (slot TTL, retry, reconcile)."""
        for state in self._states.values():
            for task in (state.slot_task, state.reconcile_task):
                if task is not None and not task.done():
                    task.cancel()
        await asyncio.gather(
            *[t for s in self._states.values()
              for t in (s.slot_task, s.reconcile_task) if t is not None],
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------

    def _expired(self, state: _BlockState) -> bool:
        return (
            state.expires_at is not None
            and state.expires_at <= datetime.now(timezone.utc)
        )

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
