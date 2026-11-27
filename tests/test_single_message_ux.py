"""Single-message UX (SDK 1.3.4) — контракт без slot-сообщений и reopen-строк.

Целевой UX клиента:
- одно интерактивное OP-сообщение на сессию;
- выполненные кнопки исчезают автоматически;
- никаких дополнительных slot-сообщений («Перейдите по кнопке…»);
- никаких reopen-строк («🔁 Запустить бота»);
- прямое открытие бота через answerCallbackQuery(url);
- fallback — прямая URL-кнопка В ТОМ ЖЕ сообщении, если url-answer отклонён.
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest
import respx

from pastuh_ads import InteractiveOpBlock, PastuhAdsClient
from test_interactive import (
    BASE, CHAT, UID, FakeTelegram, _mock_op_and_click, _op_list_payload,
    _status_payload, _task,
)


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def client() -> PastuhAdsClient:
    return PastuhAdsClient(
        base_url=BASE, api_key="opk_testkey_testkey_testkey_testkey_testke",
        source_id=50, placement_id=7,
    )


def make_block(client, tg, **kw) -> InteractiveOpBlock:
    return InteractiveOpBlock(
        client,
        send_message=tg.send,
        edit_markup=tg.edit_markup,
        delete_message=tg.delete,
        edit_text=tg.edit_text,
        slot_ttl_seconds=kw.pop("slot_ttl_seconds", 0.05),
        **kw,
    )


class UrlAnswer:
    """answerCallback с поддержкой url — фиксирует вызовы."""

    def __init__(self, *, fail_url: bool = False):
        self.calls: list[dict] = []
        self.fail_url = fail_url

    async def __call__(self, text: str = "", url: str | None = None, **kw) -> None:
        if url is not None and self.fail_url:
            raise Exception("Bad Request: URL_INVALID")
        self.calls.append({"text": text, "url": url})


# ---------------------------------------------------------------------------
# A. Отсутствие slot-сообщений и reopen-строк
# ---------------------------------------------------------------------------

@respx.mock
async def test_paid_click_no_slot_message_no_reopen_rows(client, tg):
    """Скриншот-контракт: после тапа НЕТ отдельного сообщения
    «Перейдите по кнопке и нажмите Запустить» и НЕТ reopen-строк
    «🔁 Запустить бота» в основной клавиатуре."""
    click = _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    sent_before = len(tg.sent)

    answer = UrlAnswer()
    ok = await block.handle_callback(UID, CHAT, "paop:open:12", answer=answer)
    assert ok and click.called
    await asyncio.sleep(0.05)

    # 1. Ни одного НОВОГО сообщения — слот не отправлен.
    assert len(tg.sent) == sent_before, "slot-сообщение не должно отправляться"
    assert all("Перейдите по кнопке" not in s[2] for s in tg.sent)

    # 2. Клавиатура основного сообщения: ни reopen-строк, ни задания 12.
    buttons = tg.flat_buttons()
    assert not any("reopen" in (b.get("callback_data") or "") for b in buttons)
    assert not any(b.get("text") == "🔁 Запустить бота" for b in buttons)
    assert not any("open:12" in (b.get("callback_data") or "") for b in buttons)
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_answer_receives_tme_url(client, tg):
    """answerCallbackQuery вызывается с url=target_url — прямое открытие."""
    _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)

    answer = UrlAnswer()
    await block.handle_callback(UID, CHAT, "paop:open:12", answer=answer)
    await asyncio.sleep(0.05)

    url_calls = [c for c in answer.calls if c.get("url")]
    assert url_calls, "answer должен быть вызван с url"
    assert url_calls[0]["url"] == "https://t.me/paid_bot?start=pa_12"
    assert url_calls[0]["url"].startswith("https://t.me/")
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_screenshot_scenario_two_bots_one_channel(client, tg):
    """Точное воспроизведение скриншота клиента: 2 bot_tracked_click +
    1 subscription. После выполнения обоих ботов в клавиатуре остаётся
    ТОЛЬКО «Открыть канал» — никаких 🔁-строк и никаких слотов."""
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(11, "subscription_confirmed", slot=1, url="https://t.me/chan_a", button="Открыть канал"),
        _task(12, "bot_tracked_click", slot=2, url="https://x.test/r/t1",
              target_url="https://t.me/bot_one?start=p1", open_mode="callback", button="Запустить бота"),
        _task(13, "bot_tracked_click", slot=3, url="https://x.test/r/t2",
              target_url="https://t.me/bot_two?start=p2", open_mode="callback", button="Запустить бота"),
    ])))
    respx.post(f"{BASE}/op-sessions/77/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 77}))
    for tid, bot in ((12, "bot_one"), (13, "bot_two")):
        respx.post(f"{BASE}/tasks/{tid}/callback-click").mock(
            return_value=httpx.Response(200, json={
                "status": "ok", "task_id": tid, "task_status": "confirmed",
                "verification_status": "confirmed", "charged": True,
                "target_url": f"https://t.me/{bot}?start=p{tid}",
            }))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    initial_buttons = tg.flat_buttons()
    assert len(initial_buttons) == 3
    sent_before = len(tg.sent)

    answer = UrlAnswer()
    await block.handle_callback(UID, CHAT, "paop:open:12", answer=answer)
    await asyncio.sleep(0.05)
    # После первого выполнения: 2 кнопки (channel + bot13), ноль слотов.
    buttons = tg.flat_buttons()
    assert len(buttons) == 2, f"ожидалось 2 кнопки, получено {len(buttons)}: {buttons}"
    assert not any("🔁" in b.get("text", "") for b in buttons)

    await block.handle_callback(UID, CHAT, "paop:open:13", answer=answer)
    await asyncio.sleep(0.05)
    # После второго: только «Открыть канал». Ноль новых сообщений.
    buttons = tg.flat_buttons()
    assert len(buttons) == 1
    assert buttons[0].get("url") == "https://t.me/chan_a"
    assert len(tg.sent) == sent_before, "ни одного slot-сообщения за весь сценарий"
    await block.aclose()
    await client.aclose()


# ---------------------------------------------------------------------------
# B. Fallback: url-answer отклонён / не поддержан
# ---------------------------------------------------------------------------

@respx.mock
async def test_url_answer_rejected_fallback_row_same_message(client, tg):
    """Telegram отклонил url → прямая URL-кнопка в ТОМ ЖЕ сообщении
    (не slot-сообщением). Settlement не повторяется."""
    click = _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    sent_before = len(tg.sent)

    answer = UrlAnswer(fail_url=True)
    await block.handle_callback(UID, CHAT, "paop:open:12", answer=answer)
    await asyncio.sleep(0.05)

    assert len(tg.sent) == sent_before, "fallback — НЕ slot-сообщение"
    buttons = tg.flat_buttons()
    fallback = [b for b in buttons if b.get("url") == "https://t.me/paid_bot?start=pa_12"]
    assert fallback, "прямая URL-кнопка в основном сообщении"
    assert not any("reopen" in (b.get("callback_data") or "") for b in buttons)
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_textonly_answer_callable_fallback(client, tg):
    """Старая интеграция Boom: answer принимает только text —
    TypeError перехватывается, включается fallback, механика не ломается."""
    _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)

    async def text_only_answer(text: str) -> None:
        pass

    ok = await block.handle_callback(UID, CHAT, "paop:open:12", answer=text_only_answer)
    assert ok is True
    await asyncio.sleep(0.05)
    buttons = tg.flat_buttons()
    assert any(b.get("url") == "https://t.me/paid_bot?start=pa_12" for b in buttons)
    await block.aclose()
    await client.aclose()


# ---------------------------------------------------------------------------
# C. Diagnostic timing hook
# ---------------------------------------------------------------------------

@respx.mock
async def test_diagnostic_hook_emits_latency_chain(client, tg):
    """Hook получает коррелированную цепочку монотонных событий:
    callback.received → open.api_start → open.api_end → answer_url.ok →
    render.start/end — Boom получает latency breakdown из коробки."""
    _mock_op_and_click()
    events: list[dict] = []
    block = make_block(client, tg, diagnostic_hook=events.append)
    await block.open(UID, CHAT)

    answer = UrlAnswer()
    await block.handle_callback(UID, CHAT, "paop:open:12", answer=answer)
    await asyncio.sleep(0.05)

    names = [e["event"] for e in events]
    assert "callback.received" in names
    assert "open.api_start" in names and "open.api_end" in names
    assert "answer_url.ok" in names
    assert "render.start" in names and "render.end" in names
    # monotonic возрастание внутри цепочки
    idx = {n: names.index(n) for n in ("callback.received", "open.api_start", "open.api_end")}
    assert events[idx["callback.received"]]["mono"] <= events[idx["open.api_start"]]["mono"]
    assert events[idx["open.api_start"]]["mono"] <= events[idx["open.api_end"]]["mono"]
    # корреляция
    for e in events:
        assert e["op_session_id"] == 77
        assert e["telegram_user_id"] == UID
    # charged из API-ответа попадает в диагностику
    api_end = events[idx["open.api_end"]]
    assert api_end.get("charged") is True
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_diagnostic_hook_exception_isolated(client, tg):
    """Ошибка в hook Boom не ломает OP-механику."""
    _mock_op_and_click()

    def bad_hook(payload):
        raise RuntimeError("boom integration bug")

    block = make_block(client, tg, diagnostic_hook=bad_hook)
    await block.open(UID, CHAT)
    ok = await block.handle_callback(UID, CHAT, "paop:open:12", answer=UrlAnswer())
    assert ok is True
    await asyncio.sleep(0.05)
    assert not any("open:12" in (b.get("callback_data") or "") for b in tg.flat_buttons())
    await block.aclose()
    await client.aclose()


# ---------------------------------------------------------------------------
# D. Финансовая безопасность
# ---------------------------------------------------------------------------

@respx.mock
async def test_repeat_tap_after_completion_no_second_charge(client, tg):
    """Повторный тап по выполненной кнопке (stale keyboard): settlement
    НЕ вызывается повторно локально, даже если API бы вернул charged=false."""
    click = _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    answer = UrlAnswer()

    await block.handle_callback(UID, CHAT, "paop:open:12", answer=answer)
    await asyncio.sleep(0.05)
    assert click.call_count == 1

    # Повторный тап по stale-кнопке — задание уже не в incomplete.
    await block.handle_callback(UID, CHAT, "paop:open:12", answer=answer)
    await asyncio.sleep(0.05)
    assert click.call_count == 1, "settlement не должен повторяться"
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_server_idempotency_charged_false_still_completes(client, tg):
    """Гонка/повтор дошёл до сервера: charged=false — повторной оплаты
    нет серверно, SDK завершает задание и убирает кнопку как обычно."""
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(12, "bot_tracked_click", url="https://x.test/r/tok",
              target_url="https://t.me/paid_bot?start=pa_12",
              open_mode="callback", username="paid_bot"),
    ])))
    respx.post(f"{BASE}/op-sessions/77/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 77}))
    click = respx.post(f"{BASE}/tasks/12/callback-click").mock(
        return_value=httpx.Response(200, json={
            "status": "ok", "task_id": 12, "task_status": "confirmed",
            "verification_status": "confirmed", "charged": False,
            "target_url": "https://t.me/paid_bot?start=pa_12",
        }))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:open:12", answer=UrlAnswer())
    await asyncio.sleep(0.05)
    assert click.called
    assert not any("open:12" in (b.get("callback_data") or "") for b in tg.flat_buttons())
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_settlement_ok_edit_fails_recovers_without_double_settlement(client, tg):
    """Settlement прошёл, Telegram edit упал — recovery через retry/sync
    восстанавливает UI БЕЗ повторного settlement."""
    click = _mock_op_and_click()
    block = make_block(client, tg, max_render_retries=1)
    await block.open(UID, CHAT)

    tg.fail("edit", Exception("Telegram server says: temporary"))
    answer = UrlAnswer()
    await block.handle_callback(UID, CHAT, "paop:open:12", answer=answer)
    # retry backoff — секундная шкала (2/5/15); ждём первую ступень.
    for _ in range(40):
        await asyncio.sleep(0.1)
        buttons = tg.flat_buttons()
        if not any("open:12" in (b.get("callback_data") or "") for b in buttons):
            break

    assert click.call_count == 1, "settlement ровно один даже при сбое render"
    # после retry клавиатура в конечном счёте без задания 12
    buttons = tg.flat_buttons()
    assert not any("open:12" in (b.get("callback_data") or "") for b in buttons)
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_api_failure_button_stays_no_completion(client, tg):
    """API недоступен → задание НЕ выполнено, кнопка остаётся,
    пользователь может безопасно повторить."""
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload([
        _task(12, "bot_tracked_click", url="https://x.test/r/tok",
              target_url="https://t.me/paid_bot?start=pa_12",
              open_mode="callback", username="paid_bot"),
    ])))
    respx.post(f"{BASE}/op-sessions/77/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 77}))
    respx.post(f"{BASE}/tasks/12/callback-click").mock(
        return_value=httpx.Response(500, json={"status": "error", "message": "boom"}))
    respx.post(f"{BASE}/op-sessions/77/check").mock(return_value=httpx.Response(
        200, json=_status_payload([
            _task(12, "bot_tracked_click", url="https://x.test/r/tok",
                  target_url="https://t.me/paid_bot?start=pa_12", open_mode="callback"),
        ], incomplete=[12])))
    respx.get(f"{BASE}/op-sessions/77/status").mock(return_value=httpx.Response(
        200, json=_status_payload([
            _task(12, "bot_tracked_click", url="https://x.test/r/tok",
                  target_url="https://t.me/paid_bot?start=pa_12", open_mode="callback"),
        ], incomplete=[12])))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    answer = UrlAnswer()
    await block.handle_callback(UID, CHAT, "paop:open:12", answer=answer)
    await asyncio.sleep(0.05)

    # Кнопка НЕ исчезла — действие не подтверждено.
    assert any("open:12" in (b.get("callback_data") or "") for b in tg.flat_buttons())
    # Ни один answer не содержит url — destination не открыт без settlement.
    assert not any(c.get("url") for c in answer.calls)
    await block.aclose()
    await client.aclose()


# ---------------------------------------------------------------------------
# E. Completion — пустая клавиатура
# ---------------------------------------------------------------------------

@respx.mock
async def test_completion_empty_keyboard_single_task(client, tg):
    """Единственное задание выполнено → «Доступ открыт», клавиатура ПУСТАЯ —
    никаких reopen-строк в финальном состоянии."""
    _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.handle_callback(UID, CHAT, "paop:open:12", answer=UrlAnswer())
    await asyncio.sleep(0.05)

    buttons = tg.flat_buttons()
    assert buttons == [], f"финальная клавиатура должна быть пустой: {buttons}"
    if tg.text_edits:
        assert "Доступ открыт" in tg.text_edits[-1][2]
    await block.aclose()
    await client.aclose()


# ---------------------------------------------------------------------------
# F. Latency structure: меньше Telegram-вызовов на critical path
# ---------------------------------------------------------------------------

@respx.mock
async def test_fewer_telegram_calls_than_legacy(client, tg):
    """New UX: 0 sendMessage на тап (нет слота), 1 editMarkup.
    Legacy: 1 sendMessage (слот) + 1 editMarkup + позже deleteMessage.
    Новый путь строго дешевле по числу последовательных Telegram RTT."""
    _mock_op_and_click()
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    # сбрасываем начальную отправку
    sent_before = len(tg.sent)
    edits_before = len(tg.edits) + len(tg.text_edits)

    await block.handle_callback(UID, CHAT, "paop:open:12", answer=UrlAnswer())
    await asyncio.sleep(0.1)

    new_sends = len(tg.sent) - sent_before
    new_edits = len(tg.edits) + len(tg.text_edits) - edits_before
    assert new_sends == 0, "ни одного нового сообщения"
    assert new_edits >= 1, "ровно редактирование основного сообщения"
    await block.aclose()
    await client.aclose()
