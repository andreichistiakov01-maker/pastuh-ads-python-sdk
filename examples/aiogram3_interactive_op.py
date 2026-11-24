"""Пример интерактивного OP-блока (SDK 1.2.0) для aiogram 3.x.

Одно OP-сообщение с сокращающейся клавиатурой: кнопки выполненных
заданий исчезают, оплаченные click-задания остаются через reopen-CTA,
tokenless destination живёт во временном слоте (~15с) и переоткрывается
без повторной оплаты.

  1. Пользователь пишет /start → InteractiveOpBlock.open() → op-list с
     capabilities=['callback_cta','interactive_op'] → одно сообщение.
  2. URL-задания (канал/заявка/бот с токеном) — прямые t.me-кнопки.
  3. paop:open:<task_id> — оплачиваемый клик → серверная фиксация →
     TTL-слот с прямой ссылкой → кнопка уходит, остаётся reopen-CTA.
  4. paop:reopen:<task_id> — повторное открытие оплаченного слота
     БЕЗ нового финансового вызова (ноль запросов к Partner API).
  5. paop:check — legacy-кнопка «Проверить» из старых клавиатур (SDK <1.3)
     → POST /check → пересборка. В новых клавиатурах кнопки нет: проверка
     event-driven (throttled /check на событиях + bounded fallback).
  6. dump_state/restore_state/resume — пережитие рестарта процесса.

Установка: pip install "pastuh-ads[aiogram]" (или aiogram>=3.0 + httpx)
Запуск:
    BOT_TOKEN=... PASTUH_ADS_API_URL=... PASTUH_ADS_API_KEY=opk_... \
    PASTUH_ADS_SOURCE_ID=50 PASTUH_ADS_PLACEMENT_ID=7 \
    python examples/aiogram3_interactive_op.py

    # Опциональный gender-claim (Sprint 3C: opt-in, default OFF):
    # PASTUH_ADS_GENDER_CLAIM=1 — только после того, как платформа
    # включила gender_segmentation_enabled и выдала партнёру
    # trusted_gender_observer. Без opt-in классификатор не вызывается.

Состояние блоков — в памяти; в проде сохраняйте dump_state() в Redis.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram import Bot, Dispatcher, F  # noqa: E402
from aiogram.filters import CommandStart  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from pastuh_ads import InteractiveOpBlock, PastuhAdsClient  # noqa: E402

BOT_TOKEN = os.environ["BOT_TOKEN"]

bot = Bot(BOT_TOKEN)
ads = PastuhAdsClient(
    base_url=os.environ["PASTUH_ADS_API_URL"],
    api_key=os.environ["PASTUH_ADS_API_KEY"],
    source_id=int(os.environ["PASTUH_ADS_SOURCE_ID"]),
    placement_id=int(os.environ["PASTUH_ADS_PLACEMENT_ID"]),
)


def _markup(rows: list[list[dict]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(**{k: v for k, v in b.items() if k in ("text", "url", "callback_data")})
            for b in row
        ]
        for row in rows
    ])


# --- Telegram callables для движка (транспортно-нейтральный контракт) ---

async def tg_send(chat_id: int, text: str, rows: list) -> int:
    msg = await bot.send_message(chat_id, text, reply_markup=_markup(rows))
    return msg.message_id


async def tg_edit_markup(chat_id: int, message_id: int, rows: list) -> None:
    await bot.edit_message_reply_markup(
        chat_id=chat_id, message_id=message_id, reply_markup=_markup(rows),
    )


async def tg_edit_text(chat_id: int, message_id: int, text: str, rows: list) -> None:
    await bot.edit_message_text(
        chat_id=chat_id, message_id=message_id, text=text,
        reply_markup=_markup(rows),
    )


async def tg_delete(chat_id: int, message_id: int) -> None:
    await bot.delete_message(chat_id=chat_id, message_id=message_id)


block = InteractiveOpBlock(
    ads,
    send_message=tg_send,
    edit_markup=tg_edit_markup,
    edit_text=tg_edit_text,
    delete_message=tg_delete,
    slot_ttl_seconds=15.0,
)

dp = Dispatcher()


# Gender-claim — opt-in (default OFF): только после OWNER GO платформы
# и выдачи партнёру trusted_gender_observer. Сырые имена/username
# никуда не уходят — в claim попадает только вердикт male/female/unknown.
GENDER_CLAIM = os.environ.get("PASTUH_ADS_GENDER_CLAIM") == "1"


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    # user_observation собирается локально из message.from_user:
    # is_premium всегда; gender — только при явном opt-in.
    await block.open(
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        user=message.from_user,
        include_gender=GENDER_CLAIM,
    )


@dp.callback_query(F.data.startswith("paop:"))
async def on_op_callback(callback: CallbackQuery) -> None:
    handled = await block.handle_callback(
        telegram_user_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        data=callback.data,
        answer=callback.answer,
    )
    if not handled:
        await callback.answer()


async def main() -> None:
    # В проде: восстановить сохранённое состояние до polling —
    # resume() перечитает status и пересоберёт клавиатуры.
    # block.restore_state(saved_dict); await block.resume()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
