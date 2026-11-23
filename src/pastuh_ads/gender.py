"""Локальная классификация пола для user_observation.gender (SDK 1.2.0).

Оригинальный клиентский модуль (pol_module: classify.py, namedict.py,
normalize.py) поставляется в пакете БЕЗ изменений — файлы побайтово
идентичны версии v1.0. Он использует плоские импорты
(``import normalize``, ``import namedict``), поэтому загружаем модули
под их каноническими именами через importlib — без изменения sys.path.

МОДЕЛЬ ДОВЕРИЯ И PRIVACY:
- классификация выполняется ТОЛЬКО внутри процесса партнёрского бота —
  ``first_name``, ``last_name``, ``username`` никуда не отправляются;
- на сервер уходит только вердикт ``male``/``female``/``unknown`` в
  ``user_observation.gender`` — и только если партнёр явно включил
  передачу (см. ``user_observation_from_user``);
- сервер применяет claim лишь от партнёров с флагом
  ``trusted_gender_observer`` и только при включённой
  ``gender_segmentation_enabled`` — SDK не может сам выдать себе
  trusted-статус;
- связка user_id -> gender нигде не сохраняется: ни в SDK, ни на сервере
  (request-scoped вердикт одной выдачи).

FAIL-CLOSED: любая ошибка загрузки/классификации → ``"unknown"`` —
исключение никогда не ломает OP-флоу.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

_POL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pol_module")
_POL_MODULES = ("normalize", "namedict", "classify")


def _load_classify():
    """Загрузить vendored classify() из pol_module.

    Регистрирует normalize/namedict/classify в sys.modules под их
    каноническими именами — плоские импорты classify.py резолвятся
    без мутации sys.path. Если одноимённый ЧУЖОЙ модуль уже загружен
    (например, у партнёра свой ``normalize.py`` на sys.path), отказываемся
    классифицировать: подмена модуля под чужим именем недопустима.
    """
    for name in _POL_MODULES:
        existing = sys.modules.get(name)
        if existing is not None:
            existing_file = os.path.abspath(getattr(existing, "__file__", "") or "")
            if os.path.dirname(existing_file) == _POL_DIR:
                continue  # уже загружен наш модуль — переиспользуем
            raise ImportError(
                f"sys.modules['{name}'] занят чужим модулем ({existing_file})"
            )

        path = os.path.join(_POL_DIR, name + ".py")
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"не удалось загрузить {path}")
        module = importlib.util.module_from_spec(spec)
        # Регистрируем ДО exec_module — 'import normalize as N' внутри
        # classify.py должен попасть в наш же экземпляр.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise

    return sys.modules["classify"].classify


def _field(user: Any, name: str) -> str:
    """Достать строковое поле из dict/объекта Telegram User."""
    if isinstance(user, dict):
        value = user.get(name)
    else:
        value = getattr(user, name, None)
    return value if isinstance(value, str) else ""


def gender_from_user(user: Any) -> str:
    """Классифицировать пол по Telegram User-объекту. Никогда не бросает.

    Args:
        user: полный объект пользователя из апдейта — aiogram
            ``types.User``, pyrogram ``User`` или сырой dict Bot API.
            Используются только ``first_name``, ``last_name`` и
            ``username``; отдельный ``getChat`` НЕ делается и не нужен.

    Returns:
        ``"male"`` / ``"female"`` / ``"unknown"`` — последний также при
        частичном/отсутствующем профиле и любой ошибке классификации.
    """
    try:
        if user is None:
            return "unknown"
        if isinstance(user, dict):
            if "id" not in user or "first_name" not in user:
                return "unknown"  # частичный dict — не полный Telegram User
        elif not hasattr(user, "first_name"):
            return "unknown"

        classify = _load_classify()
        result = classify(
            first_name=_field(user, "first_name"),
            last_name=_field(user, "last_name"),
            username=_field(user, "username"),
        )
        return {"f": "female", "m": "male"}.get(
            (result or {}).get("gender"), "unknown"
        )
    except Exception as exc:
        # Никакого PII в логе — только тип ошибки.
        logger.debug("gender_from_user: классификация недоступна (%s)", type(exc).__name__)
        return "unknown"
