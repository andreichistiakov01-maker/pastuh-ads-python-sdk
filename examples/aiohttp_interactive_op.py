"""Эталон интерактивного OP-блока БЕЗ SDK — голый aiohttp.

Для партнёрских ботов с собственным aiohttp-стеком (без aiogram/SDK):
пример показывает весь серверный контракт Partner API v1 и UX-механику
из HTML-макета — одно OP-сообщение, сокращающаяся клавиатура, TTL-слот
для tokenless-бота, никаких лишних сообщений между заданиями.

Что реализует пример (обязательный минимум контракта):

1. ``/start`` → ``POST /op-list`` с ``capabilities=['callback_cta',
   'interactive_op']`` → ОДНО сообщение с кнопками заданий.
2. ``op_session_id`` ↔ ``message_id`` хранятся ВМЕСТЕ. Новая сессия —
   новое сообщение: message_id прошлой сессии НИКОГДА не редактируется
   для новой (урок инцидента #50085 / SDK 1.2.0).
3. ``/shown`` вызывается только ПОСЛЕ успешного отображения блока
   (sendMessage или editMessageReplyMarkup без ошибки). При временном
   сбое — повтор при следующем рендере, сессия не зависает в created.
4. Задания подписки/заявки/бота с токеном — прямые ``url``-кнопки
   (``https://t.me/...``, без /r/{token} — iOS-safe). Засчитываются
   серверными событиями; кнопка исчезает после ``GET /status`` или
   ``POST /check``.
5. ``paop:open:{task_id}`` (bot_tracked_click) → ``POST
   /tasks/{id}/callback-click`` → ответ ``target_url`` → временное
   слот-сообщение (TTL ~15с). Клик оплачивается РОВНО один раз —
   endpoint идемпотентен (``charged``), повторный callback безопасен.
6. ``paop:reopen:{task_id}`` — повторное открытие оплаченного слота
   БЕЗ финансовых вызовов (ноль запросов к Partner API).
7. ``paop:check`` → ``POST /check`` → ``editMessageReplyMarkup`` —
   клавиатура пересобирается из ``incomplete_tasks``.
8. Все задания выполнены → основной блок редактируется в компактное
   «Доступ открыт» + reopen-CTA (вместо deleteMessage — макет-допущение
   §7: доступ не зависит от удаляемости сообщения).
9. Старое сообщение удалено/не редактируется → безопасный resend
   ОДИН раз, затем /shown.
10. Повторный ``/start`` той же сессии → edit существующего сообщения;
    у пользователя с ``access_allowed`` OP-блок не показывается снова
    без причины.

Состояние (session_state по telegram_user_id) — в проде Redis/file,
здесь dict для читаемости. Polling использован для самодостаточности;
при webhook-интеграции та же логика идёт в хендлеры update.

Запуск:
    BOT_TOKEN=... PASTUH_ADS_API_KEY=opk_... PASTUH_ADS_SOURCE_ID=42 \
    PASTUH_ADS_PLACEMENT_ID=7 python examples/aiohttp_interactive_op.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

import aiohttp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aiohttp_op")

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.environ.get("PASTUH_ADS_API_URL", "https://pastuhads.com/api/partner/v1")
API_KEY = os.environ["PASTUH_ADS_API_KEY"]
SOURCE_ID = int(os.environ["PASTUH_ADS_SOURCE_ID"])
PLACEMENT_ID = int(os.environ["PASTUH_ADS_PLACEMENT_ID"])

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
SLOT_TTL = 15.0

MAIN_TEXT = "Чтобы продолжить, подпишитесь на ресурсы."
DONE_TEXT = "🎉 Все задания выполнены. Доступ открыт!"
SLOT_BUTTON = "Перейти"
CHECK_BUTTON = "✅ Проверить выполнение"

# action_type, открываемые прямой t.me-кнопкой: серверные события
# (chat_member / chat_join_request / BotStartEvent) — единственное
# доказательство, переход сам по себе задание НЕ закрывает.
ONE_TAP_ACTIONS = {
    "subscription_confirmed",
    "join_request_submitted",
    "bot_start_confirmed",
    "bot_new_user_verified",
}
PAID_CALLBACK_ACTIONS = {"bot_tracked_click", "bot_click"}


class OpBlock:
    """Один OP-блок на пользователя: состояние ↔ сессия ↔ message."""

    def __init__(self, tg: aiohttp.ClientSession, api: aiohttp.ClientSession) -> None:
        self.tg = tg
        self.api = api
        # telegram_user_id -> state. Прод: Redis hash с TTL ~lifetime сессии.
        self.states: dict[int, dict[str, Any]] = {}

    # ------------------- Bot API helpers -------------------

    async def send(self, chat_id: int, text: str, rows: list) -> int:
        r = await self.tg.post(f"{TG}/sendMessage", json={
            "chat_id": chat_id, "text": text,
            "reply_markup": {"inline_keyboard": rows},
        })
        data = (await r.json())
        if not data.get("ok"):
            raise RuntimeError(f"sendMessage: {data.get('description')}")
        return data["result"]["message_id"]

    async def edit(self, chat_id: int, message_id: int, text: str, rows: list) -> None:
        r = await self.tg.post(f"{TG}/editMessageText", json={
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "reply_markup": {"inline_keyboard": rows},
        })
        data = await r.json()
        if not data.get("ok"):
            desc = str(data.get("description", "")).lower()
            if "not modified" in desc:
                return  # клавиатура актуальна — считаем показанным
            raise RuntimeError(f"editMessageText: {data.get('description')}")

    async def delete_quiet(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(Exception):
            await self.tg.post(f"{TG}/deleteMessage", json={
                "chat_id": chat_id, "message_id": message_id,
            })

    async def answer_cb(self, cb_id: str, text: str | None = None) -> None:
        with contextlib.suppress(Exception):
            payload: dict[str, Any] = {"callback_query_id": cb_id}
            if text:
                payload["text"] = text
            await self.tg.post(f"{TG}/answerCallbackQuery", json=payload)

    # ------------------- Partner API -------------------

    async def op_list(self, tg_user_id: int) -> dict:
        r = await self.api.post(f"{API_URL}/op-list", json={
            "source_id": SOURCE_ID,
            "placement_id": PLACEMENT_ID,
            "telegram_user_id": str(tg_user_id),
            "user_observation": {},
            "capabilities": ["callback_cta", "interactive_op"],
        })
        data = await r.json()
        return data.get("data", data)

    async def call(self, method: str, path: str, **kw) -> dict:
        r = await self.api.request(method, f"{API_URL}{path}", **kw)
        return (await r.json()).get("data", await r.json())

    # ------------------- Клавиатура -------------------

    def keyboard(self, state: dict) -> list:
        rows: list[list[dict]] = []
        incomplete = set(state["incomplete_ids"])
        for t in state["tasks"]:
            if t["task_id"] not in incomplete:
                continue
            text = t.get("action_button_text") or "Открыть"
            if t["action_type"] in ONE_TAP_ACTIONS and t.get("url"):
                rows.append([{"text": text, "url": t["url"]}])
            elif t["action_type"] in PAID_CALLBACK_ACTIONS:
                if t.get("open_mode") == "callback":
                    rows.append([{"text": text, "callback_data": f"paop:open:{t['task_id']}"}])
                elif t.get("url"):
                    rows.append([{"text": text, "url": t["url"]}])
            elif t.get("url"):
                rows.append([{"text": text, "url": t["url"]}])

        # reopen-CTA оплаченных click-заданий (ноль финансовых вызовов)
        for t in state["tasks"]:
            if t["action_type"] not in PAID_CALLBACK_ACTIONS:
                continue
            if t["task_id"] in incomplete:
                continue
            url = t.get("target_url") or t.get("url")
            if url and url.startswith("https://t.me/"):
                label = f"@{t['username']}" if t.get("username") else "бота"
                rows.append([{"text": f"🔁 Открыть {label}",
                              "callback_data": f"paop:reopen:{t['task_id']}"}])

        if not state["completed"]:
            rows.append([{"text": CHECK_BUTTON, "callback_data": "paop:check"}])
        return rows

    async def render(self, state: dict) -> None:
        """editMessageText основного блока; удалённое сообщение → resend."""
        text = DONE_TEXT if state["completed"] else MAIN_TEXT
        rows = self.keyboard(state)
        try:
            await self.edit(state["chat_id"], state["message_id"], text, rows)
        except Exception as exc:
            lowered = str(exc).lower()
            if "not found" in lowered or "can't be edited" in lowered:
                # Resend ОДИН раз — старое сообщение не воскрешаем.
                state["message_id"] = await self.send(state["chat_id"], text, rows)
            else:
                raise
        await self.ensure_shown(state)

    async def ensure_shown(self, state: dict) -> None:
        """/shown — только после успешного отображения; повтор при сбое."""
        if state["shown_marked"]:
            return
        try:
            await self.call("POST", f"/op-sessions/{state['op_session_id']}/shown")
            state["shown_marked"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("mark_shown failed: %s", exc)  # повтор при след. рендере

    async def send_slot(self, state: dict, task: dict) -> None:
        """TTL-слот с прямой t.me ссылкой — единственное временное сообщение."""
        url = task.get("target_url") or task.get("url")
        if not url or not url.startswith("https://t.me/"):
            return
        username = task.get("username") or ""
        text = (f"Перейдите в @{username} и нажмите «Запустить»."
                if username else "Нажмите кнопку ниже, чтобы перейти:")
        label = f"Перейти в @{username}" if username else SLOT_BUTTON
        old = state.get("slot_msg_id")
        try:
            mid = await self.send(state["chat_id"], text,
                                  [[{"text": label, "url": url}]])
            state["slot_msg_id"] = mid
            asyncio.create_task(self._slot_ttl(state, mid))
        except Exception as exc:  # noqa: BLE001
            log.warning("slot send failed: %s", exc)
        if old:
            await self.delete_quiet(state["chat_id"], old)  # старый слот не висит

    async def _slot_ttl(self, state: dict, mid: int) -> None:
        await asyncio.sleep(SLOT_TTL)
        if state.get("slot_msg_id") == mid:  # ref-match: новый слот не трогаем
            state["slot_msg_id"] = None
            await self.delete_quiet(state["chat_id"], mid)

    # ------------------- События -------------------

    async def on_start(self, tg_user_id: int, chat_id: int) -> None:
        op = await self.op_list(tg_user_id)
        session_id = op["op_session_id"]

        state = self.states.get(tg_user_id)
        if state and state["op_session_id"] != session_id:
            # НОВАЯ сессия — state прошлой сессии сбрасывается полностью:
            # message_id, флаги, слот не протекают (инцидент #50085).
            state = None
        if state is None:
            state = {
                "op_session_id": session_id, "chat_id": chat_id,
                "message_id": None, "shown_marked": False,
                "tasks": op.get("tasks", []),
                "incomplete_ids": {t["task_id"] for t in op.get("tasks", [])},
                "paid_done_ids": set(), "completed": False,
                "access_allowed": bool(op.get("access_allowed")),
                "slot_msg_id": None,
            }
            self.states[tg_user_id] = state
        else:
            self._merge(state, op)

        if state["access_allowed"] and not state["tasks"]:
            return  # законный доступ — повторный OP не показываем

        if state["message_id"] is None:
            text = DONE_TEXT if state["completed"] else MAIN_TEXT
            state["message_id"] = await self.send(
                chat_id, text, self.keyboard(state))
            await self.ensure_shown(state)
        else:
            await self.render(state)

    def _merge(self, state: dict, op: dict) -> None:
        """Монотонный merge: paid_done не воскресает, сервер авторитетен."""
        by_id = {t["task_id"]: t for t in op.get("tasks", [])}
        for tid, task in by_id.items():
            state["tasks"] = [t for t in state["tasks"] if t["task_id"] != tid]
            state["tasks"].append(task)
        state["tasks"].sort(key=lambda t: (t.get("slot", 0), t["task_id"]))
        incomplete = {t["task_id"] for t in (op.get("incomplete_tasks") or [])}
        if not incomplete:
            incomplete = set(by_id) if op.get("tasks") else set()
        state["incomplete_ids"] = incomplete - state["paid_done_ids"]
        state["access_allowed"] = state["access_allowed"] or bool(op.get("access_allowed"))
        state["completed"] = state["access_allowed"] or not state["incomplete_ids"]

    async def on_callback(self, cb: dict) -> None:
        data = cb.get("data") or ""
        if not data.startswith("paop:"):
            return
        tg_user_id = cb["from"]["id"]
        state = self.states.get(tg_user_id)
        if state is None:
            await self.answer_cb(cb["id"], "Сессия заданий не найдена. Отправьте /start заново.")
            return

        if data == "paop:check":
            await self.answer_cb(cb["id"], "Проверяем выполнение…")
            status = await self.call("POST", f"/op-sessions/{state['op_session_id']}/check")
            self._merge_status(state, status)
            await self.render(state)
            return

        kind, _, raw = data[5:].partition(":")
        task_id = int(raw) if raw.isdigit() else 0
        task = next((t for t in state["tasks"] if t["task_id"] == task_id), None)
        if task is None:
            await self.answer_cb(cb["id"], "Задание уже выполнено или не найдено.")
            await self.render(state)
            return

        if kind == "open":
            if task_id not in state["incomplete_ids"]:
                await self.answer_cb(cb["id"], "Задание уже выполнено.")
                return
            await self.answer_cb(cb["id"])
            # Оплачиваемый клик — серверный transition, НЕ локальный.
            resp = await self.call(
                "POST", f"/tasks/{task_id}/callback-click",
                json={"telegram_user_id": str(tg_user_id)})
            if resp.get("target_url"):
                task["target_url"] = resp["target_url"]
            # charged=True/False — клик засчитан ровно один раз.
            state["paid_done_ids"].add(task_id)
            state["incomplete_ids"].discard(task_id)
            if not state["incomplete_ids"]:
                state["completed"] = True
                state["access_allowed"] = True
            await self.send_slot(state, task)
            await self.render(state)
        elif kind == "reopen":
            # Оплаченный destination — без финансового вызова.
            await self.answer_cb(cb["id"])
            await self.send_slot(state, task)

    def _merge_status(self, state: dict, status: dict) -> None:
        state["access_allowed"] = state["access_allowed"] or bool(status.get("access_allowed"))
        incomplete = {t["task_id"] for t in (status.get("incomplete_tasks") or [])}
        state["incomplete_ids"] = incomplete - state["paid_done_ids"]
        for t in status.get("tasks", []):
            for i, ex in enumerate(state["tasks"]):
                if ex["task_id"] == t["task_id"]:
                    state["tasks"][i] = t
        state["completed"] = state["access_allowed"] or not state["incomplete_ids"]


async def main() -> None:
    async with aiohttp.ClientSession() as tg, aiohttp.ClientSession(
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as api:
        block = OpBlock(tg, api)
        offset = 0
        while True:
            r = await tg.get(f"{TG}/getUpdates",
                             params={"offset": offset, "timeout": 30})
            for upd in (await r.json()).get("result", []):
                offset = upd["update_id"] + 1
                try:
                    if (msg := upd.get("message")) and msg.get("text", "").startswith("/start"):
                        await block.on_start(msg["from"]["id"], msg["chat"]["id"])
                    elif cb := upd.get("callback_query"):
                        await block.on_callback(cb)
                except Exception as exc:  # noqa: BLE001
                    log.warning("update %s failed: %s", upd["update_id"], exc)


if __name__ == "__main__":
    asyncio.run(main())
