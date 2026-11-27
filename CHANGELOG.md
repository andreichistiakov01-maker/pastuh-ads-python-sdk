# Changelog — Pastuh Ads Partner SDK

## 1.3.4 — Single-message UX: positional URL-transform, latency, no artifacts

**Client P0 acceptance fix.** Оба класса «артефактов» устранены: отдельные
slot-сообщения («Перейдите по кнопке и нажмите Запустить») и reopen-строки
(«🔁 Запустить бота») в основной клавиатуре.

### Архитектура UX (single_message_ux=True — default)

**Официальное ограничение Telegram**: ``answerCallbackQuery(url)`` принимает
только deep links к СОБСТВЕННОМУ боту или его игре (core.telegram.org/api/bots/buttons).
Открытие third-party рекламируемого бота через callback answer невозможно —
это верифицировано по официальной документации, а не предположение.

Поэтому UX честно **двухтаповый**:

1. Тап по callback-кнопке — server-side settlement (first-click-wins,
   идемпотентно), toast «Переход засчитан», кнопка **трансформируется на
   исходной позиции** в прямую t.me URL-кнопку — тот же message, та же строка.
2. Тап по URL-кнопке — нативное открытие рекламируемого бота без браузера
   и промежуточных сообщений.

- **Никаких slot-сообщений** — ``_send_slot`` не вызывается ни на одном пути.
- **Никаких reopen-строк** — не рендерятся нигде.
- **Destination-строки живут до expiry**: деньги списаны при первом тапе,
  а бот пользователем ещё не открыт — доступ к оплаченному переходу есть
  финансовое обязательство. Sync не удаляет строку преждевременно;
  completion не уничтожает последний доступный переход (last-slot safety —
  отдельный regression test). Для bot_click строка исчезает после верификации:
  server-side presence доказывает, что доступ уже получен.
- ``try_direct_url_answer=True`` — experimental opt-in: одна попытка
  url-answer (для будущих изменений Telegram или self-referral кейсов);
  отказ переживается безопасно, клавиатура не зависит от результата.

### Latency

- Critical path тапа: settlement API → параллельные answer-toast + render.
  Controlled harness (TG 300ms RTT, API 400ms RTT): **1.003s → 0.702s**,
  sendMessage на тап: 1 → 0. Первый тап в 20-секундном окне: ещё −1 API RTT
  (piggyback /check переведён в фон). Synthetic результат — реальная
  пользовательская задержка измеряется живой приёмкой.
- ``diagnostic_hook``: monotonic T-chain — callback.received →
  open.api_start/end (charged) → render.start/end → open.settled.
  Корреляция session/user/message/task; исключения hook изолированы.

### Финансовая безопасность

- ``open_inflight`` guard: одновременные дублирующие тапы — ровно один
  settlement-запрос (серверная идемпотентность charged=false — последняя линия).
- Settlement OK + Telegram edit failure — retry восстанавливает UI без
  повторного списания (ровно 1 settlement за всю историю сбоя).
- API failure — задание не завершается, destination не раскрывается.
- destination_ids персистятся в dump/restore — перезапуск процесса не
  отнимает оплаченный переход.

### Backward compatibility

- ``single_message_ux=False`` — полное поведение 1.3.3 (TTL-слоты,
  reopen-CTA) сохранено и покрыто тестами. Дампы состояния читаются.
- answer-callables старого вида ``async (text)`` работают без изменений.

### Regression

- **111/111 tests pass**: legacy mode + single-message UX (17 тестов:
  positional transform, last-slot safety, sync-persistence, restart,
  concurrent taps, financial idempotency, screenshot-scenario) + countdown
  acceptance + auto_sync + client.

## 1.3.3 — UI consistency: no resurrected slots, no orphaned CTAs

- **BUG fix (Boom P0)**: вытесненные OP-сообщения больше не оставляют
  активных кнопок. Когда сервер выдаёт новую op_session (или пользователь
  в другом чате), клавиатура прежнего сообщения снимается — осиротевшие
  «Запустить бота» не копятся в чате.
- **Monotonic server-completion merge**: задание, подтверждённое сервером,
  терминально исключается из incomplete (``server_done_ids``) — stale
  status-ответы, улетевшие до серверного confirm, больше не воскрешают
  кнопку ни в одном из merge-путей (status/check/batch sync).
- ``server_done_ids`` персистируется в ``dump_state``/``restore_state`` —
  защита сохраняется через рестарт процесса.
- Обратная совместимость API 1.3.2 сохранена полностью; дампы 1.3.2
  читаются (поле опционально).
## 1.3.2

### Добавлено

