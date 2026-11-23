"""Исключения Pastuh Ads Partner API клиента.

Иерархия:
    PastuhAdsError                     — базовое исключение
    ├── AuthenticationError            — 401 (невалидный/отсутствующий API-ключ)
    ├── ForbiddenError                 — 403 (источник/плейсмент недоступен, wrong_mode)
    ├── NotFoundError                  — 404 (сессия/task/integration не найдены)
    ├── ConflictError                  — 409 (например, session_expired)
    ├── ValidationError                — 422 (ошибка валидации, ignored)
    ├── RateLimitError                 — 429
    ├── ServerError                    — 5xx
    ├── ApiTimeoutError                — таймаут запроса
    └── ApiConnectionError             — сетевая ошибка

НИКОГДА не содержат API-ключ: в исключение попадают только
status_code, код ошибки из тела ответа, message и details.
"""

from __future__ import annotations

from typing import Any


class PastuhAdsError(Exception):
    """Базовое исключение клиента.

    Атрибуты:
        status_code: HTTP-код ответа (0 — если ответа не было).
        error_code: машинный код ошибки из тела ответа
            (например ``source_not_allowed``, ``session_expired``) или None.
        details: dict с полем ``details`` из ответа API или None.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.details = details

    def __str__(self) -> str:
        parts = [self.message]
        if self.error_code:
            parts.append(f"[{self.error_code}]")
        if self.status_code:
            parts.append(f"(HTTP {self.status_code})")
        return " ".join(parts)


class AuthenticationError(PastuhAdsError):
    """401 — API-ключ не передан или недействителен."""


class ForbiddenError(PastuhAdsError):
    """403 — источник/плейсмент недоступен, пользователь деактивирован, wrong_mode."""


class NotFoundError(PastuhAdsError):
    """404 — op_session / task / payout / integration не найдены."""


class ConflictError(PastuhAdsError):
    """409 — конфликт состояния (например, session_expired — сессия истекла)."""


class ValidationError(PastuhAdsError):
    """422 — данные запроса не прошли валидацию или событие ignored."""


class RateLimitError(PastuhAdsError):
    """429 — превышен лимит запросов."""


class ServerError(PastuhAdsError):
    """5xx — внутренняя ошибка сервера Pastuh Ads."""


class ApiTimeoutError(PastuhAdsError):
    """Таймаут запроса (httpx.TimeoutException)."""


class ApiConnectionError(PastuhAdsError):
    """Ошибка соединения (DNS, refused, reset и т.п.)."""
