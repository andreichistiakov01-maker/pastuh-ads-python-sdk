"""Standalone-пример Partner API БЕЗ нашего SDK — только httpx.

Показывает тот же контракт, что обёрнут в PastuhAdsClient:
- GET  /me                        — проверка ключа и идентичности;
- POST /op-list                   — открытие OP-сессии (идемпотентно по пользователю);
- POST /op-sessions/{id}/shown    — факт показа OP-блока;
- POST /tasks/{task_id}/click     — фиксация клика по заданию.

Premium zero-click: поле ``user_observation`` с ``is_premium`` из
Telegram User-объекта (поле опционально и True-only — отсутствие
поля у полного User означает обычный аккаунт, т.е. false).

Запуск:
    PASTUH_ADS_API_KEY=opk_... PASTUH_ADS_SOURCE_ID=50 \
        python examples/plain_http_no_sdk.py
"""

from __future__ import annotations

import asyncio
import os

import httpx

BASE_URL = os.getenv("PASTUH_ADS_API_URL", "https://pastuhads.com/api/partner/v1")
API_KEY = os.environ["PASTUH_ADS_API_KEY"]
SOURCE_ID = int(os.environ["PASTUH_ADS_SOURCE_ID"])
PLACEMENT_ID = int(os.getenv("PASTUH_ADS_PLACEMENT_ID", "1"))
TG_USER_ID = int(os.getenv("PASTUH_ADS_TEST_TG_USER_ID", "123456789"))

HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}


async def main() -> None:
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers=HEADERS, timeout=httpx.Timeout(10.0),
    ) as http:
        me = (await http.get("/me")).json()
        print("me:", me.get("data", me))

        # user_observation: реальный is_premium из Telegram User.
        # Полный User без поля is_premium → False; частичный объект → поле не шлите.
        user_observation = {"is_premium": False}

        op = (
            await http.post("/op-list", json={
                "source_id": SOURCE_ID,
                "placement_id": PLACEMENT_ID,
                "telegram_user_id": TG_USER_ID,
                "user_observation": user_observation,
            })
        ).json()

        data = op.get("data", op)
        session_id = data.get("op_session_id")
        print("op_session:", session_id, "| premium_claim:", data.get("premium_claim"))
        if data.get("premium_verification"):
            print("fallback premium url:", data["premium_verification"].get("url"))

        # Показ блока — один вызов на сессию (не на каждое задание).
        await http.post(f"/op-sessions/{session_id}/shown")

        for task in data.get("tasks", []):
            tid = task["task_id"]
            click = (await http.post(
                f"/tasks/{tid}/click", json={"telegram_user_id": TG_USER_ID},
            )).json()
            print("task:", tid, "click:", click.get("data", click))


if __name__ == "__main__":
    asyncio.run(main())
