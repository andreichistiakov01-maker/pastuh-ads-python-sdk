# Интеграция Pastuh Ads с Python-ботом

Я собрал небольшой Python-клиент для работы с Partner API Pastuh Ads —
его можно взять за основу для подключения вашего Telegram-бота.
Ниже написано, какие значения взять из кабинета и куда их подставить.

- **Python**: 3.11+
- **HTTP-клиент**: `httpx` (async)
- **Зависимости**: только `httpx` для клиента; `aiogram 3.x` — для примера бота

---

## 1. Что нужно из кабинета

Для работы Partner API источник должен быть подключён в режиме
**API-интеграции** (не Hosted Bot) и иметь статус **Approved**.

Понадобятся три значения:

| Значение | Где взять |
|---|---|
| API-ключ `opk_...` | Кабинет → Источники → ваш источник → «Выпустить API-ключ» (или Кабинет → API-ключи). Показывается **один раз** — сразу скопируйте. |
| Source ID | Страница источника → поле **«API Source ID»**. Это просто число, например `48`. |
| Placement ID | Там же на странице источника — поле **«Placement ID»**, или раздел Кабинет → Плейсменты (у каждого плейсмента есть ID с кнопкой «Копировать»). |

### Про `src_48`

На странице источника есть ещё поле **«Публичный идентификатор»** вида
`src_48`. Это просто отображаемая метка — `src_` плюс число.
В API оно **не работает**: `source_id` принимает только само число
(`48`), без префикса. Если передать `src_48`, получите `403 source_not_allowed`.

Все три значения можно не прописывать заранее — плейсменты
также отдаёт endpoint `GET /placements` (см. раздел 8.5 ниже), а
`source.id` возвращается в ответе `GET /me`.

## 2. Где взять API-ключ

Кабинет → Источники → ваш источник → «Выпустить API-ключ»
(или сразу раздел «API-ключи»). Ключ показывается один раз сразу
после выпуска — в базе хранится только хэш, восстановить его потом
нельзя, только перевыпустить.

Ключ привязан к конкретному источнику: `source_id` в запросах обязан
совпадать с источником ключа, иначе будет `403 source_not_allowed`.

## 3. Где взять Source ID

Страница источника в кабинете → поле **«API Source ID»** — целое число,
например `48`. Не путайте с публичным идентификатором `src_48`:
для API нужен именно числовой ID.

## 3.5. Где взять Placement ID

Два способа:

- Кабинет → страница источника → поле **«Placement ID»**
  (показываются все плейсменты источника), или раздел
  **Плейсменты** — у каждого плейсмента ID с кнопкой «Копировать».
- Из кода — `await client.list_placements()` вернёт список
  плейсментов этого источника; у каждого есть `id`. Подробности —
  в разделе 8.5.

## 4. Установка

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt  # только httpx
```

Для примера aiogram-бота:

```bash
pip install aiogram
# или сразу всё для разработки и тестов:
pip install -r requirements-dev.txt
```

## 5. .env

```bash
cp .env.example .env
```

```env
PASTUH_ADS_API_URL=https://pastuhads.com/api/partner/v1
PASTUH_ADS_API_KEY=opk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
PASTUH_ADS_SOURCE_ID=50
PASTUH_ADS_PLACEMENT_ID=1
```

`.env` **не коммитить** — в нём реальный ключ.

## 6. Проверка подключения

```bash
python examples/basic_usage.py
```

Скрипт вызовет `GET /me` и напечатает партнёра, источник и баланс,
затем запросит OP-лист для тестового `telegram_user_id`.

Минимальный код:

```python
import asyncio, os
from pastuh_ads import PastuhAdsClient

async def main():
    async with PastuhAdsClient(
        base_url=os.environ["PASTUH_ADS_API_URL"],
        api_key=os.environ["PASTUH_ADS_API_KEY"],
        source_id=int(os.environ["PASTUH_ADS_SOURCE_ID"]),
        placement_id=int(os.environ["PASTUH_ADS_PLACEMENT_ID"]),
    ) as client:
        me = await client.get_me()
        print(me.partner.id, me.source.username)

