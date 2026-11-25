# Changelog — Pastuh Ads Partner SDK

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
