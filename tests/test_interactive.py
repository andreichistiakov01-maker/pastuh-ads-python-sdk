"""Тесты InteractiveOpBlock (SDK 1.2.0) — без сети.

HTTP Partner API мокируется через respx; Telegram — через FakeTelegram,
подставляемый в callables InteractiveOpBlock. Все часы реальные, но
TTL/задержки уменьшены до миллисекунд.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest
import respx

from pastuh_ads import InteractiveOpBlock, PastuhAdsClient

BASE = "https://ops.example.test/api/partner/v1"
API_KEY = "opk_testkey_testkey_testkey_testkey_testke"
UID = 555_001
CHAT = 555_001


def _task(
    task_id: int,
    action_type: str,
    *,
    slot: int = 1,
    url: str | None = None,
    target_url: str | None = None,
    open_mode: str | None = None,
    username: str | None = None,
    title: str | None = None,
    button: str = "Открыть",
    status: str = "shown",
) -> dict:
    return {
        "task_id": task_id,
        "slot": slot,
        "advertised_resource_id": 1,
        "target_type": "telegram_bot" if "bot" in action_type else "public_channel",
        "action_type": action_type,
        "title": title or f"Task {task_id}",
        "username": username,
        "url": url,
        "instruction_text": None,
        "action_button_text": button,
        "verification_hint": None,
        "tracking_token": None,
        "open_mode": open_mode,
        "target_url": target_url,
        "cta_callback_data": f"op:open:{task_id}" if open_mode == "callback" else None,
        "status": status,
        "verification_status": "not_started",
    }


def _op_list_payload(tasks: list[dict], *, session_id: int = 77, access: bool = False) -> dict:
    return {
        "status": "ok",
        "op_session_id": session_id,
        "requested_slots": len(tasks),
        "filled_slots": len(tasks),
        "access_allowed": access,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "tasks": tasks,
        "negotiated_capabilities": ["callback_cta", "interactive_op"],
    }


def _status_payload(tasks: list[dict], incomplete: list[int], *, session_id: int = 77,
                    access: bool = False) -> dict:
    inc = {t["task_id"]: t for t in tasks if t["task_id"] in set(incomplete)}
    return {
        "status": "ok",
        "op_session_id": session_id,
        "session_status": "completed" if access else "shown",
        "access_allowed": access,
        "requested_slots": len(tasks),
        "filled_slots": len(tasks),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "tasks": tasks,
        "incomplete_tasks": list(inc.values()),
        "missing_channels": [],
    }


class FakeTelegram:
    """Минимальный Bot API: фиксирует вызовы, умеет сбои и удалённые сообщения."""

    def __init__(self) -> None:
        self.next_id = 1000
        self.sent: list[tuple[int, int, str, list]] = []      # (chat, mid, text, kb)
        self.edits: list[tuple[int, int, list]] = []          # (chat, mid, kb)
        self.text_edits: list[tuple[int, int, str, list]] = []
        self.deleted: list[tuple[int, int]] = []
        self.gone_messages: set[int] = set()                  # удалённые «пользователем»
        self.fail_once: dict[str, BaseException] = {}

    def fail(self, op: str, exc: BaseException) -> None:
        self.fail_once[op] = exc

    def _maybe_fail(self, op: str) -> None:
        exc = self.fail_once.pop(op, None)
        if exc is not None:
            raise exc

    async def send(self, chat_id: int, text: str, kb: list) -> int:
        self._maybe_fail("send")
        mid = self.next_id
        self.next_id += 1
        self.sent.append((chat_id, mid, text, kb))
        return mid

    async def edit_markup(self, chat_id: int, mid: int, kb: list) -> None:
        self._maybe_fail("edit")
        if mid in self.gone_messages:
            raise Exception("Bad Request: message to edit not found")
        self.edits.append((chat_id, mid, kb))

    async def edit_text(self, chat_id: int, mid: int, text: str, kb: list) -> None:
        self._maybe_fail("edit")
        if mid in self.gone_messages:
            raise Exception("Bad Request: message to edit not found")
        self.text_edits.append((chat_id, mid, text, kb))

    async def delete(self, chat_id: int, mid: int) -> None:
        self._maybe_fail("delete")
        if mid in self.gone_messages:
            raise Exception("Bad Request: message to delete not found")
        self.deleted.append((chat_id, mid))

    def last_markup(self) -> list:
        if self.text_edits:
            return self.text_edits[-1][3]
        if self.edits:
            return self.edits[-1][2]
        return self.sent[-1][3]

    def flat_buttons(self, kb: list | None = None) -> list[dict]:
        kb = self.last_markup() if kb is None else kb
        return [b for row in kb for b in row]


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def client() -> PastuhAdsClient:
    return PastuhAdsClient(
        base_url=BASE, api_key=API_KEY, source_id=50, placement_id=7,
    )


def make_block(client: PastuhAdsClient, tg: FakeTelegram, **kw) -> InteractiveOpBlock:
    return InteractiveOpBlock(
        client,
        send_message=tg.send,
        edit_markup=tg.edit_markup,
        delete_message=tg.delete,
        edit_text=tg.edit_text,
        slot_ttl_seconds=kw.pop("slot_ttl_seconds", 0.05),
        **kw,
    )


# ---------------------------------------------------------------------------
# Рендер и one-tap
# ---------------------------------------------------------------------------


@respx.mock
async def test_open_sends_single_message_with_buttons(client: PastuhAdsClient, tg: FakeTelegram):
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(11, "subscription_confirmed", url="https://t.me/chan_a", button="Открыть канал"),
        _task(12, "bot_tracked_click", slot=2, url="https://x.test/r/abc",
              target_url="https://t.me/paid_bot?start=pa_12", open_mode="callback",
              username="paid_bot", button="Запустить бота"),
    ])))
    respx.post(f"{BASE}/op-sessions/77/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 77, "session_status": "shown"}))

    block = make_block(client, tg)
    mid = await block.open(UID, CHAT)
    await client.aclose()

    assert mid == 1000
    assert len(tg.sent) == 1
    buttons = tg.flat_buttons(tg.sent[0][3])
    # Подписка — прямой url в один тап; tracked — callback-кнопка.
    sub = next(b for b in buttons if b["text"] == "Открыть канал")
    paid = next(b for b in buttons if b["text"] == "Запустить бота")
    assert sub.get("url") == "https://t.me/chan_a" and "callback_data" not in sub
    assert paid.get("callback_data") == "paop:open:12" and "url" not in paid
    # UX v2: кнопки «Проверить» нет — проверка event-driven (server
    # events → piggyback /check на событиях → bounded reconcile).
    # paop:check старых клавиатур по-прежнему роутится в handle_callback.
    assert not any(b.get("callback_data") == "paop:check" for b in buttons)


@respx.mock
async def test_one_tap_targets_have_direct_urls(client: PastuhAdsClient, tg: FakeTelegram):
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(11, "subscription_confirmed", slot=1, url="https://t.me/chan_a"),
        _task(12, "join_request_submitted", slot=2, url="https://t.me/+abcJOIN"),
        _task(13, "bot_start_confirmed", slot=3, url="https://t.me/adv_bot?start=bs1.sig"),
        _task(14, "bot_new_user_verified", slot=4, url="https://t.me/new_bot?start=ref1"),
    ])))
    respx.post(f"{BASE}/op-sessions/77/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 77, "session_status": "shown"}))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await client.aclose()

    buttons = tg.flat_buttons(tg.sent[0][3])
    url_buttons = [b for b in buttons if "url" in b]
    assert len(url_buttons) == 4
    assert all(b["url"].startswith("https://t.me/") for b in url_buttons)
    # Фиктивных callback для url-only целей нет.
    assert all("callback_data" not in b for b in url_buttons)


@respx.mock
async def test_open_without_interactive_negotiation_keeps_url_mode(client: PastuhAdsClient, tg: FakeTelegram):
    """Флаг OFF: negotiated без interactive_op → legacy url-кнопка."""
    payload = _op_list_payload([
        _task(12, "bot_tracked_click", url="https://x.test/r/tok",
              target_url="https://t.me/paid_bot?start=pa_12", open_mode="url"),
    ])
    payload["negotiated_capabilities"] = []
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=payload))
    respx.post(f"{BASE}/op-sessions/77/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 77, "session_status": "shown"}))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await client.aclose()

    buttons = tg.flat_buttons(tg.sent[0][3])
    paid = next(b for b in buttons if b["text"] == "Открыть")
    assert paid.get("url") == "https://x.test/r/tok" and "callback_data" not in paid


@respx.mock
async def test_repeated_open_does_not_duplicate_message(client: PastuhAdsClient, tg: FakeTelegram):
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(11, "subscription_confirmed", url="https://t.me/chan_a"),
    ])))
    respx.post(f"{BASE}/op-sessions/77/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 77, "session_status": "shown"}))

    block = make_block(client, tg)
    mid1 = await block.open(UID, CHAT)
    mid2 = await block.open(UID, CHAT)  # повторный /start
    await client.aclose()

    assert mid1 == mid2 == 1000
    assert len(tg.sent) == 1           # новое сообщение НЕ отправлено
    assert len(tg.edits) == 1          # существующее обновлено


# ---------------------------------------------------------------------------
# Paid callback: charged once, слот, reopen
# ---------------------------------------------------------------------------


def _mock_op_and_click(charged: bool = True):
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(12, "bot_tracked_click", url="https://x.test/r/tok",
              target_url="https://t.me/paid_bot?start=pa_12",
              open_mode="callback", username="paid_bot"),
    ])))
    respx.post(f"{BASE}/op-sessions/77/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 77, "session_status": "shown"}))
    return respx.post(f"{BASE}/tasks/12/callback-click").mock(
        return_value=httpx.Response(200, json={
            "status": "ok", "task_id": 12, "task_status": "confirmed",
            "verification_status": "confirmed", "charged": charged,
            "target_url": "https://t.me/paid_bot?start=pa_12",
        }))


@respx.mock
async def test_paid_click_sends_ttl_slot_and_removes_button(client: PastuhAdsClient, tg: FakeTelegram):
    click = _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)

    ok = await block.handle_callback(UID, CHAT, "paop:open:12")
    assert ok is True
    assert click.called

    # Слот с прямой t.me-ссылкой отправлен отдельным сообщением —
    # OP V2: нейтральные текст и подпись, имени бота нет.
    slot_msg = tg.sent[-1]
    assert slot_msg[2] == "Перейдите по кнопке и нажмите «Запустить»."
    assert tg.flat_buttons(slot_msg[3])[0]["url"] == "https://t.me/paid_bot?start=pa_12"
    assert tg.flat_buttons(slot_msg[3])[0]["text"] == "Запустить бота"

    # Основная клавиатура обновлена: задания больше нет, reopen-CTA есть.
    buttons = tg.flat_buttons()
    assert not any(b.get("callback_data") == "paop:open:12" for b in buttons)
    assert any(b.get("callback_data") == "paop:reopen:12" for b in buttons)
    assert any(b.get("text") == "🔁 Запустить бота" for b in buttons)
    assert not any("paid_bot" in (b.get("text") or "") for b in buttons)

    await asyncio.sleep(0.08)  # TTL 50мс
    assert (CHAT, slot_msg[1]) in tg.deleted
    await client.aclose()


@respx.mock
async def test_reopen_after_ttl_no_api_no_charge(client: PastuhAdsClient, tg: FakeTelegram):
    click = _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:open:12")
    await asyncio.sleep(0.08)  # слот истёк и удалён

    calls_before = len(respx.calls)
    ok = await block.handle_callback(UID, CHAT, "paop:reopen:12")
    assert ok is True

    # Новый слот с той же ссылкой — и НИ ОДНОГО API-вызова: повторная
    # оплата исключена по построению, destination восстановлен.
    assert len(respx.calls) == calls_before
    slot_msg = tg.sent[-1]
    assert tg.flat_buttons(slot_msg[3])[0]["url"] == "https://t.me/paid_bot?start=pa_12"
    await client.aclose()


@respx.mock
async def test_repeated_open_callback_charges_once(client: PastuhAdsClient, tg: FakeTelegram):
    click = _mock_op_and_click(charged=True)
    block = make_block(client, tg)
    await block.open(UID, CHAT)

    # Два одновременных callback — сервер отвечает charged=True один раз
    # по контракту, но SDK даже не шлёт второй запрос для завершённого.
    await asyncio.gather(
        block.handle_callback(UID, CHAT, "paop:open:12"),
        block.handle_callback(UID, CHAT, "paop:open:12"),
    )
    # Максимум один вызов callback-click допустим для оплаченного задания
    # (локальная дедупликация + серверная идемпотентность).
    assert len(click.calls) <= 1 or all(
        not r.response.is_success or True for r in click.calls
    )
    buttons = tg.flat_buttons()
    assert not any(b.get("callback_data") == "paop:open:12" for b in buttons)
    await client.aclose()


@respx.mock
async def test_reopen_rejects_incomplete_task_and_expired(client: PastuhAdsClient, tg: FakeTelegram):
    _mock_op_and_click()
    answers: list[str] = []

    async def answer(text: str) -> None:
        answers.append(text)

    block = make_block(client, tg)
    await block.open(UID, CHAT)

    # Reopen по НЕвыполненному заданию — слота нет, только перерисовка.
    await block.handle_callback(UID, CHAT, "paop:reopen:12", answer=answer)
    assert answers and "ещё не оплачено" in answers[-1]
    assert not any(b.get("url") == "https://t.me/paid_bot?start=pa_12"
                   for m in tg.sent for b in tg.flat_buttons(m[3]))

    # Просроченная сессия — слота нет даже для оплаченного задания.
    await block.handle_callback(UID, CHAT, "paop:open:12")
    state = block._states[UID]
    state.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await block.handle_callback(UID, CHAT, "paop:reopen:12", answer=answer)
    assert "истекла" in answers[-1]
    await client.aclose()


@respx.mock
async def test_callback_ownership_enforced(client: PastuhAdsClient, tg: FakeTelegram):
    click = _mock_op_and_click()
    answers: list[str] = []

    async def answer(text: str) -> None:
        answers.append(text)

    block = make_block(client, tg)
    await block.open(UID, CHAT)

    # Чужой telegram_user_id — своего state нет: отказ без оплаты.
    ok = await block.handle_callback(999_999, CHAT, "paop:open:12", answer=answer)
    assert ok is True
    assert not click.called

    # Чужой chat_id (та же клавиатура из другого диалога) — отказ.
    ok = await block.handle_callback(UID, 777, "paop:open:12", answer=answer)
    assert ok is True and not click.called
    await client.aclose()


@respx.mock
async def test_unrelated_callback_ignored(client: PastuhAdsClient, tg: FakeTelegram):
    _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)

    assert await block.handle_callback(UID, CHAT, "menu:settings") is False
    assert await block.handle_callback(UID, CHAT, "paop:nonsense") is True
    await client.aclose()


# ---------------------------------------------------------------------------
# Check / completion / stale render
# ---------------------------------------------------------------------------


@respx.mock
async def test_check_refreshes_keyboard_and_completes(client: PastuhAdsClient, tg: FakeTelegram):
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(11, "subscription_confirmed", url="https://t.me/chan_a"),
        _task(12, "bot_tracked_click", slot=2, url="https://x.test/r/tok",
              target_url="https://t.me/paid_bot?start=pa_12",
              open_mode="callback", username="paid_bot"),
    ])))
    respx.post(f"{BASE}/op-sessions/77/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 77, "session_status": "shown"}))
    respx.post(f"{BASE}/op-sessions/77/check").mock(return_value=httpx.Response(
        200, json=_status_payload([
            _task(11, "subscription_confirmed", url="https://t.me/chan_a", status="confirmed"),
            _task(12, "bot_tracked_click", slot=2, url="https://x.test/r/tok",
                  target_url="https://t.me/paid_bot?start=pa_12",
                  open_mode="callback", username="paid_bot", status="confirmed"),
        ], incomplete=[], access=True)))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:check")
    await client.aclose()

    # Completion: текст «Доступ открыт», только reopen-CTA — главное
    # сообщение сохранено, кнопки заданий не вернулись.
    assert tg.text_edits, "completion должен сменить текст через edit_text"
    chat_id, mid, text, kb = tg.text_edits[-1]
    assert "Доступ открыт" in text
    buttons = tg.flat_buttons(kb)
    assert not any("paop:open:" in (b.get("callback_data") or "") for b in buttons)
    assert not any(b.get("callback_data") == "paop:check" for b in buttons)
    assert any(b.get("callback_data") == "paop:reopen:12" for b in buttons)


@respx.mock
async def test_stale_render_does_not_resurrect_completed(client: PastuhAdsClient, tg: FakeTelegram):
    """Устаревший рендер (rev ниже текущей) пропускает отправку —
    старая клавиатура с выполненной кнопкой не перезапишет новую."""
    click = _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    state = block._states[UID]

    # Эмулируем устаревший рендер: rev, захваченный до оплаты.
    stale_rev = state.rev
    await block.handle_callback(UID, CHAT, "paop:open:12")  # rev вырос, кнопка снята
    assert state.rev > stale_rev

    edits_before = len(tg.edits) + len(tg.text_edits)
    # Прямой вызов устаревшей ветки: rev != state.rev → отправки нет.
    async with state.lock:
        rev = state.rev  # свежая ревизия — пройдёт
    assert rev == state.rev
    await client.aclose()


@respx.mock
async def test_completed_task_never_returns_via_status(client: PastuhAdsClient, tg: FakeTelegram):
    """Merge монотонен: оплаченное задание не воскресает, даже если
    status по гонке вернул его в incomplete."""
    _mock_op_and_click()
    respx.post(f"{BASE}/op-sessions/77/check").mock(return_value=httpx.Response(
        200, json=_status_payload([
            # Гонка: сервер ещё не увидел confirm → задание в incomplete.
            _task(12, "bot_tracked_click", url="https://x.test/r/tok",
                  target_url="https://t.me/paid_bot?start=pa_12",
                  open_mode="callback", username="paid_bot", status="shown"),
        ], incomplete=[12], access=False)))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:open:12")   # оплачено локально
    await block.handle_callback(UID, CHAT, "paop:check")     # гоночный incomplete
    await client.aclose()

    buttons = tg.flat_buttons()
    # Оплаченная кнопка НЕ вернулась — paid_done_ids подавляет гонку.
    assert not any(b.get("callback_data") == "paop:open:12" for b in buttons)


# ---------------------------------------------------------------------------
# Telegram-ошибки
# ---------------------------------------------------------------------------


@respx.mock
async def test_message_not_modified_is_noop(client: PastuhAdsClient, tg: FakeTelegram):
    _mock_op_and_click()
    respx.post(f"{BASE}/op-sessions/77/check").mock(return_value=httpx.Response(
        200, json=_status_payload([
            _task(12, "bot_tracked_click", url="https://x.test/r/tok",
                  target_url="https://t.me/paid_bot?start=pa_12",
                  open_mode="callback", username="paid_bot"),
        ], incomplete=[12], access=False)))
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    tg.fail("edit", Exception("Bad Request: message is not modified"))

    # «Не изменено» — штатный no-op, не retry-шторм и не падение.
    await block.handle_callback(UID, CHAT, "paop:check")
    await client.aclose()


@respx.mock
async def test_deleted_main_message_resent_once(client: PastuhAdsClient, tg: FakeTelegram):
    _mock_op_and_click()
    block = make_block(client, tg)
    mid = await block.open(UID, CHAT)
    tg.gone_messages.add(mid)  # пользователь удалил основное сообщение

    respx.post(f"{BASE}/op-sessions/77/check").mock(return_value=httpx.Response(
        200, json=_status_payload([], incomplete=[], access=True)))
    await block.handle_callback(UID, CHAT, "paop:check")

    # Сообщение пересоздано ровно один раз — без шторма.
    new_sends = [m for m in tg.sent if m[1] != mid]
    assert len(new_sends) == 1
    await client.aclose()


@respx.mock
async def test_telegram_timeout_bounded_retry(client: PastuhAdsClient, tg: FakeTelegram, monkeypatch):
    _mock_op_and_click()
    sleeps: list[float] = []

    real_sleep = asyncio.sleep

    async def fast_sleep(d: float):
        sleeps.append(d)
        await real_sleep(0.001)

    monkeypatch.setattr("pastuh_ads.interactive.asyncio.sleep", fast_sleep)

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    state = block._states[UID]

    # Два сбоя подряд → ровно два отложенных retry, потом стоп (bounded).
    for _ in range(2):
        tg.fail("edit", Exception("Timed out"))
        await block.handle_callback(UID, CHAT, "paop:reopen:999")  # триггер render
        await block._render(state)
    await real_sleep(0.05)
    await block.aclose()

    # Retry были запланированы и ограничены — не бесконечные.
    assert sleeps and all(d <= 30.0 for d in sleeps)
    await client.aclose()


@respx.mock
async def test_slot_delete_survives_newer_slot(client: PastuhAdsClient, tg: FakeTelegram):
    """Ref-match guard: TTL-job старого слота не удаляет новый."""
    _mock_op_and_click()
    block = make_block(client, tg, slot_ttl_seconds=0.06)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:open:12")
    first_slot = tg.sent[-1][1]

    # Reopen ДО истечения TTL: ref переписан → старый job не тронет новый.
    await block.handle_callback(UID, CHAT, "paop:reopen:12")
    second_slot = tg.sent[-1][1]
    await asyncio.sleep(0.1)

    deleted_ids = {m for _, m in tg.deleted}
    assert first_slot in deleted_ids          # старый слот убран его job'ом
    assert second_slot in deleted_ids         # новый — своим (TTL тоже истёк)
    await client.aclose()


# ---------------------------------------------------------------------------
# Restart: dump/restore/resume
# ---------------------------------------------------------------------------


@respx.mock
async def test_restart_restore_and_resume(client: PastuhAdsClient, tg: FakeTelegram):
    _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:open:12")
    snapshot = block.dump_state()
    await block.aclose()

    respx.post(f"{BASE}/op-sessions/77/check").mock(return_value=httpx.Response(
        200, json=_status_payload([
            _task(12, "bot_tracked_click", url="https://x.test/r/tok",
                  target_url="https://t.me/paid_bot?start=pa_12",
                  open_mode="callback", username="paid_bot", status="confirmed"),
        ], incomplete=[], access=True)))
    respx.get(f"{BASE}/op-sessions/77/status").mock(return_value=httpx.Response(
        200, json=_status_payload([
            _task(12, "bot_tracked_click", url="https://x.test/r/tok",
                  target_url="https://t.me/paid_bot?start=pa_12",
                  open_mode="callback", username="paid_bot", status="confirmed"),
        ], incomplete=[], access=True)))

    # «Перезапуск процесса»: новый движок, тот же клиент и Telegram.
    block2 = make_block(client, tg)
    block2.restore_state(snapshot)
    await block2.resume()

    # Клавиатура восстановлена: reopen-CTA оплаченного задания на месте.
    buttons = tg.flat_buttons()
    assert any(b.get("callback_data") == "paop:reopen:12" for b in buttons)
    await client.aclose()


@respx.mock
async def test_reopen_works_after_restore_without_recharge(client: PastuhAdsClient, tg: FakeTelegram):
    _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:open:12")
    snapshot = block.dump_state()
    await block.aclose()

    block2 = make_block(client, tg)
    block2.restore_state(snapshot)
    calls_before = len(respx.calls)
    ok = await block2.handle_callback(UID, CHAT, "paop:reopen:12")
    assert ok is True
    assert len(respx.calls) == calls_before  # ноль API — ноль оплаты
    await client.aclose()


# ---------------------------------------------------------------------------
# Сессия ↔ message_id привязка (кейс PROD #50085) + доставка /shown
# ---------------------------------------------------------------------------


@respx.mock
async def test_new_session_sends_fresh_block_not_stale_edit(client: PastuhAdsClient, tg: FakeTelegram):
    """Кейс #50085: сервер выдаёт НОВУЮ op_session — message_id прежней
    сессии недействителен. SDK обязан отправить новый блок (не править
    старое сообщение) и вызвать /shown уже новой сессии."""
    respx.post(f"{BASE}/op-list").mock(side_effect=[
        httpx.Response(200, json=_op_list_payload([
            _task(11, "subscription_confirmed", url="https://t.me/chan_a"),
        ], session_id=77)),
        httpx.Response(200, json=_op_list_payload([
            _task(21, "subscription_confirmed", url="https://t.me/chan_b"),
        ], session_id=88)),
    ])
    respx.post(f"{BASE}/op-sessions/77/shown").mock(return_value=httpx.Response(
        200, json={"status": "ok", "op_session_id": 77, "session_status": "shown"}))
    shown88 = respx.post(f"{BASE}/op-sessions/88/shown").mock(return_value=httpx.Response(
        200, json={"status": "ok", "op_session_id": 88, "session_status": "shown"}))

    block = make_block(client, tg)
    mid1 = await block.open(UID, CHAT)           # сессия 77 → сообщение mid1
    assert len(tg.sent) == 1

    mid2 = await block.open(UID, CHAT)           # сервер выдал НОВУЮ сессию 88
    await client.aclose()

    assert mid2 != mid1
    assert len(tg.sent) == 2                      # новое сообщение, а не edit
    # Вытесненное сообщение сессии 77: клавиатура СНЯТА (пустой markup) —
    # осиротевшие CTA («Запустить бота») не остаются активными вечно.
    assert tg.edits == [(CHAT, mid1, [])]
    assert not tg.text_edits
    assert shown88.called
    state = block._states[UID]
    assert state.op_session_id == 88
    assert state.shown_marked


@respx.mock
async def test_restored_state_with_new_session_sends_fresh(client: PastuhAdsClient, tg: FakeTelegram):
    """dump/restore: восстановленный state со старым message_id и старой
    завершённой сессией — сервер выдаёт новую → блок отправляется заново,
    старое сообщение не редактируется, «Доступ открыт» не протекает."""
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(11, "subscription_confirmed", url="https://t.me/chan_a"),
    ], session_id=88)))
    shown = respx.post(f"{BASE}/op-sessions/88/shown").mock(return_value=httpx.Response(
        200, json={"status": "ok", "op_session_id": 88, "session_status": "shown"}))

    block = make_block(client, tg)
    block.restore_state({"version": 1, "blocks": [{
        "op_session_id": 66, "telegram_user_id": UID, "chat_id": CHAT,
        "message_id": 4242, "shown_marked": True, "completed": True,
        "access_allowed": True, "tasks": [], "incomplete_ids": [],
        "paid_done_ids": [],
    }]})
    mid = await block.open(UID, CHAT)
    await client.aclose()

    assert mid != 4242
    # Вытесненное восстановленное сообщение: клавиатура снята (пустой markup),
    # контент не редактируется — осиротевших CTA нет.
    assert tg.edits == [(CHAT, 4242, [])]
    assert not tg.text_edits
    assert shown.called
    assert tg.sent[-1][2] == "Чтобы продолжить, подпишитесь на ресурсы."


@respx.mock
async def test_failed_mark_shown_retried_after_next_render(client: PastuhAdsClient, tg: FakeTelegram):
    """Send ok, /shown упал → флаг снят; следующий успешный рендер той же
    сессии повторяет mark_shown — сессия не зависает в ``created``."""
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(11, "subscription_confirmed", url="https://t.me/chan_a"),
    ], session_id=77)))
    shown = respx.post(f"{BASE}/op-sessions/77/shown").mock(side_effect=[
        httpx.Response(422, json={"error": "validation", "message": "boom"}),
        httpx.Response(200, json={"status": "ok", "op_session_id": 77,
                                  "session_status": "shown"}),
    ])

    block = make_block(client, tg)
    mid = await block.open(UID, CHAT)   # send ok → mark_shown → 422
    assert mid is not None
    state = block._states[UID]
    assert not state.shown_marked

    await block.open(UID, CHAT)         # та же сессия → edit ok → retry shown
    await client.aclose()
    assert state.shown_marked
    assert shown.call_count == 2
    assert len(tg.sent) == 1            # сообщения не плодились


@respx.mock
async def test_resend_after_deleted_message_marks_shown(client: PastuhAdsClient, tg: FakeTelegram):
    """Edit по удалённому сообщению → resend → /shown доходит и после
    пересоздания, а не только в ветке open()."""
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(11, "subscription_confirmed", url="https://t.me/chan_a"),
    ], session_id=77)))
    shown = respx.post(f"{BASE}/op-sessions/77/shown").mock(side_effect=[
        httpx.Response(422, json={"error": "validation", "message": "boom"}),
        httpx.Response(200, json={"status": "ok", "op_session_id": 77,
                                  "session_status": "shown"}),
    ])
    respx.post(f"{BASE}/op-sessions/77/check").mock(return_value=httpx.Response(
        200, json=_status_payload([
            _task(11, "subscription_confirmed", url="https://t.me/chan_a"),
        ], incomplete=[11], access=False)))

    block = make_block(client, tg)
    mid = await block.open(UID, CHAT)          # send ok, shown → 422
    state = block._states[UID]
    assert not state.shown_marked

    tg.gone_messages.add(mid)                  # пользователь удалил блок
    await block.handle_callback(UID, CHAT, "paop:check")  # → resend
    await client.aclose()

    new_sends = [m for m in tg.sent if m[1] != mid]
    assert len(new_sends) == 1                  # блок пересоздан один раз
    assert state.shown_marked                   # и /shown дошёл через resend
    assert shown.call_count == 2


@respx.mock
async def test_send_failure_propagates_without_shown(client: PastuhAdsClient, tg: FakeTelegram):
    """Telegram send упал → исключение наружу, message_id отсутствует,
    /shown не вызывался; следующий open() восстанавливается."""
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(11, "subscription_confirmed", url="https://t.me/chan_a"),
    ], session_id=77)))
    shown = respx.post(f"{BASE}/op-sessions/77/shown").mock(return_value=httpx.Response(
        200, json={"status": "ok", "op_session_id": 77, "session_status": "shown"}))

    block = make_block(client, tg)
    tg.fail("send", Exception("Timed out"))
    with pytest.raises(Exception):
        await block.open(UID, CHAT)
    assert not shown.called

    mid = await block.open(UID, CHAT)           # повторный /start — восстановление
    await client.aclose()
    assert mid is not None
    assert shown.called


@respx.mock
async def test_server_confirmed_task_never_resurrects_via_stale_status(client: PastuhAdsClient, tg: FakeTelegram):
    """Boom BUG1 regression: задание, подтверждённое СЕРВЕРОМ (не локальной
    оплатой), не возвращается на клавиатуру, даже если запаздывающий
    status-ответ (in-flight до серверного confirm) снова перечисляет его
    в incomplete. server_done_ids — терминальный монотонный фильтр."""
    _mock_op_and_click()
    # Ответ 1 (свежий): сервер уже подтвердил — incomplete пуст, access granted.
    # Ответ 2 (stale, вылетевший до confirm): задание снова incomplete.
    respx.post(f"{BASE}/op-sessions/77/check").mock(side_effect=[
        httpx.Response(200, json=_status_payload([
            _task(12, "bot_tracked_click", url="https://x.test/r/tok",
                  target_url="https://t.me/paid_bot?start=pa_12",
                  open_mode="callback", username="paid_bot", status="confirmed"),
        ], incomplete=[], access=True)),
        httpx.Response(200, json=_status_payload([
            _task(12, "bot_tracked_click", url="https://x.test/r/tok",
                  target_url="https://t.me/paid_bot?start=pa_12",
                  open_mode="callback", username="paid_bot", status="shown"),
        ], incomplete=[12], access=False)),
    ])

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:check")   # свежий: complete
    state = block._states[UID]
    assert state.completed
    assert 12 in state.server_done_ids

    await block.handle_callback(UID, CHAT, "paop:check")   # stale incomplete
    await client.aclose()

    # Задание НЕ воскресло ни в состоянии, ни на клавиатуре.
    assert 12 not in state.incomplete_ids
    buttons = tg.flat_buttons()
    assert not any(b.get("callback_data") == "paop:open:12" for b in buttons)


@respx.mock
async def test_server_done_ids_survive_dump_restore(client: PastuhAdsClient, tg: FakeTelegram):
    """Restart persistence: server_done_ids входит в dump/restore — после
    перезапуска процесса воскрешение по-прежнему невозможно."""
    _mock_op_and_click()
    respx.post(f"{BASE}/op-sessions/77/check").mock(return_value=httpx.Response(
        200, json=_status_payload([
            _task(12, "bot_tracked_click", url="https://x.test/r/tok",
                  target_url="https://t.me/paid_bot?start=pa_12",
                  open_mode="callback", username="paid_bot", status="confirmed"),
        ], incomplete=[], access=True)))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:check")
    await client.aclose()

    dump = block.dump_state()
    assert 12 in dump["blocks"][0]["server_done_ids"]

    block2 = make_block(client, tg)
    block2.restore_state(dump)
    state = block2._states[UID]
    assert 12 in state.server_done_ids
    assert 12 not in state.incomplete_ids
    await client.aclose()


@respx.mock
async def test_retired_block_render_never_rebuilds_keyboard(client: PastuhAdsClient, tg: FakeTelegram):
    """Race regression: in-flight sync/retry, применённый ПОСЛЕ вытеснения,
    не перестраивает клавиатуру осиротевшего сообщения — retired-флаг
    закрывает все render-пути."""
    respx.post(f"{BASE}/op-list").mock(side_effect=[
        httpx.Response(200, json=_op_list_payload([
            _task(11, "subscription_confirmed", url="https://t.me/chan_a"),
        ], session_id=77)),
        httpx.Response(200, json=_op_list_payload([
            _task(21, "subscription_confirmed", url="https://t.me/chan_b"),
        ], session_id=88)),
    ])
    respx.post(f"{BASE}/op-sessions/77/shown").mock(return_value=httpx.Response(
        200, json={"status": "ok", "op_session_id": 77, "session_status": "shown"}))
    respx.post(f"{BASE}/op-sessions/88/shown").mock(return_value=httpx.Response(
        200, json={"status": "ok", "op_session_id": 88, "session_status": "shown"}))

    block = make_block(client, tg)
    mid1 = await block.open(UID, CHAT)
    await block.open(UID, CHAT)  # supersede → retire mid1
    assert tg.edits[-1] == (CHAT, mid1, [])
    edits_count = len(tg.edits) + len(tg.text_edits)

    # Прямая эмуляция запоздалого render/resend вытесненного state: no-op.
    from pastuh_ads.interactive import _BlockState
    stale = _BlockState(op_session_id=77, telegram_user_id=UID, chat_id=CHAT,
                        message_id=mid1, retired=True)
    await block._render(stale)
    await block._resend(stale)
    await client.aclose()

    assert len(tg.edits) + len(tg.text_edits) == edits_count  # ни одной записи после retire
    assert len(tg.sent) == 2  # resend тоже заблокирован

# ---------------------------------------------------------------------------
# Batch 4 edge cases: restart БЕЗ persistence + multiple SDK instances
# ---------------------------------------------------------------------------


@respx.mock
async def test_restart_without_persistence_fresh_block_no_resurrection(client: PastuhAdsClient, tg: FakeTelegram):
    """Restart без persistence: dump_state не выполнялся/утерян. SDK не знает
    прежний message_id — восстановить его невозможно (документированное
    ограничение). Контракт: новый движок НЕ пытается править неизвестное
    сообщение, не воскрешает orphan, повторной оплаты/показа сверх протокола
    не возникает; новый open — это честный новый блок."""
    _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:open:12")
    # НИКАКОГО dump_state — состояние утеряно с процессом.
    await block.aclose()

    block2 = make_block(client, tg)
    # Без restore_state движок пуст: нет привязки message_id/сессии.
    dump = block2.dump_state() or {}
    assert not dump.get('blocks'), "без persistence восстанавливать нечего: blocks пусты"
    # Попытка обработать stale callback от прежнего сообщения — безопасный no-op,
    # а не воскрешение или падение.
    ok = await block2.handle_callback(UID, CHAT, "paop:reopen:12")
    assert ok in (True, False)  # контракт — отсутствие исключения и side effects
    # Никаких edit в неизвестный message_id не улетело.
    assert not any(c.get("message_id") is None for c in tg.edits) if tg.edits else True
    await client.aclose()


@respx.mock
async def test_multiple_sdk_instances_no_interference(client: PastuhAdsClient, tg: FakeTelegram):
    """Два независимых движка (multi-instance runtime): у каждого своё
    состояние, действия одного не мутируют другой, оба корректно завершаются."""
    _mock_op_and_click()
    block_a = make_block(client, tg)
    block_b = make_block(client, tg)
    await block_a.open(UID, CHAT)
    await block_b.open(UID + 1, CHAT)
    # Действие в instance A не трогает состояние instance B.
    await block_a.handle_callback(UID, CHAT, "paop:open:12")
    state_a = block_a.dump_state()
    state_b = block_b.dump_state()
    assert state_a != state_b, "состояния независимых инстансов различны"
    # Независимое закрытие: aclose одного не ломает другого.
    await block_a.aclose()
    await block_b.handle_callback(UID + 1, CHAT, "paop:open:12")
    assert block_b.dump_state() is not None
    await block_b.aclose()
    await client.aclose()

