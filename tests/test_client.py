"""Тесты PastuhAdsClient с мокированным HTTP (respx).

Запуск:
    pip install -r requirements-dev.txt
    python -m pytest tests/ -v

Ни один тест не ходит в сеть.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
import pytest
import respx

from pastuh_ads import (
    ApiTimeoutError,
    AuthenticationError,
    ForbiddenError,
    NotFoundError,
    PastuhAdsClient,
    RateLimitError,
    ServerError,
    ValidationError,
    user_observation_from_user,
)

BASE = "https://ops.example.test/api/partner/v1"
API_KEY = "opk_testkey_testkey_testkey_testkey_testke"


@pytest.fixture
def client() -> PastuhAdsClient:
    return PastuhAdsClient(
        base_url=BASE,
        api_key=API_KEY,
        source_id=50,
        placement_id=7,
    )


# ---------------------------------------------------------------------------
# Базовые свойства запросов
# ---------------------------------------------------------------------------


@respx.mock
async def test_auth_header_and_base_url(client: PastuhAdsClient) -> None:
    route = respx.get(f"{BASE}/me").mock(
        return_value=httpx.Response(200, json={"status": "ok", "partner": {}, "source": {}, "payouts": {}})
    )
    await client.get_me()
    await client.aclose()

    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.headers["Accept"] == "application/json"
    assert str(request.url) == f"{BASE}/me"


@respx.mock
async def test_get_me_maps_response(client: PastuhAdsClient) -> None:
    respx.get(f"{BASE}/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "partner": {
                    "id": 4, "status": "active", "balance": 100.0,
                    "pending_balance": 5.0, "available_balance": 95.0,
                    "open_fraud_flags": 0,
                },
                "source": {"id": 50, "username": "mybot", "status": "approved", "quality_level": "trusted"},
                "payouts": {"pending_count": 1, "paid_total": 250.5},
            },
        )
    )
    me = await client.get_me()
    await client.aclose()

    assert me.partner.id == 4
    assert me.partner.available_balance == 95.0
    assert me.source.id == 50
    assert me.source.username == "mybot"
    assert me.payouts.paid_total == 250.5


# ---------------------------------------------------------------------------
# OP flow
# ---------------------------------------------------------------------------


@respx.mock
async def test_op_list_sends_required_fields(client: PastuhAdsClient) -> None:
    route = respx.post(f"{BASE}/op-list").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok", "op_session_id": 99, "requested_slots": 2,
                "filled_slots": 2, "access_allowed": False,
                "expires_at": "2026-09-27T20:00:00+00:00",
                "tasks": [
                    {"task_id": 11, "slot": 1, "advertised_resource_id": 3,
                     "target_type": "telegram_channel", "action_type": "subscription_confirmed",
                     "title": "Channel A", "username": "channel_a",
                     "url": "https://t.me/channel_a", "instruction_text": "Подпишитесь",
                     "action_button_text": "Подписаться", "verification_hint": None}
                ],
                "channels": [],
            },
        )
    )
    result = await client.get_op_list(telegram_user_id=555001)
    await client.aclose()

    import json
    sent = json.loads(route.calls[0].request.content)
    assert sent["source_id"] == 50
    assert sent["placement_id"] == 7
    assert sent["telegram_user_id"] == 555001

    assert result.op_session_id == 99
    assert result.tasks[0].task_id == 11
    assert result.tasks[0].action_type == "subscription_confirmed"


@respx.mock
async def test_op_list_sends_user_observation(client: PastuhAdsClient) -> None:
    """Premium SDK: user_observation уходит в теле запроса."""
    route = respx.post(f"{BASE}/op-list").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok", "op_session_id": 100, "requested_slots": 2,
                "filled_slots": 1, "access_allowed": False,
                "expires_at": None, "tasks": [],
                "premium_claim": {"status": "applied"},
            },
        )
    )
    result = await client.get_op_list(
        telegram_user_id=555002,
        user_observation={"is_premium": True},
    )
    await client.aclose()

    import json
    sent = json.loads(route.calls[0].request.content)
    assert sent["user_observation"] == {"is_premium": True}
    assert result.premium_claim is not None
    assert result.premium_claim.status == "applied"


@respx.mock
async def test_op_list_parses_premium_verification_hint(client: PastuhAdsClient) -> None:
    """Premium V2 fallback: premium_verification парсится в модель."""
    respx.post(f"{BASE}/op-list").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok", "op_session_id": 101, "requested_slots": 2,
                "filled_slots": 0, "access_allowed": False,
                "expires_at": None, "tasks": [],
                "premium_verification": {
                    "url": "https://t.me/service_bot?start=pv_555_1_abc",
                    "expires_at": "2026-11-11T20:00:00+00:00",
                },
                "premium_claim": {"status": "ignored_untrusted"},
            },
        )
    )
    result = await client.get_op_list(telegram_user_id=555003)
    await client.aclose()

    assert result.premium_verification is not None
    assert result.premium_verification.url.endswith("pv_555_1_abc")
    assert result.premium_claim is not None
    assert result.premium_claim.status == "ignored_untrusted"


@respx.mock
async def test_verify_premium(client: PastuhAdsClient) -> None:
    """POST /premium-verification — initData-канал фиксации статуса."""
    route = respx.post(f"{BASE}/premium-verification").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok", "verified": True, "telegram_user_id": 555004,
                "is_premium": True, "observed_at": "2026-11-08T20:00:00+00:00",
            },
        )
    )
    result = await client.verify_premium(555004, "query_id=AAE&user=%7B%22id%22%3A555004%7D&hash=x")
    await client.aclose()

    import json
    sent = json.loads(route.calls[0].request.content)
    assert sent["telegram_user_id"] == 555004
    assert "init_data" in sent
    assert result.verified is True
    assert result.is_premium is True


# ---------------------------------------------------------------------------
# user_observation_from_user — автоматический сбор из Telegram-апдейтов
# ---------------------------------------------------------------------------


def test_user_observation_from_dict() -> None:
    # Полный User dict (Bot API: id + is_bot + first_name обязательны).
    full = {"id": 1, "is_bot": False, "first_name": "P"}
    # SDK 1.2.0: gender-claim — ЯВНЫЙ opt-in. По умолчанию наблюдение
    # побайтово как у SDK 1.1.0 — только is_premium, классификатор
    # не вызывается вовсе.
    assert user_observation_from_user({**full, "is_premium": True}) == {
        "is_premium": True}
    assert user_observation_from_user({**full, "is_premium": False}) == {
        "is_premium": False}
    # Полный User БЕЗ поля is_premium → Telegram его не прислал → non-Premium.
    assert user_observation_from_user(full) == {"is_premium": False}
    # Opt-in: вердикт добавляется только по явному флагу.
    assert user_observation_from_user(full, include_gender=True) == {
        "is_premium": False, "gender": "unknown"}
    assert user_observation_from_user(
        {**full, "is_premium": True}, include_gender=True) == {
        "is_premium": True, "gender": "unknown"}


def test_user_observation_from_object() -> None:
    class TgUser:
        id = 1
        is_bot = False
        first_name = "P"
        is_premium = True

    assert user_observation_from_user(TgUser()) == {"is_premium": True}
    assert user_observation_from_user(TgUser(), include_gender=True) == {
        "is_premium": True, "gender": "unknown"}

    class RegularUser:
        id = 2
        is_bot = False
        first_name = "R"
        is_premium = None

    # Полный объект, слот есть, значение None → non-Premium claim.
    assert user_observation_from_user(RegularUser()) == {"is_premium": False}


def test_user_observation_partial_returns_none() -> None:
    # Частичный/неоднозначный объект → наблюдение НЕ отправляется.
    # Никогда не фабрикуем статус из неполных данных.
    assert user_observation_from_user({"id": 1}) is None            # dict без is_bot/first_name
    assert user_observation_from_user({"id": 1, "is_bot": False}) is None
    assert user_observation_from_user(None) is None
    assert user_observation_from_user(object()) is None

    class WeirdObj:
        id = 1
        is_bot = False
        first_name = "W"
        # нет слота is_premium — не можем отличить «поля нет» от «статус неизвестен»

    assert user_observation_from_user(WeirdObj()) is None


def test_user_observation_from_aiogram_user() -> None:
    """Реальный aiogram 3 types.User — основной кейс партнёрских ботов."""
    aiogram = pytest.importorskip("aiogram")
    from aiogram.types import User

    # Telegram прислал is_premium=True
    premium = User(id=1, is_bot=False, first_name="P", is_premium=True)
    assert user_observation_from_user(premium) == {"is_premium": True}
    assert user_observation_from_user(premium, include_gender=True) == {
        "is_premium": True, "gender": "unknown"}

    # Явный False из апдейта — non-premium claim
    explicit_false = User(id=2, is_bot=False, first_name="R", is_premium=False)
    assert user_observation_from_user(explicit_false) == {"is_premium": False}

    # Полный aiogram User с is_premium=None (поле не пришло в апдейте)
    # → семантика True-only поля: пользователь не Premium → claim false.
    # Это даёт корректный downgrade после прежнего claim=true.
    regular = User(id=3, is_bot=False, first_name="U")
    assert regular.is_premium is None
    assert user_observation_from_user(regular) == {"is_premium": False}


@respx.mock
async def test_list_placements(client: PastuhAdsClient) -> None:
    respx.get(f"{BASE}/placements").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "placements": [
                    {"id": 7, "name": "OP-блок", "type": "op_slots",
                     "source_id": 50, "slots_count": 3, "status": "active"},
                    {"id": 8, "name": "Старый", "type": "op_slots",
                     "source_id": 50, "slots_count": 2, "status": "archived"},
                ],
            },
        )
    )
    placements = await client.list_placements()
    await client.aclose()

    assert len(placements) == 2
    assert placements[0].id == 7
    assert placements[0].status == "active"
    assert placements[0].source_id == 50
    assert placements[1].status == "archived"


@respx.mock
async def test_wrong_placement_raises_forbidden(client: PastuhAdsClient) -> None:
    """placement_id чужого источника → 403 placement_not_allowed."""
    respx.post(f"{BASE}/op-list").mock(
        return_value=httpx.Response(
            403, json={"status": "error", "error": "placement_not_allowed",
                       "message": "Плейсмент не принадлежит источнику трафика."})
    )
    with pytest.raises(ForbiddenError) as exc_info:
        await client.get_op_list(telegram_user_id=1, placement_id=999)
    await client.aclose()
    assert exc_info.value.error_code == "placement_not_allowed"


@respx.mock
async def test_wrong_source_id_raises_forbidden(client: PastuhAdsClient) -> None:
    """source_id не соответствует ключу → 403 source_not_allowed.

    Например, если вместо целого числа передать публичный идентификатор
    вида src_48 — сервер его не примет, нужен именно числовой id.
    """
    respx.post(f"{BASE}/op-list").mock(
        return_value=httpx.Response(
            403, json={"status": "error", "error": "source_not_allowed",
                       "message": "Источник трафика не соответствует API-ключу."})
    )
    with pytest.raises(ForbiddenError) as exc_info:
        await client.get_op_list(telegram_user_id=1, source_id=999)
    await client.aclose()
    assert exc_info.value.error_code == "source_not_allowed"


@respx.mock
async def test_mark_shown(client: PastuhAdsClient) -> None:
    respx.post(f"{BASE}/op-sessions/99/shown").mock(
        return_value=httpx.Response(200, json={"status": "ok", "op_session_id": 99, "session_status": "shown"})
    )
    result = await client.mark_shown(99)
    await client.aclose()
    assert result.session_status == "shown"


@respx.mock
async def test_check_session(client: PastuhAdsClient) -> None:
    respx.post(f"{BASE}/op-sessions/99/check").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok", "op_session_id": 99, "session_status": "completed",
                "access_allowed": True, "requested_slots": 1, "filled_slots": 1,
                "expires_at": None, "tasks": [], "incomplete_tasks": [],
                "missing_channels": [],
            },
        )
    )
    result = await client.check_session(99)
    await client.aclose()
    assert result.access_allowed is True


@respx.mock
async def test_get_session_status(client: PastuhAdsClient) -> None:
    respx.get(f"{BASE}/op-sessions/99/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok", "op_session_id": 99, "session_status": "shown",
                "access_allowed": False, "requested_slots": 1, "filled_slots": 1,
                "expires_at": "2026-09-27T21:00:00+00:00",
                "tasks": [], "incomplete_tasks": [
                    {"task_id": 11, "slot": 1, "advertised_resource_id": 3,
                     "target_type": None, "action_type": "subscription_confirmed",
                     "title": "Ch", "username": "ch", "url": "https://t.me/ch",
                     "instruction_text": None, "action_button_text": None,
                     "verification_hint": None, "status": "shown",
                     "verification_status": "pending"}
                ],
            },
        )
    )
    result = await client.get_session_status(99)
    await client.aclose()
    assert result.access_allowed is False
    assert result.incomplete_tasks[0].verification_status == "pending"


@respx.mock
async def test_register_click(client: PastuhAdsClient) -> None:
    route = respx.post(f"{BASE}/tasks/11/click").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "task_id": 11, "task_status": "clicked", "verification_status": "pending"})
    )
    result = await client.register_click(11, telegram_user_id=555001)
    await client.aclose()

    import json
    sent = json.loads(route.calls[0].request.content)
    assert sent["telegram_user_id"] == 555001
    assert result.task_status == "clicked"


@respx.mock
async def test_register_callback_click(client: PastuhAdsClient) -> None:
    route = respx.post(f"{BASE}/tasks/11/callback-click").mock(
        return_value=httpx.Response(
            200, json={
                "status": "ok", "task_id": 11, "task_status": "clicked",
                "verification_status": "confirmed", "charged": True,
                "target_url": "https://t.me/advertised_bot?start=pa_11"})
    )
    result = await client.register_callback_click(11, telegram_user_id=555001)
    await client.aclose()

    import json
    sent = json.loads(route.calls[0].request.content)
    assert sent["telegram_user_id"] == 555001
    assert result.charged is True
    assert result.target_url == "https://t.me/advertised_bot?start=pa_11"


@respx.mock
async def test_register_callback_click_duplicate_not_charged(client: PastuhAdsClient) -> None:
    respx.post(f"{BASE}/tasks/11/callback-click").mock(
        return_value=httpx.Response(
            200, json={
                "status": "ok", "task_id": 11, "task_status": "clicked",
                "verification_status": "confirmed", "charged": False,
                "target_url": "https://t.me/advertised_bot?start=pa_11"})
    )
    result = await client.register_callback_click(11, telegram_user_id=555001)
    await client.aclose()

    assert result.charged is False


@respx.mock
async def test_confirm_bot_start(client: PastuhAdsClient) -> None:
    route = respx.post(f"{BASE}/bot-start/confirm").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "event_id": 42, "is_unique": True})
    )
    result = await client.confirm_bot_start(
        telegram_user_id=555001,
        payload="bs1.abc.def.ghi.jkl.mno.sig",
        bot_username="mybot",
    )
    await client.aclose()

    import json
    sent = json.loads(route.calls[0].request.content)
    assert sent == {
        "telegram_user_id": 555001,
        "payload": "bs1.abc.def.ghi.jkl.mno.sig",
        "bot_username": "mybot",
    }
    assert result.is_unique is True


@respx.mock
async def test_confirm_bot_start_duplicate(client: PastuhAdsClient) -> None:
    respx.post(f"{BASE}/bot-start/confirm").mock(
        return_value=httpx.Response(
            200,
            json={"status": "ok", "event_id": 43, "is_unique": False,
                  "message": "Повторный start — начисление не дублируется."},
        )
    )
    result = await client.confirm_bot_start(
        telegram_user_id=555001, payload="bs1.x", telegram_bot_id=99001)
    await client.aclose()
    assert result.is_unique is False


async def test_confirm_bot_start_requires_bot_ref(client: PastuhAdsClient) -> None:
    with pytest.raises(ValueError):
        await client.confirm_bot_start(telegram_user_id=1, payload="bs1.x")
    await client.aclose()


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------


@respx.mock
async def test_401_raises_authentication_error(client: PastuhAdsClient) -> None:
    respx.get(f"{BASE}/me").mock(
        return_value=httpx.Response(
            401, json={"status": "error", "error": "unauthorized",
                       "message": "API-ключ не передан или недействителен."})
    )
    with pytest.raises(AuthenticationError) as exc_info:
        await client.get_me()
    await client.aclose()

    assert exc_info.value.status_code == 401
    assert exc_info.value.error_code == "unauthorized"


@respx.mock
async def test_403_raises_forbidden_error(client: PastuhAdsClient) -> None:
    respx.post(f"{BASE}/op-list").mock(
        return_value=httpx.Response(
            403, json={"status": "error", "error": "source_not_allowed",
                       "message": "Источник трафика не соответствует API-ключу."})
    )
    with pytest.raises(ForbiddenError) as exc_info:
        await client.get_op_list(telegram_user_id=1)
    await client.aclose()
    assert exc_info.value.error_code == "source_not_allowed"


@respx.mock
async def test_404_raises_not_found(client: PastuhAdsClient) -> None:
    respx.get(f"{BASE}/op-sessions/777/status").mock(
        return_value=httpx.Response(404, json={"status": "error", "message": "Not found."})
    )
    with pytest.raises(NotFoundError):
        await client.get_session_status(777)
    await client.aclose()


@respx.mock
async def test_422_raises_validation_error(client: PastuhAdsClient) -> None:
    respx.post(f"{BASE}/bot-start/confirm").mock(
        return_value=httpx.Response(
            422, json={"status": "error", "error": "ignored",
                       "message": "Start не засчитан.",
                       "details": {"event_id": 44}})
    )
    with pytest.raises(ValidationError) as exc_info:
        await client.confirm_bot_start(1, "bad-payload", bot_username="b")
    await client.aclose()
    assert exc_info.value.error_code == "ignored"
    assert exc_info.value.details == {"event_id": 44}


@respx.mock
async def test_429_raises_rate_limit(client: PastuhAdsClient) -> None:
    respx.get(f"{BASE}/me").mock(return_value=httpx.Response(429, json={"message": "Too many."}))
    with pytest.raises(RateLimitError):
        await client.get_me()
    await client.aclose()


@respx.mock
async def test_timeout_raises_and_retries(client: PastuhAdsClient) -> None:
    calls = 0

    def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("timed out", request=request)

    respx.get(f"{BASE}/me").mock(side_effect=side_effect)
    with pytest.raises(ApiTimeoutError):
        await client.get_me()
    await client.aclose()
    assert calls == 3  # 1 + 2 retries (GET — безопасен)


@respx.mock
async def test_503_retries_then_succeeds(client: PastuhAdsClient) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, json={"status": "error", "message": "down"})
        return httpx.Response(200, json={"status": "ok", "partner": {}, "source": {}, "payouts": {}})

    respx.get(f"{BASE}/me").mock(side_effect=handler)
    me = await client.get_me()
    await client.aclose()
    assert attempts == 3
    assert me.partner.id == 0


@respx.mock
async def test_non_idempotent_post_not_retried(client: PastuhAdsClient) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"status": "error", "message": "down"})

    respx.post(f"{BASE}/payouts").mock(side_effect=handler)
    with pytest.raises(ServerError):
        await client.request_payout(100.0)
    await client.aclose()
    assert calls == 1  # payout НЕ ретраится


@respx.mock
async def test_api_key_never_in_exception(client: PastuhAdsClient) -> None:
    respx.get(f"{BASE}/me").mock(
        return_value=httpx.Response(
            401, json={"status": "error", "error": "unauthorized",
                       "message": f"key {API_KEY} echoed"})  # даже если сервер отразит
    )
    try:
        await client.get_me()
        pytest.fail("expected AuthenticationError")
    except AuthenticationError as exc:
        # сообщение сервера может содержать что угодно, но repr/args
        # не должны содержать Authorization-заголовок
        assert "Bearer" not in str(exc)
        assert "Authorization" not in str(exc)
    await client.aclose()