- **Фоновая адаптивная синхронизация OP (`auto_sync=True` по умолчанию).**
  Выполненный слот исчезает сам — без «Проверить», повторного `/start`
  и любых действий пользователя. Один фоновый цикл на `InteractiveOpBlock`
  (бота) батчем `POST /op-sessions/sync` (≤50 сессий за запрос, не чаще
  `sync_min_interval_seconds`, default 2с). Расписание от последнего
  действия пользователя: 2с первую минуту → 5с до 3 минут → 15с до
  10 минут → **редкий idle-poll 120с до expiry сессии** (подписка
  «вернувшегося молча» пользователя снимает слот даже спустя 10+
  минут без единого действия). Остановка на completion/expiry/
  неизвестной сессии; аварийный giveup через 2ч неактивности.
  Расписание с jitter ±20% — нет синхронного стада после рестарта.
- `PastuhAdsClient.sync_sessions()` + модели `SessionSyncItem`,
  `SessionSyncResponse`.
- **Completion event `on_completed`.** Опциональный callback
  `(telegram_user_id, op_session_id)` — sync или async, ровно один раз
  на сессию при полном подтверждении всех заданий (любым путём:
  фоновая синхронизация, /check, callback-click, первичный op-list).
  Это точка, где бот продолжает исходный сценарий пользователя
  (например, обработку ссылки, ради которой показывался ОП) — повторный
  /start не нужен. Флаг exactly-once персистится в `dump_state`:
  completion, случившийся в простое бота, доставляется один раз при
  `resume()`. Ошибки hook'а изолированы от OP-механики.
- 429 Partner API: `RateLimitError.retry_after` из заголовка
  `Retry-After`; фоновый backoff не короче серверной подсказки.
- Telegram 429 при редактировании: повтор ровно через `retry_after`
  (aiogram `TelegramRetryAfter.retry_after` или текст `retry after N`).
- Очистка давно истёкших блоков (через час после expiry) — без роста
  памяти у долгоживущего бота.

### Совместимость

- Сервер без `/op-sessions/sync` (404) — автоматический откат на
  `GET /status` (реже: ≥5с на сессию, ≤3 сессии за тик).
- 429 Partner API — экспоненциальный backoff 5→60с (не короче
  `Retry-After` сервера); сбои сети/5xx — 2→30с. Отдельный серверный
  лимит sync не расходует бюджет op-list/click.
- Idle-poll ограничен: 1 батч-запрос на 50 сессий в 120с — 1000
  «спящих» сессий одного бота ≈ 0.17 rps, вписывается в лимит 2 rps
  на источник даже с активными сессиями поверх.
- `auto_sync=False` — поведение 1.3.1 без изменений; legacy
  `reconcile_interval_seconds` действует только в этом режиме.
- Публичные сигнатуры, callback-контракты и финансовая семантика не
  изменены; фоновая синхронизация только читает состояние (без `/check`).
- `pastuh_ads.__version__` теперь корректно сообщает версию пакета
  (в 1.3.1 оставалось `1.3.0`).

## 1.3.1

### Изменено

- **OP V2: нейтральные CTA-подписи.** Названия и @username
  рекламируемых ресурсов убраны из кнопок: сервер теперь отдаёт
  `action_button_text` «Открыть канал» / «Запустить бота»;
  SDK-формируемые подписи приведены к тому же контракту —
  reopen-CTA «🔁 Запустить бота», destination-слот «Перейдите по кнопке
  и нажмите «Запустить».» с кнопкой «Запустить бота». Маршрутизация
  остаётся уникальной по task_id/callback_data/URL.
- `examples/aiohttp_interactive_op.py` и `examples/aiogram3_bot.py`
  обновлены до нейтральных подписей.

### Совместимость

Без breaking changes: публичные сигнатуры, callback-контракты
(`paop:open`, `paop:reopen`, `paop:check`) и финансовая семантика
(клик — billable event для tokenless) не изменены. Нейтральные
подписи hosted и external OP идентичны.

## 1.3.0

### Добавлено

- **Interactive OP V2 (event-driven, UX-контракт v2)**: кнопки
  «Проверить выполнение» в новой клавиатуре нет. Подтверждение заданий
  выполняет сервер (chat_member / chat_join_request / BotStartEvent /
  presence-проба), блок узнаёт об этом:
  - piggyback: повторный `open()` и любой `paop:*` callback вызывают
    throttled `POST /check` (не чаще `check_min_interval_seconds`,
    default 20с);
  - опциональный bounded fallback `reconcile_interval_seconds` —
    мягкий `GET /status` цикл до expiry/240 итераций (OFF по умолчанию).
- `paop:check` из старых клавиатур по-прежнему роутится в `/check`
  (compat) — удалять обработчик нельзя.
- `examples/aiohttp_interactive_op.py` обновлён до UX v2: gender
  `user_observation` (env `PASTUH_ADS_GENDER_CLAIM=off|sdk|male|female|
  unknown`), JSON-persistence `PASTUH_ADS_STATE_PATH` (atomic write,
  chmod 600), bounded fallback [60с, 240с].

### Совместимость

Публичные сигнатуры без breaking changes: `open()`/`handle_callback()`/
`dump_state`/`restore_state`/`resume` — прежние. Новый параметр
`check_min_interval_seconds` имеет default. Обработчик `paop:check`
обязателен для старых сообщений.