asyncio.run(main())
```

## 7. Получение OP (список заданий)

`POST /op-list` открывает или **переиспользует** активную OP-сессию
пользователя и возвращает задания (подписки/боты):

```python
op_list = await client.get_op_list(
    telegram_user_id=message.from_user.id,   # обязательное поле
    user_language="ru",                       # опционально
)
# op_list.op_session_id — ID сессии, сохраните его для пользователя
# op_list.tasks — задания: task_id, title, url, action_type, ...
# op_list.expires_at — дедлайн сессии (ISO-8601)
```

Поля `tasks[]`: `task_id`, `slot`, `action_type`, `title`, `username`,
`url` (для `bot_start_confirmed` — deep-link `t.me/bot?start=bs1...`),
`instruction_text`, `action_button_text`, `tracking_token` (только для
`bot_start_confirmed`).

## 8. Отметка показа

Когда показали задания пользователю — отметьте показ:

```python
await client.mark_shown(op_list.op_session_id)
```

Повторный вызов безопасен — сервер просто вернёт текущий статус.

## 8.5. Список плейсментов через API

Если не хочется лезть в кабинет за `placement_id`, плейсменты
можно получить прямо через API:

```python
placements = await client.list_placements()
for p in placements:
    print(p.id, p.name, p.status)   # active / inactive / archived
```

Endpoint отдаёт только плейсменты источника, к которому привязан
ваш ключ — чужие не показывает. Для op-list подходит плейсмент
со `status="active"`.

## 9. Проверка выполнения

Когда пользователь нажал «Проверить»:

```python
status = await client.check_session(op_list.op_session_id)
if status.access_allowed:
    ...  # выдать контент
else:
    ...  # status.incomplete_tasks — невыполненные задания
```

`check` запускает серверную верификацию (подписка/старт бота).
`GET /status` возвращает последний известный статус **без** повторной
проверки — для фонового опроса.

Истёкшая сессия → `409 session_expired`: запросите новый op-list.

Клик по заданию (опционально, учитывается в ротации):

```python
await client.register_click(task.task_id, telegram_user_id=user_id)
```

## 9.5. Callback-CTA для bot_tracked_click (SDK ≥1.1)

`bot_tracked_click` — tokenless-задание, где оплачиваемое событие —
первое подтверждённое нажатие кнопки «Запустить бота» в вашем OP, а не
сам переход в рекламируемый бот.

Два режима ответа `op-list`:

| `task.open_mode` | Что делать |
|---|---|
| `"url"` (по умолчанию) | Покажите обычную url-кнопку на `task.url` (server-hop `/r/{token}`) — как раньше. |
| `"callback"` | Покажите callback-кнопку `task.cta_callback_data`; по callback_query вызовите `register_callback_click`, ответьте на callback **без** `url` и отправьте сообщение с нативной url-кнопкой «Запустить бота» на `result.target_url`. |

Чтобы сервер вернул `open_mode="callback"`, задекларируйте
capability при запросе op-list:

```python
op_list = await client.get_op_list(
    telegram_user_id=user_id,
    capabilities=["callback_cta"],
)
```

```python
@dp.callback_query(F.data == task.cta_callback_data)
async def on_open(callback):
    result = await client.register_callback_click(
        task.task_id, telegram_user_id=callback.from_user.id)
    await callback.answer()  # БЕЗ url — переход в чужой бот через
    # answerCallbackQuery(url) Telegram отклоняет (URL_INVALID).
    await callback.message.answer(
        "После выполнения нажмите «Проверить».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Запустить бота", url=result.target_url),
        ]]))
```

Идемпотентно: повторный callback вернёт `charged=false`, повторный тап
по t.me-кнопке оплачиваемым событием не является. Полная реализация —
в `examples/aiogram3_bot.py`.

## 9.6. Интерактивный OP-блок (SDK ≥1.2)

`InteractiveOpBlock` — готовый движок «одного OP-сообщения»: клавиатура
сокращается по мере выполнения, tokenless-destination живёт во временном
слоте (~15 с) и переоткрывается кнопкой «🔁 Запустить бота» без повторной
оплаты.

```python
from pastuh_ads import InteractiveOpBlock, PastuhAdsClient

