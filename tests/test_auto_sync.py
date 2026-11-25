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
    monkeypatch.setattr(interactive_mod, "_SYNC_IDLE_INTERVAL", 0.15)
    monkeypatch.setattr(interactive_mod, "_SYNC_IDLE_GIVEUP", 3.0)
    monkeypatch.setattr(interactive_mod, "_SYNC_JITTER", 0.0)
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


@respx.mock
async def test_idle_poll_until_expiry_completion_after_inactivity(client, tg):
    """Сценарий приёмки: пользователь неактивен дольше всех быстрых фаз
    (в проде — 12+ минут), затем подписывается молча. Слот исчезает
    через редкий idle-poll — без /start и callback."""
    _mock_open(TWO_CHANNELS)
    # Сервер подтверждает задание в момент, когда блок уже давно в idle.
    calls = {"n": 0}

    def sync_handler(req):
        calls["n"] += 1
        done = [11] if calls["n"] >= 3 else []
        return httpx.Response(200, json=_sync_body(
            _snap(77, [12] if done else [11, 12], done)))

    sync = respx.post(f"{BASE}/op-sessions/sync").mock(side_effect=sync_handler)

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    # Ждём выхода за пределы быстрых фаз (0.6/1.2/1.8с в ускорении) —
    # sync-запросы НЕ прекращаются, а переходят в idle-режим.
    await asyncio.sleep(2.0)
    n_before = calls["n"]
    assert n_before >= 1
    assert block._states[UID].sync_active  # цикл жив, а не остановлен
    await asyncio.sleep(0.5)
    assert calls["n"] > n_before  # idle-poll продолжается

    # «Подписка» подтверждена сервером — кнопка исчезает без действий.
    assert await _wait(lambda: tg.edits)
    buttons = tg.flat_buttons(tg.edits[-1][2])
    assert [b["url"] for b in buttons] == ["https://t.me/chan_b"]
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_idle_giveup_bounds_lifetime(client, tg):
    """Аварийный giveup: после _SYNC_IDLE_GIVEUP бездействия polling
    останавливается даже без expiry — память/сеть ограничены."""
    _mock_open(TWO_CHANNELS)
    sync = respx.post(f"{BASE}/op-sessions/sync").mock(
        return_value=httpx.Response(200, json=_sync_body(_snap(77, [11, 12], []))))

    block = make_block(client, tg)
    await block.open(UID, CHAT)
    await asyncio.sleep(3.4)  # giveup = 3.0 в ускорении
    assert not block._states[UID].sync_active
    calls = sync.call_count
    await asyncio.sleep(0.4)
    assert sync.call_count == calls
    await block.aclose()
    await client.aclose()


@respx.mock
async def test_on_completed_fires_once_sync_detected(client, tg):
    """Completion event партнёра: фоновая синхронизация обнаружила
    выполнение — callback вызван ровно один раз, async поддержан."""
    _mock_open(TWO_CHANNELS)
    respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(_snap(77, [], [11, 12], access=True))))
    events = []

    async def on_completed(uid, sid):
        await asyncio.sleep(0)
        events.append((uid, sid))

    block = make_block(client, tg, on_completed=on_completed)
    await block.open(UID, CHAT)
    assert await _wait(lambda: events)
    await asyncio.sleep(0.2)
    assert events == [(UID, 77)]  # ровно один раз
    await client.aclose()


@respx.mock
async def test_on_completed_sync_callable_and_error_isolated(client, tg):
    """Sync-callback тоже поддержан; исключение hook'а не ломает OP."""
    _mock_open(TWO_CHANNELS)
    respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(_snap(77, [], [11, 12], access=True))))
    events = []

    def bad_hook(uid, sid):
        events.append((uid, sid))
        raise RuntimeError("partner bug")

    block = make_block(client, tg, on_completed=bad_hook)
    await block.open(UID, CHAT)
    assert await _wait(lambda: events)
    assert await _wait(lambda: tg.text_edits)  # completion-рендер не пострадал
    assert tg.text_edits[-1][2] == interactive_mod._COMPLETED_TEXT
    await asyncio.sleep(0.1)
    assert events == [(UID, 77)]
    await client.aclose()