## 1.2.2

### Изменено

- **UX-паритет слота с эталонным макетом**: текст временного сообщения
  теперь содержит имя бота — «Перейдите в @username и нажмите
  «Запустить».» и кнопка «Перейти в @username» (раньше —
  безликие «Нажмите кнопку ниже»/«Перейти»). Fallback без username —
  прежний текст. Финансовый контракт и TTL-механика не изменялись.

### Добавлено

- `examples/aiohttp_interactive_op.py` — эталон интеграции БЕЗ SDK на
  голом aiohttp (для партнёров с собственным стеком): весь контракт
  op-list/shown/check/callback-click, session-scoped message_id,
  paop:* routing, TTL-слот, resend, монотонный merge.

Совместимость: без изменений Partner API v1 и публичных сигнатур;
рекомендуется обновить зависимость до `pastuh-ads==1.2.2`.

## 1.2.1

### Добавлено

- `User-Agent: pastuh-ads/<version>` в запросах `PastuhAdsClient` —
  платформа может верифицировать фактически установленную версию SDK
  по access-логам (capabilities этого не доказывают).

### Исправлено

- **`open()` больше не переиспользует `message_id` чужой сессии**: если
  сервер выдал новую `op_session_id`, сохранённый блок прежней сессии
  (message_id, shown/completed-флаги) сбрасывается — отправляется новое
  OP-сообщение внизу чата вместо редактирования древнего блока в глубине
  истории. Прод-инцидент: сессия `created` без `POST /shown`, OP невидим.
- **`mark_shown` вызывается после любого успешного показа**, а не только
  при первом send: `_ensure_shown` покрывает edit, «not modified» и
  resend-пути; при ошибке `mark_shown` флаг остаётся снятым и вызов
  повторяется на следующем успешном рендере — сессия не зависает в
  `created`, когда пользователь блок фактически видел.
- Resend после удалённого основного сообщения также доводит `/shown`
  до сервера.

Совместимость: без изменений Partner API v1 и публичных сигнатур;
рекомендуется обновить зависимость до `pastuh-ads==1.2.1`.

## 1.2.0

### Добавлено

- **`InteractiveOpBlock`** (`pastuh_ads.interactive`) — транспортно-
  нейтральный движок интерактивного OP-блока: одно сообщение,
  сокращающаяся клавиатура, one-tap t.me-кнопки для url-целей,
  оплачиваемый `paop:open:` callback для click-заданий, TTL-слот
  destination и `paop:reopen:` без повторной оплаты. Revision-fencing
  защищает от stale-клавиатуры; ограниченный retry на сбоях Telegram;
  `dump_state`/`restore_state`/`resume` для рестарта процесса.
- **Capabilities negotiation**: `capabilities=['callback_cta',
  'interactive_op']` передаётся в op-list; принятые значения — в
  `OpListResponse.negotiated_capabilities`. `interactive_op` принимается
  только при включённой `external_op_block_interactive_enabled` И
  партнёрском `external_op_interactive_allowed` (pilot-allowlist,
  Sprint 3C) — без них ответ совместим с SDK 1.1.0.
- **Gender-claim**: `gender_from_user(user)` и опциональный `gender` в
  `user_observation_from_user` — **opt-in, выключен по умолчанию**
  (`include_gender=False`): без opt-in классификатор не импортируется
  и не вызывается. Vendored pol_module v1.0 — побайтово оригинальные
  classify/namedict/normalize. Сервер применяет claim только для
  `trusted_gender_observer` при включённой `gender_segmentation_enabled`.
- `examples/aiogram3_interactive_op.py` — рабочий каркас бота.
- `MIGRATION_SDK_1.2.0_RU.md`, `INTEGRATION_CHECKLIST_RU.md`.

### Изменено

- `OpListResponse` — новое поле `negotiated_capabilities`.
- `user_observation_from_user` — новый параметр `include_gender`
  (default **False** — Sprint 3C P0: без явного opt-in никаких
  gender-вычислений и никаких claim).
- `InteractiveOpBlock.open` — `include_gender` по умолчанию `False`.
- `InteractiveOpBlock`: при смене `chat_id` старый reconcile-цикл
  отменяется — устаревшие рендеры не уходят в прежний диалог.
- **Rollback (сервер, Sprint 3C)**: persisted `partner_capabilities` в
  `op_sessions.meta` применяются только при живых флаге и allowlist —
  выключение любого возвращает `open_mode='url'` в status/check.

### Совместимость

Обратная совместимость с 1.1.0 сохранена: прежние методы, модели и
контракты работают без изменений; сервер без флага отвечает по
прежнему контракту.

## 1.1.0

- `capabilities=["callback_cta"]`, `open_mode`/`target_url`/
  `cta_callback_data` для `bot_tracked_click`, `register_callback_click`.
- Premium `user_observation` + `premium_claim` диагностика.

## 1.0.0

- Первый релиз: op-list, shown, check, status, click, bot-start,
  placements, payouts, Premium verification.
