"""Базовый пример: проверка подключения к Pastuh Ads Partner API.

Что делает:
  1. GET /me — проверяет ключ и показывает партнёра/источник/баланс.
  2. POST /op-list — открывает OP-сессию для тестового пользователя
     и выводит список заданий (без побочных финансовых последствий —
     показ НЕ отмечается, клики не регистрируются).

Запуск:
    cp .env.example .env   # заполните ключи
    python examples/basic_usage.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pastuh_ads import PastuhAdsClient, PastuhAdsError  # noqa: E402


def load_env() -> None:
    """Мини-загрузчик .env (без зависимости от python-dotenv)."""
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def main() -> None:
    load_env()

    base_url = os.environ.get("PASTUH_ADS_API_URL")
    api_key = os.environ.get("PASTUH_ADS_API_KEY")
    source_id = os.environ.get("PASTUH_ADS_SOURCE_ID")
    placement_id = os.environ.get("PASTUH_ADS_PLACEMENT_ID")
    test_user = int(os.environ.get("PASTUH_ADS_TEST_TG_USER_ID", "123456789"))

    if not base_url or not api_key:
        sys.exit("Заполните PASTUH_ADS_API_URL и PASTUH_ADS_API_KEY в .env")

    async with PastuhAdsClient(
        base_url=base_url,
        api_key=api_key,
        source_id=int(source_id) if source_id else None,
        placement_id=int(placement_id) if placement_id else None,
    ) as client:
        try:
            me = await client.get_me()
        except PastuhAdsError as exc:
            sys.exit(f"Ошибка подключения: {exc}")

        print(f"Партнёр #{me.partner.id} ({me.partner.status})")
        print(f"Источник #{me.source.id} @{me.source.username} ({me.source.status})")
        print(f"Баланс: {me.partner.available_balance} (всего {me.partner.balance})")
        print(f"Выплат в ожидании: {me.payouts.pending_count}, "
              f"выплачено всего: {me.payouts.paid_total}")
        print()

        # Если PASTUH_ADS_PLACEMENT_ID не задан — попробуем найти
        # первый активный плейсмент через GET /placements.
        placement_id_resolved = client._placement_id
        if placement_id_resolved is None:
            placements = await client.list_placements()
            if placements:
                for p in placements:
                    print(f"  Плейсмент #{p.id}: {p.name or 'без названия'} "
                          f"({p.status}, слотов: {p.slots_count})")
                active = next((p for p in placements if p.status == "active"), None)
                placement_id_resolved = (active or placements[0]).id
                print(f"Используем placement_id={placement_id_resolved} "
                      f"(можно зафиксировать в .env)")
            else:
                print("У источника нет плейсментов — создайте в кабинете "
                      "(Источники → Плейсменты).")
                return

        if client._source_id is None:
            print("PASTUH_ADS_SOURCE_ID не задан — возьмите число из поля "
                  "«API Source ID» на странице источника в кабинете.")
            return

        try:
            op_list = await client.get_op_list(
                telegram_user_id=test_user,
                placement_id=placement_id_resolved,
                user_language="ru",
            )
        except PastuhAdsError as exc:
            sys.exit(f"Ошибка op-list: {exc}")

        print(f"OP-сессия #{op_list.op_session_id}: "
              f"{op_list.filled_slots}/{op_list.requested_slots} слотов, "
              f"истекает {op_list.expires_at}")
        for task in op_list.tasks:
            print(f"  [{task.slot}] {task.title} (@{task.username})")
            print(f"      action={task.action_type} url={task.url}")


if __name__ == "__main__":
    asyncio.run(main())
