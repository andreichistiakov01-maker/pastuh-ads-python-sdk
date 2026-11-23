# Миграция Partner SDK 1.1.0 → 1.2.0

SDK 1.2.0 — обратно-совместимое минорное обновление: весь код 1.1.0
продолжает работать без изменений. Ломающих изменений API нет.

## Что нового

| Возможность | Как включается |
|---|---|
| `InteractiveOpBlock` — одно OP-сообщение, сокращающаяся клавиатура, TTL-слот, reopen без повторной оплаты | `block.open()` сам передаёт `capabilities=['callback_cta','interactive_op']`; требует глобального `external_op_block_interactive_enabled` **и** разрешения партнёру (`external_op_interactive_allowed`) — оба ставит админ платформы |
| `gender` в `user_observation` (`male`/`female`/`unknown`) | **opt-in, выключено по умолчанию** (`include_gender=False`); без opt-in классификатор не вызывается |
| `negotiated_capabilities` в ответе op-list | автоматически |
| Vendored pol_module (классификатор пола v1.0) | внутри пакета, без зависимостей |

## Совместимость

- **Без флага платформы или partner-allowlist**: сервер не принимает
  `interactive_op` → `bot_tracked_click` отдаётся в legacy
  `open_mode='url'` (`/r/{token}`), как у SDK 1.1.0. Принудительной
  миграции нет.
- **`user_observation_from_user`**: по умолчанию возвращает только
  `is_premium` — побайтово как 1.1.0. `gender` появляется только при
  явном `include_gender=True`; без opt-in модуль классификации даже
  не импортируется.
- **Gender claim**: без `trusted_gender_observer` на стороне партнёра
  claim игнорируется сервером (`gender_claim.status == "ignored_untrusted"`)
  — безопасно, но включать следует только после OWNER GO платформы.
- **`paop:*` callback_data** — внутренний формат SDK для кнопок вашего
  бота. Не путать с серверным `task.cta_callback_data` (`op:open:*`) —
  оба namespace'а не пересекаются.

## Пошаговая миграция

1. Обновите пакет: `pip install pastuh-ads==1.2.1` (или whl из архива).
2. Если используете ручной OP-рендер — можете остаться на нём; для
   интерактивного блока подключите `InteractiveOpBlock` (см.
   `examples/aiogram3_interactive_op.py`).
3. Решите по gender: оставить default (`include_gender=False`) или
   включить `include_gender=True` — только после выдачи партнёру
   `trusted_gender_observer`.
4. Сообщите платформе о готовности: включение делает админ — глобальный
   `external_op_block_interactive_enabled` + партнёрский
   `external_op_interactive_allowed`. До этого SDK работает по прежнему
   контракту.

## Checklist после обновления

- [ ] `/start` рендерит одно OP-сообщение с кнопками заданий.
- [ ] Подписка/заявка/бот с токеном — прямые t.me-кнопки.
- [ ] Tokenless-бот — callback → слот со ссылкой ~15 с.
- [ ] Повторное открытие после TTL — без повторной оплаты.
- [ ] «Проверить выполнение» снимает выполненные кнопки.
- [ ] Завершение — «Доступ открыт» + reopen-CTA остаются.
- [ ] `dump_state()`/`restore_state()`/`resume()` подключены к вашему
      хранилищу (если нужно переживать рестарт).