block = InteractiveOpBlock(
    ads,
    send_message=tg_send,        # async (chat_id, text, rows) -> message_id
    edit_markup=tg_edit_markup,  # async (chat_id, message_id, rows) -> None
    edit_text=tg_edit_text,      # async (chat_id, message_id, text, rows)
    delete_message=tg_delete,    # async (chat_id, message_id) -> None
    slot_ttl_seconds=15.0,
)

# /start — одно сообщение со всеми заданиями:
await block.open(message.from_user.id, message.chat.id, user=message.from_user)

# callback_query вашего бота:
handled = await block.handle_callback(
    telegram_user_id=cq.from_user.id,
    chat_id=cq.message.chat.id,
    data=cq.data,               # paop:open:*, paop:reopen:*, paop:check
    answer=cq.answer,
)
```

Контракт кнопок совпадает с hosted-версией:

| action_type | Кнопка |
|---|---|
| `subscription_confirmed` / `join_request_submitted` / `bot_start_confirmed` / `bot_new_user_verified` | прямой `url` (one-tap t.me) |
| `bot_tracked_click` / `bot_click` | `paop:open:<task_id>` → серверный transition → TTL-слот |
| выполненный paid-click | `paop:reopen:<task_id>` — слот без финансового вызова |
| `paop:check` | legacy-клавиатуры SDK <1.3: «✅ Проверить выполнение» → `POST /check` (роутинг сохранён; в новой клавиатуре кнопки нет) |

**Event-driven проверка (UX v2, SDK ≥1.3)**: кнопки «Проверить» в новой
клавиатуре нет — подтверждение заданий делает сервер. Блок подтягивает
результат throttled `POST /check` на наблюдаемых событиях пользователя
(`open()`, любой `paop:*` callback; не чаще `check_min_interval_seconds`,
default 20с) и опциональным bounded fallback-циклом
`reconcile_interval_seconds` (до expiry/240 итераций, по умолчанию OFF).

- **Negotiation**: `block.open()` сам передаёт
  `capabilities=['callback_cta','interactive_op']`. Если сервер не
  принял `interactive_op` (флаг `external_op_block_interactive_enabled`
  выключен), `bot_tracked_click` деградирует в legacy `url`-режим —
  ответ побайтово совместим с SDK 1.1.0. Принятые capabilities видны в
  `op_list.negotiated_capabilities`.
- **Биллинг**: финансовый вердикт — только из ответа API (`charged`,
  `task_status`). Reopen не делает ни одного запроса к API — повторная
  оплата исключена по построению.
- **Stale UI**: монотонная ревизия рендера — устаревший апдейт не
  перезапишет свежую клавиатуру; выполненная кнопка не воскресает.
- **Ошибки Telegram**: `message is not modified` — no-op; удалённое
  основное сообщение пересоздаётся один раз; временные сбои — bounded
  retry (backoff, `max_render_retries`), без шторма.
- **Рестарт**: `dump_state()` → сохранить dict → `restore_state()` +
  `resume()` после запуска — клавиатуры перестраиваются из `status`.
- **Мягкий sync**: опциональный `reconcile_interval_seconds` — фон-refresh
  через `GET /status` пока сессия жива (по умолчанию выключен; это НЕ
  polling всех сессий каждую секунду).

Полный рабочий пример — `examples/aiogram3_interactive_op.py`.

## 9.7. Gender-claim (SDK ≥1.2)

`user_observation_from_user()` умеет возвращать `gender`
(`male`/`female`/`unknown`) — локальный вердикт оригинального
классификатора пола (pol_module v1.0) по `first_name`/`last_name`/
`username` из апдейта. Сырые поля никуда не отправляются.

**Важно (Sprint 3C):** это явный opt-in. По умолчанию
`include_gender=False` — классификатор даже не загружается, ответ
побайтово как у SDK 1.1.0 (`{"is_premium": ...}`). Включайте только
после того, как платформа выдала `trusted_gender_observer` и включила
`gender_segmentation_enabled`.

```python
# По умолчанию — только is_premium, классификатор не вызывается:
obs = user_observation_from_user(message.from_user)
# {"is_premium": False}

