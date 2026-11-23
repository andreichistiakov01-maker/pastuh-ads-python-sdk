"""Пример интеграции Pastuh Ads в aiogram 3.x бота.

Показывает реальный OP-flow для источника трафика (partner source):

  1. Пользователь пишет /start или команду контента.
  2. Бот вызывает POST /op-list → получает задания (подписки/боты).
  3. Бот показывает задания инлайн-кнопками → POST /shown.
  4. Клик по кнопке задания → POST /tasks/{id}/click (опционально, но учитывается).
  5. Кнопка «Проверить» → POST /check → если access_allowed — открываем контент,
     иначе показываем невыполненные задания.
  6. /start с deep-link payload bs1.* → POST /bot-start/confirm
     (ТОЛЬКО если ваш бот — рекламируемый ресурс в режиме server_callback).

Установка: pip install aiogram>=3.0 httpx
Запуск:    BOT_TOKEN=... python examples/aiogram3_bot.py

Состояние op_session_id хранится в памяти ради простоты —
в проде используйте FSM-storage / БД.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram import Bot, Dispatcher, F  # noqa: E402
from aiogram.filters import Command, CommandObject, CommandStart  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from pastuh_ads import (  # noqa: E402
    PastuhAdsClient,
    PastuhAdsError,
    ValidationError,
    user_observation_from_user,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]

ads = PastuhAdsClient(
    base_url=os.environ["PASTUH_ADS_API_URL"],
    api_key=os.environ["PASTUH_ADS_API_KEY"],
    source_id=int(os.environ["PASTUH_ADS_SOURCE_ID"]),
    placement_id=int(os.environ["PASTUH_ADS_PLACEMENT_ID"]),
)

# Telegram username вашего бота — нужен для bot-start/confirm,
# если бот сам является рекламируемым ресурсом (server_callback mode).
BOT_USERNAME = os.environ.get("BOT_USERNAME")

dp = Dispatcher()

# user_id -> op_session_id (в проде — FSM storage / Redis / БД)
_user_sessions: dict[int, int] = {}


def op_keyboard(op_list) -> InlineKeyboardMarkup:
    """Кнопки заданий из op-list + fallback Premium-верификации + проверка.

    bot_tracked_click с open_mode="callback" → callback-кнопка
    (нажатие фиксируется через register_callback_click, затем нативная
    t.me-кнопка «Перейти в бота»). Остальные → обычные url-кнопки.
    """
    rows = []
    for task in op_list.tasks:
        text = task.action_button_text or f"Задание {task.slot}"
        if task.open_mode == "callback" and task.cta_callback_data:
            rows.append([InlineKeyboardButton(text=text, callback_data=task.cta_callback_data)])
        elif task.url:
            rows.append([InlineKeyboardButton(text=text, url=task.url)])
    # Premium V2 fallback: если is_premium недостоверен и claim не принят —
    # показываем ссылку подтверждения через сервисный бот платформы.
    if op_list.premium_verification:
        rows.append([InlineKeyboardButton(
            text="💎 Есть задания для Telegram Premium — подтвердить аккаунт",
            url=op_list.premium_verification.url,
        )])
    rows.append([InlineKeyboardButton(
        text="Проверить выполнение",
        callback_data="op_check",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_op_block(message: Message, telegram_user_id: int) -> None:
    """Запросить OP-лист, показать задания, отметить показ."""
    try:
        op_list = await ads.get_op_list(
            telegram_user_id=telegram_user_id,
            user_language=message.from_user.language_code,
            # Callback-CTA: декларируем, что умеем новый флоу для
            # bot_tracked_click. Уберите строку — и задания вернутся
            # на legacy /r/{token} url-кнопках (полная совместимость).
            capabilities=["callback_cta"],
            # Premium SDK: is_premium приходит в самом апдейте — zero-click.
            # Принимается сервером только при trusted_premium_observer;
            # иначе в ответе premium_claim.status == "ignored_untrusted" и
            # пользователь получит fallback-кнопку premium_verification.
            user_observation=user_observation_from_user(message.from_user),
        )
    except PastuhAdsError as exc:
        # Ключ/источник/плейсмент не настроены — не ломаем бота.
        await message.answer(f"Сервис временно недоступен ({exc.error_code or exc.status_code})")
        return

    _user_sessions[telegram_user_id] = op_list.op_session_id

    if not op_list.tasks:
        if op_list.premium_verification:
            await message.answer(
                "Для вас могут быть задания для Telegram Premium — подтвердите аккаунт и обновите список.",
                reply_markup=op_keyboard(op_list),
            )
            return
        await message.answer("Заданий пока нет — контент открыт.")
        return

    first = op_list.tasks[0]
    text = first.instruction_text or "Выполните задания, чтобы продолжить:"
    await message.answer(text, reply_markup=op_keyboard(op_list))

    # Отмечаем показ (идемпотентно — повтор безопасен).
    try:
        await ads.mark_shown(op_list.op_session_id)
    except PastuhAdsError:
        pass  # показ не отмечен — не критично для пользователя


@dp.message(CommandStart(deep_link=True))
async def on_start_deeplink(message: Message, command: CommandObject) -> None:
    """/start <payload> — deep-link из рекламного задания."""
    payload = (command.args or "").strip()

    # Подтверждаем ТОЛЬКО подписанные payload bs1.* —
    # обычный /start никогда не подтверждает рекламное действие.
    if payload.startswith("bs1.") and BOT_USERNAME:
        try:
            result = await ads.confirm_bot_start(
                telegram_user_id=message.from_user.id,
                payload=payload,
                bot_username=BOT_USERNAME,
            )
            if result.is_unique:
                pass  # старт засчитан — при желании залогируйте event_id
        except ValidationError as exc:
            if exc.error_code == "ignored":
                pass  # невалидный/чужой payload — просто обычный /start
        except PastuhAdsError:
            pass

    await message.answer("Добро пожаловать!")
    await send_op_block(message, message.from_user.id)


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    """Обычный /start — БЕЗ вызова bot-start/confirm."""
    await message.answer("Добро пожаловать!")
    await send_op_block(message, message.from_user.id)


@dp.message(Command("content"))
async def on_content(message: Message) -> None:
    """Точка входа в контент — OP-гейт."""
    await send_op_block(message, message.from_user.id)


@dp.callback_query(F.data == "op_check")
async def on_check(callback: CallbackQuery) -> None:
    """«Проверить выполнение» → POST /check."""
    user_id = callback.from_user.id
    session_id = _user_sessions.get(user_id)
    if session_id is None:
        await callback.answer("Сессия не найдена — отправьте /content")
        return

    try:
        status = await ads.check_session(session_id)
    except PastuhAdsError as exc:
        await callback.answer(f"Ошибка проверки ({exc.error_code or exc.status_code})", show_alert=True)
        return

    if status.access_allowed:
        await callback.message.edit_text("Все задания выполнены — контент открыт!")
        # ← здесь выдача вашего контента
    else:
        missing = ", ".join(
            t.title or f"#{t.task_id}" for t in status.incomplete_tasks
        ) or "задания"
        await callback.answer(f"Ещё не выполнено: {missing}", show_alert=True)


# --- Callback-CTA: bot_tracked_click (оплачиваемое нажатие) ----------------
# Согласованный флоу: пользователь жмёт «Открыть бота» в OP → ваш бот
# ловит callback_query → register_callback_click фиксирует биллинг на
# бэкенде → отвечаете на callback БЕЗ url → отправляете сообщение с
# нативной url-кнопкой «Перейти в бота» на response.target_url.
#
# НЕ используйте callback.answer(url=...) для перехода в другого бота —
# Telegram отклоняет такой url ошибкой URL_INVALID. Повторный callback
# и второй тап по t.me-кнопке начисления не создают (charged=False).
@dp.callback_query(F.data.startswith("op:open:"))
async def on_tracked_click(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[-1])
    try:
        result = await ads.register_callback_click(task_id, callback.from_user.id)
    except ValidationError:
        await callback.answer("Сессия заданий истекла — отправьте /content", show_alert=True)
        return
    except PastuhAdsError:
        await callback.answer()
        return

    await callback.answer()
    if result.target_url:
        await callback.message.answer(
            "После выполнения нажмите «Проверить».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Перейти в бота", url=result.target_url)],
                [InlineKeyboardButton(text="✅ Проверить", callback_data="op_check")],
            ]),
        )


# --- Опционально: регистрация клика по не-tracked заданию -------------------
# Для каналов/подписок телеметрия перехода фиксируется так (не биллинг):
#
# @dp.callback_query(F.data.startswith("op_click:"))
# async def on_task_click(callback: CallbackQuery) -> None:
#     task_id = int(callback.data.split(":", 1)[1])
#     try:
#         await ads.register_click(task_id, callback.from_user.id)
#     except PastuhAdsError:
#         pass
#     await callback.answer(url=<task.url>)  # url той же платформы — ок


async def main() -> None:
    bot = Bot(BOT_TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await ads.aclose()


if __name__ == "__main__":
    asyncio.run(main())
