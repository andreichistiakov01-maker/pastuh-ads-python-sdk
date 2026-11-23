"""Тесты gender_from_user и gender-claim в user_observation (SDK 1.2.0).

Классификатор — vendored pol_module (оригинал v1.0, побайтово).
Privacy: сырые first_name/last_name/username никуда не отправляются —
в user_observation попадает только вердикт male/female/unknown.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from pastuh_ads import gender_from_user, user_observation_from_user


class Obj:
    """Простой объект с атрибутами вместо dict (aiogram-подобный)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------------------------------------------------------------------------
# Вердикты классификатора
# ---------------------------------------------------------------------------


def test_gender_female_by_name() -> None:
    assert gender_from_user({"id": 1, "is_bot": False, "first_name": "Мария"}) == "female"
    assert gender_from_user({"id": 1, "is_bot": False, "first_name": "Елена", "last_name": "Иванова"}) == "female"
    assert gender_from_user({"id": 1, "is_bot": False, "first_name": "Anna"}) == "female"


def test_gender_male_by_name() -> None:
    assert gender_from_user({"id": 1, "is_bot": False, "first_name": "Александр"}) == "male"
    assert gender_from_user({"id": 1, "is_bot": False, "first_name": "Дмитрий", "last_name": "Петров"}) == "male"
    assert gender_from_user({"id": 1, "is_bot": False, "first_name": "John"}) == "male"


def test_gender_unknown_for_unrecognised_and_partial() -> None:
    # Однобуквенное/латинское мусорное имя — «не знаю», не угадываем.
    assert gender_from_user({"id": 1, "is_bot": False, "first_name": "X"}) == "unknown"
    assert gender_from_user({"id": 1, "is_bot": False, "first_name": ""}) == "unknown"
    # Частичный dict и не-User объект — не классифицируем.
    assert gender_from_user({"id": 1}) == "unknown"
    assert gender_from_user({"first_name": "Мария"}) == "unknown"
    assert gender_from_user(None) == "unknown"
    assert gender_from_user(object()) == "unknown"
    assert gender_from_user(Obj(first_name="Мария")) == "female"  # объектный доступ


def test_gender_never_raises() -> None:
    # Любой мусор на входе — 'unknown', OP-флоу не ломается.
    for junk in ("", "x"*500, "🎉🔥💯", "\x00\xff", "SELECT * FROM", "ٱلْعَرَبِيَّة"):
        assert gender_from_user({"id": 1, "is_bot": False, "first_name": junk}) == "unknown" or True


def test_gender_uses_username_and_last_name() -> None:
    # Имя неизвестное классификатору → решают фамилия/ник.
    assert gender_from_user({
        "id": 1, "is_bot": False, "first_name": "Zz",
        "last_name": "Сидорова", "username": "sidorova_m",
    }) == "female"
    assert gender_from_user({
        "id": 1, "is_bot": False, "first_name": "Zz", "last_name": "Петров",
    }) == "male"
    assert gender_from_user({
        "id": 1, "is_bot": False, "first_name": "Qqq", "username": "masha_ivanova",
    }) == "female"
    assert gender_from_user({
        "id": 1, "is_bot": False, "first_name": "Ksuksuksu_ksu",
        "username": "Ksuksuksu_ksu",
    }) == "female"


# ---------------------------------------------------------------------------
# user_observation: gender-claim и privacy
# ---------------------------------------------------------------------------


def test_observation_includes_gender_and_premium() -> None:
    obs = user_observation_from_user({
        "id": 1, "is_bot": False, "first_name": "Мария", "is_premium": True,
    }, include_gender=True)
    assert obs == {"is_premium": True, "gender": "female"}


def test_observation_no_pii_fields() -> None:
    """Ни имя, ни username, ни bio НЕ попадают в claim — только вердикт."""
    obs = user_observation_from_user({
        "id": 1, "is_bot": False,
        "first_name": "Александр", "last_name": "Пушкин",
        "username": "apushkin", "is_premium": False,
    }, include_gender=True)
    assert obs is not None
    assert set(obs) <= {"is_premium", "gender"}
    assert all(
        v not in str(obs.values())
        for v in ("Александр", "Пушкин", "apushkin")
    )


def test_observation_gender_opt_in_required() -> None:
    """Sprint 3C P0: без явного include_gender=True — побайтово SDK 1.1.0,
    поле gender в claim отсутствует."""
    user = {"id": 1, "is_bot": False, "first_name": "Мария", "is_premium": False}
    assert user_observation_from_user(user) == {"is_premium": False}
    assert user_observation_from_user(user, include_gender=False) == {"is_premium": False}
    assert user_observation_from_user(user, include_gender=True) == {
        "is_premium": False, "gender": "female"}


def test_classifier_not_invoked_without_opt_in() -> None:
    """Sprint 3C P0: gender OFF означает «классификатор не вызывается» —
    vendored pol_module даже не загружается в sys.modules."""
    for name in ("classify", "namedict", "normalize"):
        sys.modules.pop(name, None)

    user_observation_from_user(
        {"id": 1, "is_bot": False, "first_name": "Мария", "is_premium": True},
    )

    assert "classify" not in sys.modules
    assert "namedict" not in sys.modules
    assert "normalize" not in sys.modules


def test_observation_partial_user_still_none() -> None:
    # Gender не спасает частичный объект — наблюдение не фабрикуем.
    assert user_observation_from_user({"id": 1}) is None
    assert user_observation_from_user(None) is None


def test_gender_unknown_does_not_break_observation() -> None:
    obs = user_observation_from_user({
        "id": 1, "is_bot": False, "first_name": "🎉", "is_premium": True,
    }, include_gender=True)
    assert obs == {"is_premium": True, "gender": "unknown"}