# Явный opt-in — только по решению оператора:
obs = user_observation_from_user(message.from_user, include_gender=True)
# {"is_premium": False, "gender": "female"}
```

Сервер применяет claim **только** для партнёров с флагом
`trusted_gender_observer` и только при включённой
`gender_segmentation_enabled`. Итог — в `response.gender_claim.status`:
`applied` / `disabled` / `ignored_untrusted` / `ignored_invalid` /
`not_sent`. Сам себе trusted-статус SDK выдать не может — флаг ставит
админ платформы.

## 10. /start confirmation

Если **ваш бот** — рекламируемый ресурс (на него покупают старты,
режим `server_callback`), пользователь приходит по deep-link
`https://t.me/your_bot?start=bs1....`.

```python
# В обработчике /start с аргументом:
if payload.startswith("bs1."):
    result = await client.confirm_bot_start(
        telegram_user_id=user.id,
        payload=payload,
        bot_username="your_bot",   # или telegram_bot_id=<int>
    )
    # result.is_unique=True — засчитан; False + status="duplicate" — повтор
```

**Важно:**

- `payload` — это подписанный tracking token `bs1.*`, а не произвольная строка.
- Обычный `/start` без payload **никогда не подтверждается** — сервер
  вернёт `422 ignored` (событие записывается как проигнорированное).
- Нужно передать `bot_username` ИЛИ `telegram_bot_id` для поиска integration.
- Операция идемпотентна: повтор → `duplicate`, двойного начисления нет.

Ошибки endpoint'а: `404 integration_not_found`, `403 wrong_mode`
(integration не в режиме server_callback), `422 ignored`.

## 11. Работа с aiogram 3

`examples/aiogram3_bot.py` — готовый каркас:

- `/start` без payload → обычная логика + OP-гейт.
- `/start bs1.*` → `confirm_bot_start` + OP-гейт.
- Команда контента → `get_op_list` → инлайн-кнопки `task.url` → `mark_shown`.
- Callback «Проверить выполнение» → `check_session` → контент или список
  невыполненных.

Перенесите хендлеры в свой проект, заменив `_user_sessions` (dict в памяти)
на FSM-storage или БД.

## 11.5. Premium-задания: автоматический сбор статуса (zero-click)

Ваш бот уже получает `is_premium` в каждом Telegram-апдейте —
в `message.from_user.is_premium` (aiogram) или сыром `from` update.
SDK пересылает его платформе как `user_observation`, и Premium-задания
выдаются **без единого дополнительного действия от пользователя**:

```python
from pastuh_ads import user_observation_from_user

op_list = await client.get_op_list(
    telegram_user_id=message.from_user.id,
    user_observation=user_observation_from_user(message.from_user),
)
```

`user_observation_from_user` возвращает `None`, если объект не несёт
данных о статусе — тогда поле просто не отправляется.

### Модель доверия (обязательно к пониманию)

- `user_observation` — это **заявление партнёра**, а не криптографическое
  доказательство Telegram. API-ключ удостоверяет вас, а не данные.
- Claim принимается только если админ платформы включил для вас флаг
  `trusted_premium_observer`. Запросите это у владельца платформы после
  того, как интеграция заработает.
- Итог обработки приходит в ответе: `op_list.premium_claim.status` —
  `applied` / `ignored_untrusted` / `ignored_trusted_observation` / `not_sent`.
- Если claim не принят или статус недостоверен, в ответе появляется
  `op_list.premium_verification.url` — покажите её пользователю
  как кнопку (fallback-подтверждение через сервисный бот платформы).
  Пример рендеринга — в `examples/aiogram3_bot.py`.
- Альтернативный верифицированный канал: `await client.verify_premium(
  telegram_user_id, init_data)` — подписанный initData из Mini App
  сервисного бота платформы.

### Чего нельзя делать

- **Не отправляйте `is_premium`, которого у вас нет.** Синтетическая
  разметка пользователей Premium — это фрод против рекламодателей:
  ваши claims помечаются `premium_source=partner_claim:<id>` и видны
  в аудите, у них укороченный срок жизни, а злоупотребление заканчивается
  отзывом флага и модерацией источника.