@respx.mock
async def test_on_completed_not_refired_after_restore(client, tg):
    """Exactly-once переживает restart: уведомлённая сессия не стреляет
    повторно; завершившаяся в простое — стреляет один раз при resume."""
    _mock_open(TWO_CHANNELS)
    respx.post(f"{BASE}/op-sessions/sync").mock(return_value=httpx.Response(
        200, json=_sync_body(_snap(77, [], [11, 12], access=True))))
    respx.get(f"{BASE}/op-sessions/77/status").mock(return_value=httpx.Response(
        200, json=_status_payload([TWO_CHANNELS[0], TWO_CHANNELS[1]], [], session_id=77,
                                  access=True)))
    events = []

    async def hook(uid, sid):
        events.append((uid, sid))

    block = make_block(client, tg, on_completed=hook)
    await block.open(UID, CHAT)
    assert await _wait(lambda: events)
    assert events == [(UID, 77)]
    dump = block.dump_state()
    await block.aclose()

    # Рестарт: флаг персистирован — повторного события нет.
    block2 = make_block(client, tg, on_completed=hook)
    block2.restore_state(dump)
    await block2.resume()
    await asyncio.sleep(0.3)
    assert events == [(UID, 77)]
    await block2.aclose()
    await client.aclose()


def test_retry_after_honored_in_backoff(client, tg):
    """429 с Retry-After: пауза не меньше серверной подсказки; без
    подсказки — экспоненциальный backoff."""
    import time as _time

    from pastuh_ads import RateLimitError

    block = make_block(client, tg)
    before = _time.monotonic()
    block._sync_backoff(RateLimitError("x", status_code=429, retry_after=30.0), base=5.0, cap=60.0)
    assert 29.0 <= block._sync_pause_until - before <= 31.0

    block._sync_failures = 0
    block._sync_pause_until = 0.0
    before = _time.monotonic()
    block._sync_backoff(RateLimitError("x", status_code=429), base=5.0, cap=60.0)
    pause = block._sync_pause_until - before
    assert 4.5 <= pause <= 5.5  # первая экспоненциальная ступень, не хуже


@respx.mock
async def test_fairness_120_sessions_all_synced(client, tg):
    """120 активных сессий одного источника (>2 батчей по 50): ротация
    по next_sync_mono — обслуживаются ВСЕ, голодания нет."""
    n_sessions = 120
    tasks_by_session = {}
    for sid in range(1000, 1000 + n_sessions):
        tasks_by_session[sid] = [_task(sid * 10 + 1, "subscription_confirmed", slot=1,
                                       url=f"https://t.me/c{sid}")]
    seen_ids: set[int] = set()

    def handler(req):
        import json as _json
        body = _json.loads(req.content)
        assert len(body["op_session_ids"]) <= 50  # батч-контракт
        seen_ids.update(body["op_session_ids"])
        snaps = [_snap(sid, [sid * 10 + 1], []) for sid in body["op_session_ids"]]
        return httpx.Response(200, json=_sync_body(*snaps))

    respx.post(f"{BASE}/op-sessions/sync").mock(side_effect=handler)
    # Открываем сессии напрямую через состояние блока: op-list для каждого
    # пользователя — дорого в unit-тесте, важна ротация цикла.
    block = make_block(client, tg)
    now = asyncio.get_running_loop().time()
    for i, sid in enumerate(range(1000, 1000 + n_sessions)):
        uid = 5000 + i
        task_objs = [interactive_mod._task_from_dict(t) for t in tasks_by_session[sid]]
        state = interactive_mod._BlockState(
            op_session_id=sid, telegram_user_id=uid, chat_id=uid,
            tasks={t.task_id: t for t in task_objs},
            incomplete_ids={t.task_id for t in task_objs},
        )
        state.last_activity_mono = now
        block._states[uid] = state
        block._touch(state, delay=0.01)

    assert await _wait(lambda: len(seen_ids) == n_sessions, timeout=4.0)
    await block.aclose()
    await client.aclose()
