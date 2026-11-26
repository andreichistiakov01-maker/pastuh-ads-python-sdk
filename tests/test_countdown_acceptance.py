"""Exact acceptance: 4 задания → 3 → 2 → 1 → завершено.

Мандат финальной приёмки OP V2.1: в рамках живой сессии — тот же message_id,
тот же session_id, нет повторного sendMessage, нет resurrection. Каждое
серверное подтверждение убирает ровно одну кнопку через edit markup.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest
import respx

from pastuh_ads import PastuhAdsClient
from tests.test_interactive import (
    BASE, CHAT, UID, FakeTelegram, _op_list_payload, _status_payload, _task,
    make_block,
)


def _four_tasks() -> list[dict]:
    return [
        _task(11, "subscription_confirmed", slot=1, url="https://t.me/chan_a"),
        _task(12, "subscription_confirmed", slot=2, url="https://t.me/chan_b"),
        _task(13, "subscription_confirmed", slot=3, url="https://t.me/chan_c"),
        _task(14, "subscription_confirmed", slot=4, url="https://t.me/chan_d"),
    ]


def _task_buttons(kb: list) -> list:
    return [b for row in kb for b in row if (b.get("callback_data") or "").startswith("paop:open:")
            or b.get("url")]


@pytest.fixture
def client() -> PastuhAdsClient:
    return PastuhAdsClient(
        base_url=BASE, api_key="opk_testkey_testkey_testkey_testkey_testke", source_id=50, placement_id=7,
    )


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()




@respx.mock
@pytest.mark.asyncio
async def test_four_to_zero_countdown_same_message_no_resend(client: PastuhAdsClient, tg: FakeTelegram):
    tasks = _four_tasks()
    respx.post(f"{BASE}/op-list").mock(return_value=httpx.Response(200, json=_op_list_payload(tasks)))
    respx.post(f"{BASE}/op-sessions/77/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 77, "session_status": "shown"}))

    # Сервер подтверждает задачи прогрессивно: сначала 11, потом 11+12, 11+12+13, наконец все.
    confirmed_so_far: list[int] = []

    def check_handler(request):
        done = {t["task_id"]: dict(t, status="confirmed") for t in tasks if t["task_id"] in confirmed_so_far}
        merged = [done.get(t["task_id"], t) for t in tasks]
        incomplete = [t["task_id"] for t in tasks if t["task_id"] not in confirmed_so_far]
        access = len(confirmed_so_far) == 4
        return httpx.Response(200, json=_status_payload(merged, incomplete, access=access))

    respx.post(f"{BASE}/op-sessions/77/check").mock(side_effect=check_handler)

    block = make_block(client, tg)
    await block.open(UID, CHAT)

    # 1. Ровно один sendMessage, 4 кнопки заданий.
    assert len(tg.sent) == 1, "initial open — единственный sendMessage"
    main_mid = tg.sent[0][1]
    initial_buttons = _task_buttons(tg.sent[0][3])
    assert len(initial_buttons) == 4, f"ожидалось 4 кнопки, получено {len(initial_buttons)}"

    expected_counts = [(11, 3), (12, 2), (13, 1)]
    for task_id, remaining in expected_counts:
        confirmed_so_far.append(task_id)
        sends_before = len(tg.sent)
        await block.handle_callback(UID, CHAT, "paop:check")

        # Тот же message_id: новых sendMessage быть не должно.
        assert len(tg.sent) == sends_before, f"после подтверждения {task_id} появился повторный sendMessage"
        assert tg.edits, "подтверждение должно редактировать markup"
        edit_mid = tg.edits[-1][1]
        assert edit_mid == main_mid, f"edit пошёл в message {edit_mid} вместо {main_mid}"
        buttons = _task_buttons(tg.edits[-1][2])
        assert len(buttons) == remaining, (
            f"после подтверждения {task_id}: ожидалось {remaining} кнопок, получено {len(buttons)}")
        # Подтверждённая кнопка исчезла, нет resurrection предыдущих.
        cb_datas = [b.get("callback_data") or "" for b in buttons]
        urls = [b.get("url") or "" for b in buttons]
        assert not any(str(task_id) in cb for cb in cb_datas)

    # 2. Четвёртое подтверждение — блок завершён.
    confirmed_so_far.append(14)
    sends_before = len(tg.sent)
    await block.handle_callback(UID, CHAT, "paop:check")
    assert len(tg.sent) == sends_before, "completion не должен порождать новый sendMessage"
    assert tg.text_edits, "completion меняет текст"
    assert "Доступ открыт" in tg.text_edits[-1][2]
    final_kb = tg.text_edits[-1][3]
    final_buttons = _task_buttons(final_kb)
    # Reopen-CTA допустимы, но ни одной кнопки задания с url на каналы.
    assert not any("chan_" in (b.get("url") or "") for b in final_buttons), "resurrection url-кнопок после completion"

    # 3. Повторный check после completion — ничего не воскрешает.
    await block.handle_callback(UID, CHAT, "paop:check")
    assert len(tg.sent) == 1, "за весь lifecycle — ровно один sendMessage"
    await client.aclose()