- Не дублируйте значение в `meta.user_metadata.is_premium` — там оно
  никогда не было и не будет доверенным (поле принимается ради
  совместимости, но в наблюдения не пишется).

## 12. Ошибки API

Все ошибки приходят как исключения `pastuh_ads` с полями
`status_code`, `error_code`, `message`, `details`. API-ключ
в исключение не попадает — его можно спокойно логировать вместе
с остальной ошибкой.

Что значат коды простым языком:

| Код | Что значит | Что проверить |
|---|---|---|
| 401 | Неверный или отозванный API-ключ | `PASTUH_ADS_API_KEY` |
| 403 | Ключ не имеет доступа к этому `source_id` / `placement_id` | Что ID совпадают со значениями из кабинета; что не подставили `src_NN` вместо числа |
| 404 | Сессия, задание или интеграция не найдены | Что `op_session_id`/`task_id` из вашего же op-list |
| 409 | OP-сессия истекла | Запросите новый op-list для пользователя |
| 422 | Ошибка в данных запроса | Тело запроса; для bot-start — что payload валидный `bs1.*` |
| 429 | Слишком много запросов | Притормозите и повторите позже |
| 5xx | Временная ошибка сервера | Повторите запрос чуть позже |

Отдельно: `ApiTimeoutError` — запрос не уложился в таймаут,
`ApiConnectionError` — сетевая проблема (DNS, refused и т.п.).

```python
from pastuh_ads import PastuhAdsError, ConflictError

try:
    status = await client.check_session(session_id)
except ConflictError:
    # сессия истекла → запросить новый op-list
except PastuhAdsError as e:
    log.warning("Pastuh Ads error: %s", e)  # ключ в e не попадает
```

## 13. Безопасность

- API-ключ передаётся **только** в заголовке `Authorization: Bearer`.
- Ключ и секреты никогда не логируйте (клиент их в исключения не включает).
- `POST /payouts` — НЕ идемпотентен: не делайте повторов слепых ретраев
  (клиент его не ретраит).
- Retry в клиенте — только безопасные: timeout/connect-ошибки и
  502/503/504 для GET и идемпотентных POST (op-list, shown, check, click,
  bot-start/confirm).
- HTTPS обязателен — HTTP base_url не поддерживается продом.

---

## BACKEND GAPS (известные ограничения API)

Что стоит знать про текущее API (проверено по коду):

1. **`source_id` — только целое число.** Публичный идентификатор
   `src_48` из кабинета API не принимает: передавайте `48`.
2. **`GET /placements`** есть и возвращает плейсменты вашего источника
   (`id`, `name`, `type`, `source_id`, `slots_count`, `status`) —
   `placement_id` можно узнать из кода.
3. **Нет idempotency-key** — повторы безопасны только там, где сам
   сервер идемпотентен (таблица ниже). `POST /payouts` — НЕ
   идемпотентен: повтор создаст вторую заявку.
4. **Rate-limit без заголовков** — `429` обрабатывайте и ретрайте
   с паузой, `Retry-After` сервер не отдаёт.
5. **`status` не проверяет заново** — только `check` запускает
   верификацию. `status` годится для отображения, но при принятии
   решения (открыть контент?) вызывайте `check`.

Идемпотентность по endpoint'ам (проверено по коду):

| Endpoint | Повторять можно? |
|---|---|
| `GET /me`, `GET /status`, `GET /placements`, `GET /payouts` | да, это чтение |
| `POST /op-list` | да — вернёт ту же открытую сессию |
| `POST /shown`, `POST /check` | да |
| `POST /tasks/{id}/click` | да — дубликат не создаётся |
| `POST /tasks/{id}/callback-click` | да — `charged=false`, второго начисления нет |
| `POST /bot-start/confirm` | да — вернёт `duplicate`, двойного начисления нет |
| `POST /payouts` | **нет** — создаст вторую заявку на выплату |
| `POST /payouts/{id}/cancel` | повтор вернёт ошибку, если заявка уже не pending |
