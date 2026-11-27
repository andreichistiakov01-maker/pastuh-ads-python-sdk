# Pastuh Ads — Python SDK

Асинхронный Python SDK для **Pastuh Ads Partner API v1**: выдача
OP-заданий (обязательных подписок), интерактивный OP-блок в Telegram,
клики, подтверждения `/start`, Premium-наблюдения.

Полная документация: [README_RU.md](README_RU.md) ·
[CHANGELOG](CHANGELOG.md) · [docs/](docs/)

## Установка

```bash
pip install pastuh-ads
# с примерами для aiogram 3.x:
pip install "pastuh-ads[aiogram]"
```

Python ≥ 3.10. Единственная обязательная зависимость — `httpx`.

## Быстрый старт

### 1. Получите API-ключ

Ключ выдаётся в кабинете Pastuh Ads для вашего источника трафика
(формат `opk_…`). Нужны также числовые `SOURCE_ID` и `PLACEMENT_ID`.

```bash
export PASTUH_ADS_API_KEY=opk_...
export PASTUH_ADS_SOURCE_ID=50
export PASTUH_ADS_PLACEMENT_ID=1
```

### 2. Получение заданий (op-list)

```python
from pastuh_ads import PastuhAdsClient

ads = PastuhAdsClient(
    base_url="https://pastuhads.com/api/partner/v1",
    api_key=os.environ["PASTUH_ADS_API_KEY"],
    source_id=int(os.environ["PASTUH_ADS_SOURCE_ID"]),
    placement_id=int(os.environ["PASTUH_ADS_PLACEMENT_ID"]),
)

op = await ads.get_op_list(telegram_user_id, user=message.from_user)
# op.op_session_id, op.tasks[], op.access_allowed, op.expires_at
```

### 3. Отметка показа — mark_shown

После фактического отображения блока пользователю:

```python
await ads.mark_shown(op.op_session_id)
```

### 4. Интерактивный OP-блок (одно сообщение)

`InteractiveOpBlock` реализует эталонную механику: одно OP-сообщение,
клавиатура сокращается по мере выполнения, временный TTL-слот для
tokenless-бота, reopen-CTA без повторной оплаты.

```python
from pastuh_ads import InteractiveOpBlock

block = InteractiveOpBlock(
    ads,
    send_message=tg_send,        # async (chat_id, text, rows) -> message_id
    edit_markup=tg_edit_markup,  # async (chat_id, message_id, rows)
    edit_text=tg_edit_text,      # async (chat_id, message_id, text, rows)
    delete_message=tg_delete,    # async (chat_id, message_id)
    slot_ttl_seconds=15.0,
    # SDK >= 1.3.2: background adaptive sync is ON by default — completed
    # slots disappear without "Check" or a repeated /start (one batched
    # POST /op-sessions/sync per bot). auto_sync=False restores 1.3.1.
)

# /start → одно сообщение со всеми заданиями
await block.open(telegram_user_id=uid, chat_id=cid, user=message.from_user)

# callback_query → роутинг paop:*
handled = await block.handle_callback(
    telegram_user_id=callback.from_user.id,
    chat_id=callback.message.chat.id,
    data=callback.data,
    answer=callback.answer,
)
```

Callback-data бота:

| data | Действие |
|---|---|
| `paop:open:{task_id}` | Оплачиваемый клик → серверная фиксация → прямое открытие через `answerCallbackQuery(url)` (SDK ≥1.3.4); кнопка исчезает, дополнительных сообщений нет |
| `paop:reopen:{task_id}` | legacy-клавиатуры SDK ≤1.3.3 — reopen оплаченного слота (в ≥1.3.4 reopen-строки не рендерятся) |
| `paop:check` | legacy-клавиатуры SDK <1.3 → POST /check (в новой кнопки нет — проверка event-driven: throttled /check на событиях пользователя + bounded fallback) |

**SDK ≥1.3.4 — single-message UX (default):** выполненные кнопки исчезают,
slot-сообщения («Перейдите по кнопке…») и reopen-строки («🔁 Запустить бота»)
не создаются. Целевой бот открывается нативно одним тапом — aiogram
`callback.answer` поддерживает `url` из коробки, изменений интеграции
не требуется. `single_message_ux=False` восстанавливает поведение 1.3.3.
`diagnostic_hook=(lambda ev: logger.info(ev))` — коррелированная
latency-диагностика (callback.received → api → answer_url → render).

Готовый пример: [examples/aiogram3_interactive_op.py](examples/aiogram3_interactive_op.py).
Без aiogram/SDK — голый aiohttp: [examples/aiohttp_interactive_op.py](examples/aiohttp_interactive_op.py).

### 5. Подтверждение действий

Задания `subscription_confirmed` / `join_request_submitted` /
`bot_start_confirmed` подтверждаются серверными событиями платформы —
переход по ссылке сам по себе ничего не завершает. Кнопка задания
исчезает из OP-блока только после подтверждения (через `/check` или
`GET /status`).

Оплачиваемый клик (`bot_tracked_click`) фиксируется один раз через
`POST /tasks/{id}/callback-click` — идемпотентно (`charged`).

### 6. Ошибки

```python
from pastuh_ads import PastuhAdsError

try:
    await ads.get_op_list(uid)
except PastuhAdsError as exc:
    exc.status_code  # HTTP-статус API
    exc.error_code   # машинный код ошибки (session_expired, …)
    exc.message      # человекочитаемое описание
```

`InteractiveOpBlock` не пробрасывает Telegram-исключения наружу:
временные сбои — ограниченный backoff + ленивое восстановление,
`on_error`-hook для метрик.

### 7. Пережитие рестарта

```python
saved = block.dump_state()      # → в Redis/файл
block.restore_state(saved)      # при старте процесса
await block.resume()            # перечитать status, пересобрать клавиатуры
```

## Обновление

```bash
pip install -U pastuh-ads
```

Миграции между версиями: [docs/MIGRATION_SDK_1.2.0_RU.md](docs/MIGRATION_SDK_1.2.0_RU.md).
`__version__` — `pastuh_ads.__version__`; запросы подписаны
`User-Agent: pastuh-ads/<version>`.

## Разработка

```bash
pip install -e ".[dev]"
pytest
python -m build
```

## Лицензия

Proprietary — условия согласовываются с правообладателем до публикации
отдельной лицензии.
