"""Тесты фоновой адаптивной синхронизации InteractiveOpBlock (SDK 1.3.2).

Partner API — respx, Telegram — FakeTelegram из test_interactive.
Расписание ускорено monkeypatch'ем модульных констант.
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
from pastuh_ads import interactive as interactive_mod

from test_interactive import BASE, API_KEY, FakeTelegram, _op_list_payload, _status_payload, _task

UID = 777_001
CHAT = 777_001


@pytest.fixture(autouse=True)
def fast_schedule(monkeypatch):
    monkeypatch.setattr(interactive_mod, "_SYNC_PHASES", ((0.6, 0.03), (1.2, 0.06), (1.8, 0.1)))
    monkeypatch.setattr(interactive_mod, "_SYNC_FIRST_DELAY", 0.01)
    monkeypatch.setattr(interactive_mod, "_SYNC_MIN_INTERVAL_FLOOR", 0.01)
    monkeypatch.setattr(interactive_mod, "_SYNC_FALLBACK_MIN_INTERVAL", 0.03)


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def client() -> PastuhAdsClient:
    return PastuhAdsClient(base_url=BASE, api_key=API_KEY, source_id=50, placement_id=7, max_retries=0)


def make_block(client, tg, **kw) -> InteractiveOpBlock:
    return InteractiveOpBlock(
        client,
        send_message=tg.send,
        edit_markup=tg.edit_markup,
        delete_message=tg.delete,
        edit_text=tg.edit_text,
        slot_ttl_seconds=kw.pop("slot_ttl_seconds", 0.05),
        sync_min_interval_seconds=kw.pop("sync_min_interval_seconds", 0.01),
        **kw,
    )


def _snap(session_id: int, incomplete: list[int], completed: list[int], *, access: bool = False,
          status: str | None = None) -> dict:
    return {
        "op_session_id": session_id,
        "session_status": status or ("completed" if access else "shown"),
        "access_allowed": access,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "completed_task_ids": completed,
        "incomplete_task_ids": incomplete,
    }


def _sync_body(*snaps: dict, unknown: list[int] | None = None) -> dict:
    return {"status": "ok", "sessions": list(snaps), "unknown_session_ids": unknown or [],
            "server_time": datetime.now(timezone.utc).isoformat()}


def _mock_open(tasks: list[dict], session_id: int = 77) -> None:
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload(tasks, session_id=session_id)))
    respx.post(f"{BASE}/op-sessions/{session_id}/shown").mock(return_value=httpx.Response(
        200, json={"status": "ok", "op_session_id": session_id, "session_status": "shown"}))


async def _wait(predicate, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


TWO_CHANNELS = [
    _task(11, "subscription_confirmed", slot=1, url="https://t.me/chan_a", button="Открыть канал"),
    _task(12, "subscription_confirmed", slot=2, url="https://t.me/chan_b", button="Открыть канал"),
]


@respx.mock
async def test_slot_disappears_without_user_action(client, tg):
    """Пользователь подписался и вернулся — без /start и «Проверить»."""
    _mock_open(TWO_CHANNELS)
    replies = iter([_sync_body(_snap(77, [11, 12], [])), _sync_body(_snap(77, [12], [11]))])
    sync = respx.post(f"{BASE}/op-sessions/sync").mock(
        side_effect=lambda req: httpx.Response(200, json=next(replies, _sync_body(_snap(77, [12], [11])))))
    check = respx.post(f"{BASE}/op-sessions/77/check").mock(return_value=httpx.Response(500))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    assert await _wait(lambda: tg.edits)

    # Одинаковые подписи «Открыть канал» у разных заданий: исчезла
    # только кнопка выполненного (по task_id / url), вторая осталась.
    buttons = tg.flat_buttons(tg.edits[-1][2])
    assert [b["url"] for b in buttons] == ["https://t.me/chan_b"]
    assert tg.edits[-1][1] == tg.sent[0][1]  # редактируется исходное сообщение
    assert len(tg.sent) == 1
    assert not check.called  # фон — только read-only sync, без /check
    assert sync.calls[0].request.content and b'"op_session_ids":[77]' in sync.calls[0].request.content.replace(b" ", b"")
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_full_completion_edits_text_and_stops_loop(client, tg):
    _mock_open(TWO_CHANNELS)
    sync = respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(_snap(77, [], [11, 12], access=True))))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    assert await _wait(lambda: tg.text_edits)
    assert tg.text_edits[-1][2] == interactive_mod._COMPLETED_TEXT
    assert tg.text_edits[-1][3] == []
    calls = sync.call_count
    await asyncio.sleep(0.2)
    assert sync.call_count == calls == 1  # после completion — ни одного запроса
    assert block._sync_task is None or block._sync_task.done()
    await client.aclose()


@respx.mock
async def test_completed_task_never_resurrects(client, tg):
    _mock_open(TWO_CHANNELS)
    replies = iter([_sync_body(_snap(77, [12], [11]))])
    respx.post(f"{BASE}/op-sessions/sync").mock(
        side_effect=lambda req: httpx.Response(200, json=next(replies, _sync_body(_snap(77, [11, 12], [])))))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    assert await _wait(lambda: tg.edits)
    await asyncio.sleep(0.15)
    for _, _, kb in tg.edits:
        assert [b["url"] for b in tg.flat_buttons(kb)] == ["https://t.me/chan_b"]
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_multiple_sessions_share_one_batch(client, tg):
    respx.post(f"{BASE}/op-list").mock(side_effect=[
        httpx.Response(200, json=_op_list_payload([TWO_CHANNELS[0]], session_id=81)),
        httpx.Response(200, json=_op_list_payload([TWO_CHANNELS[1]], session_id=82)),
    ])
    for sid in (81, 82):
        respx.post(f"{BASE}/op-sessions/{sid}/shown").mock(return_value=httpx.Response(
            200, json={"status": "ok", "op_session_id": sid, "session_status": "shown"}))
    sync = respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(_snap(81, [11], []), _snap(82, [12], []))))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await block.open(UID + 1, CHAT + 1)
    assert await _wait(lambda: any(b'81' in c.request.content and b'82' in c.request.content for c in sync.calls))
    # Ни одного запроса на сессию: только батч.
    assert all(c.request.url.path.endswith("/op-sessions/sync") for c in sync.calls)
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_rate_limit_backoff_then_recovery(client, tg, monkeypatch):
    _mock_open(TWO_CHANNELS)
    replies = iter([
        httpx.Response(429, json={"status": "error", "error": "rate_limited", "message": "Too Many Attempts."}),
        httpx.Response(200, json=_sync_body(_snap(77, [12], [11]))),
    ])
    sync = respx.post(f"{BASE}/op-sessions/sync").mock(side_effect=lambda req: next(replies))
    errors: list[str] = []

    block = make_block(client, tg, on_error=lambda ctx, exc: errors.append(ctx))
    started = asyncio.get_running_loop().time()
    await block.open(UID, CHAT)
    # 429 → пауза backoff (5с), сокращаем для теста.
    assert await _wait(lambda: block._sync_pause_until > 0)
    paused_for = block._sync_pause_until - interactive_mod.time.monotonic()
    assert 3.0 < paused_for <= 5.0
    block._sync_pause_until = 0.0
    assert await _wait(lambda: tg.edits)
    assert "sync" in errors and sync.call_count == 2
    assert block._sync_failures == 0
    assert asyncio.get_running_loop().time() - started < 2.0
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_network_timeout_backoff(client, tg):
    _mock_open(TWO_CHANNELS)
    replies = iter([httpx.ReadTimeout("slow"), httpx.Response(200, json=_sync_body(_snap(77, [12], [11])))])

    def _reply(req):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    respx.post(f"{BASE}/op-sessions/sync").mock(side_effect=_reply)
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    assert await _wait(lambda: block._sync_failures == 1)
    assert 0 < block._sync_pause_until - interactive_mod.time.monotonic() <= 2.0
    block._sync_pause_until = 0.0
    assert await _wait(lambda: tg.edits)
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_old_server_falls_back_to_status(client, tg):
    _mock_open(TWO_CHANNELS)
    respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    status = respx.get(f"{BASE}/op-sessions/77/status").mock(return_value=httpx.Response(
        200, json=_status_payload(TWO_CHANNELS, [12])))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    assert await _wait(lambda: tg.edits)
    assert status.called and block._sync_batch_supported is False
    assert [b["url"] for b in tg.flat_buttons(tg.edits[-1][2])] == ["https://t.me/chan_b"]
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_unknown_and_expired_sessions_stop_sync(client, tg):
    _mock_open(TWO_CHANNELS)
    sync = respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(unknown=[77])))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    assert await _wait(lambda: sync.called)
    await asyncio.sleep(0.1)
    assert sync.call_count == 1 and not block._states[UID].sync_active

    sync.mock(return_value=httpx.Response(200, json=_sync_body(_snap(77, [11, 12], [], status="expired"))))
    block._touch(block._states[UID])
    assert await _wait(lambda: sync.call_count == 2)
    await asyncio.sleep(0.1)
    assert sync.call_count == 2 and not block._states[UID].sync_active
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_idle_timeout_stops_and_user_event_restarts(client, tg):
    _mock_open(TWO_CHANNELS)
    sync = respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(_snap(77, [11, 12], []))))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    # Фазы 0.03→0.06→0.1 до 1.8с бездействия, затем стоп.
    assert await _wait(lambda: block._sync_task is not None and block._sync_task.done(), timeout=4.0)
    calls = sync.call_count
    assert 10 < calls < 80
    await asyncio.sleep(0.2)
    assert sync.call_count == calls

    # Любой paop-callback возвращает блок в быструю фазу.
    await block.handle_callback(UID, CHAT, "paop:reopen:999")
    assert await _wait(lambda: sync.call_count > calls)
    await block.aclose()
    assert block._sync_task is None
    await client.aclose()


@respx.mock
async def test_auto_sync_disabled_keeps_131_behavior(client, tg):
    _mock_open(TWO_CHANNELS)
    sync = respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(200, json=_sync_body()))
    status = respx.get(f"{BASE}/op-sessions/77/status").mock(return_value=httpx.Response(
        200, json=_status_payload(TWO_CHANNELS, [12])))

    block = make_block(client, tg, auto_sync=False, reconcile_interval_seconds=0.02)
    await block.open(UID, CHAT)
    assert await _wait(lambda: status.called)
    assert not sync.called and block._sync_task is None
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_restore_resume_restarts_sync(client, tg):
    _mock_open(TWO_CHANNELS)
    respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(_snap(77, [11, 12], []))))
    first = make_block(client, tg)
    await first.open(UID, CHAT)
    dumped = first.dump_state()
    await first.aclose()

    respx.get(f"{BASE}/op-sessions/77/status").mock(return_value=httpx.Response(
        200, json=_status_payload(TWO_CHANNELS, [11, 12])))
    sync = respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(_snap(77, [11], [12]))))
    second = make_block(client, tg)
    second.restore_state(dumped)
    await second.resume()
    assert await _wait(lambda: sync.called and tg.edits
                       and [b["url"] for b in tg.flat_buttons(tg.edits[-1][2])] == ["https://t.me/chan_a"])
    await second.aclose()
    await client.aclose()


@respx.mock
async def test_telegram_429_on_edit_honors_retry_after(client, tg, monkeypatch):
    _mock_open(TWO_CHANNELS)
    respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(_snap(77, [12], [11]))))
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *a, **kw):
        if delay >= 1.0:
            delays.append(delay)
            delay = 0.01
        return await real_sleep(delay, *a, **kw)

    class RetryAfter(Exception):
        retry_after = 3

    tg.fail("edit", RetryAfter("Telegram server says - Too Many Requests: retry after 3"))
    monkeypatch.setattr(interactive_mod.asyncio, "sleep", fake_sleep)
    block = make_block(client, tg)
    await block.open(UID, CHAT)
    assert await _wait(lambda: tg.edits)
    assert 3.5 in delays  # retry_after + 0.5
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_not_modified_on_sync_render_is_quiet(client, tg):
    _mock_open(TWO_CHANNELS)
    respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(_snap(77, [12], [11]))))
    errors: list[str] = []
    tg.fail("edit", Exception("Bad Request: message is not modified"))
    block = make_block(client, tg, on_error=lambda ctx, exc: errors.append(ctx))
    await block.open(UID, CHAT)
    await asyncio.sleep(0.2)
    assert "render" not in errors
    await block.aclose()
    await client.aclose()


def test_retry_after_parsing():
    assert interactive_mod._retry_after_seconds(Exception("Too Many Requests: retry after 7")) == 7.0
    assert interactive_mod._retry_after_seconds(Exception("other")) is None


async def test_expired_states_are_pruned(client, tg):
    block = make_block(client, tg)
    block.restore_state({"version": 1, "blocks": [{
        "op_session_id": 5, "telegram_user_id": 1, "chat_id": 1, "message_id": 10,
        "expires_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "tasks": [], "incomplete_ids": [], "paid_done_ids": [],
    }, {
        "op_session_id": 6, "telegram_user_id": 2, "chat_id": 2, "message_id": 11,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "tasks": [], "incomplete_ids": [], "paid_done_ids": [],
    }]})
    block._prune_states()
    assert list(block._states) == [2]
    await client.aclose()
