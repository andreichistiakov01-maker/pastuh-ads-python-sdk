# Pastuh Ads — быстрый старт (Python)

**1.** API-ключ: кабинет → Источники → ваш источник →
«Выпустить API-ключ» (или раздел «API-ключи»). Показывается один раз.

**2.** Source ID: страница источника → поле **«API Source ID»** —
число, например `48`.
Не `src_48`! Публичный идентификатор с префиксом `src_` API не принимает —
нужно просто число.

**3.** Placement ID: там же на странице источника — поле
**«Placement ID»**; или раздел «Плейсменты» (ID + кнопка копирования);
или из кода: `await client.list_placements()`.

**4.** Заполните `.env`:

```bash
cp .env.example .env
```

```env
PASTUH_ADS_API_URL=https://pastuhads.com/api/partner/v1
PASTUH_ADS_API_KEY=opk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
PASTUH_ADS_SOURCE_ID=50
PASTUH_ADS_PLACEMENT_ID=1
```

**5.** Установите зависимости:

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**6.** Проверьте подключение:

```bash
python examples/basic_usage.py
```

Должны увидеть: ID партнёра, username источника, баланс, список заданий OP.

**7.** Для aiogram-бота: перенесите хендлеры из `examples/aiogram3_bot.py`
в ваш проект (`pip install aiogram`). Точки интеграции:

- `get_op_list` → показ заданий → `mark_shown`
- кнопка «Проверить» → `check_session` → контент при `access_allowed`
- `/start bs1.*` → `confirm_bot_start` (только с payload, обычный /start — нет)

**8.** Premium-задания (zero-click): передавайте
`user_observation=user_observation_from_user(message.from_user)` в
`get_op_list` — SDK берёт `is_premium` прямо из Telegram-апдейта.
Работает после включения флага `trusted_premium_observer` админом
платформы; иначе в ответе будет `premium_verification.url` — покажите её
как кнопку. Подробности — `README_RU.md` §11.5.

Подробности и коды ошибок — в `README_RU.md`.
