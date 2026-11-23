# Integration Checklist — Partner SDK 1.2.0

## Установка

- [ ] `pip install pastuh-ads==1.2.1` (whl из handoff-архива или sdist).
- [ ] `python -c "import pastuh_ads; print(pastuh_ads.__version__)"` → `1.2.1`.

## Базовая интеграция

- [ ] `PastuhAdsClient` создан с `base_url`/`api_key`/`source_id`/`placement_id`.
- [ ] `block.open()` вызывается на входе пользователя; повторный `/start`
      НЕ создаёт второе OP-сообщение.
- [ ] Все `paop:*` callback'и маршрутизируются в `block.handle_callback`.
- [ ] `block.aclose()` вызывается при shutdown (отменяет фон-задачи).

## Кнопки и оплата

- [ ] `subscription_confirmed` / `join_request_submitted` /
      `bot_start_confirmed` / `bot_new_user_verified` — прямые
      t.me url-кнопки.
- [ ] `bot_tracked_click` / `bot_click` — callback-кнопки `paop:open:*`;
      нативная url-кнопка в слоте — `target_url` из ответа API (никогда
      не `/r/` и не произвольная ссылка).
- [ ] Reopen (`paop:reopen:*`) не содержит вызовов Partner API —
      повторная оплата исключена по построению.
- [ ] Callback-ответ на `answerCallbackQuery` — БЕЗ `url` (Telegram
      отклоняет переход в чужой бот: `URL_INVALID`).

## Состояние и рестарт

- [ ] `dump_state()` сохраняется (Redis/файл) после мутаций блока.
- [ ] `restore_state()` + `resume()` при старте — клавиатуры
      пересобираются из `GET /status`.
- [ ] Удалённое пользователем OP-сообщение пересоздаётся один раз
      (проверьте лог `resend`).

## Gender (опционально, opt-in — default OFF)

- [ ] По умолчанию `include_gender=False` — классификатор не вызывается,
      ответ `user_observation_from_user` побайтово как в SDK 1.1.0.
- [ ] `include_gender=True` передаётся ТОЛЬКО после того, как платформа
      выдала `trusted_gender_observer` и включила
      `gender_segmentation_enabled` (отдельный OWNER GO).
- [ ] В логах бота нет сырых `first_name`/`last_name`/`username`
      для целей классификации.
- [ ] Понято, что claim — это вердикт алгоритма, а не достоверный
      биологический пол пользователя.

## Безопасность и состояние

- [ ] API-ключ не логируется и не попадает в исключения.
- [ ] Чужой `telegram_user_id`/`chat_id` в callback не получает слот
      (SDK проверяет владельца сессии до любого API-вызова).
- [ ] `dump_state()` хранится только у партнёра: в нём — идентификаторы
      сессии (`op_session_id`, `telegram_user_id`, `chat_id`,
      `message_id`, `expires_at`), состояние заданий и destination-URL.
      Ни токена бота, ни API-ключа, ни полей профиля, ни gender —
      ничего лишнего. State НЕ является финансовым источником:
      оплата подтверждается только ответом Partner API; подделка дампа
      может лишь показать уже публичный `t.me`-адрес — денег не двигает.
- [ ] TTL состояния у партнёра — не дольше жизни OP-сессии
      (`expires_at` из `dump_state`); просроченный дамп удаляется.
- [ ] `external_op_interactive_allowed` (пилот-allowlist) + глобальный
      `external_op_block_interactive_enabled` запрошены у админа; до
      включения проверена legacy-деградация (`open_mode='url'`).

## Rollback (откат флага платформой)

- [ ] Известно: выключение `external_op_block_interactive_enabled` или
      снятие `external_op_interactive_allowed` мгновенно возвращает
      `open_mode='url'` в status/check — SDK перестраивает клавиатуру
      с `/r/{token}`-кнопками, задания остаются выполнимыми.
- [ ] Уже отрисованные `paop:open:`-кнопки продолжают работать через
      `callback-click`/`click` — это легаси-контракт SDK 1.1.0, оплата
      идемпотентна (`charged=false` на повторах).
- [ ] Restart-runbook: без `restore_state()`+`resume()` фоновые TTL-
      задачи умерших процессов не восстанавливаются — старые слоты
      остаются висеть в чате до ручного удаления пользователем; для
      prod-режима dump/restore обязателен.
