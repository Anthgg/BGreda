"""Emision y verificacion de tokens CSRF."""

from __future__ import annotations

import time

import pytest

from app.auth import csrf
from app.core.config import Settings
from app.core.errors import CsrfTokenInvalidError, CsrfTokenMissingError


@pytest.fixture
def settings() -> Settings:
    return Settings(CSRF_SECRET="secreto-de-pruebas-para-firmar-tokens-csrf")


def test_token_emitido_es_valido(settings: Settings) -> None:
    token = csrf.issue_token(settings)

    assert csrf.is_token_valid(settings, token)


def test_cada_token_es_distinto(settings: Settings) -> None:
    assert csrf.issue_token(settings) != csrf.issue_token(settings)


def test_token_expirado_no_es_valido(settings: Settings) -> None:
    token = csrf.issue_token(settings)
    futuro = int(time.time()) + settings.CSRF_TOKEN_TTL_SECONDS + 1

    assert not csrf.is_token_valid(settings, token, now=futuro)


def test_token_con_firma_alterada_no_es_valido(settings: Settings) -> None:
    nonce, expiracion, firma = csrf.issue_token(settings).split(".")
    alterado = f"{nonce}.{expiracion}.{'0' * len(firma)}"

    assert not csrf.is_token_valid(settings, alterado)


def test_token_con_expiracion_extendida_no_es_valido(settings: Settings) -> None:
    """Alargar la vigencia invalida la firma."""
    nonce, expiracion, firma = csrf.issue_token(settings).split(".")
    extendido = f"{nonce}.{int(expiracion) + 10_000}.{firma}"

    assert not csrf.is_token_valid(settings, extendido)


def test_token_de_otro_secreto_no_es_valido(settings: Settings) -> None:
    otro = Settings(CSRF_SECRET="un-secreto-completamente-distinto-abcdef")
    token = csrf.issue_token(otro)

    assert not csrf.is_token_valid(settings, token)


@pytest.mark.parametrize("token", ["", "sin-separadores", "a.b", "a.b.c.d", "a.no-numero.c"])
def test_tokens_mal_formados_no_son_validos(settings: Settings, token: str) -> None:
    assert not csrf.is_token_valid(settings, token)


def test_validate_exige_cabecera_y_cookie(settings: Settings) -> None:
    token = csrf.issue_token(settings)

    with pytest.raises(CsrfTokenMissingError):
        csrf.validate(settings, header_token=None, cookie_token=token)
    with pytest.raises(CsrfTokenMissingError):
        csrf.validate(settings, header_token=token, cookie_token=None)


def test_validate_exige_que_coincidan(settings: Settings) -> None:
    with pytest.raises(CsrfTokenInvalidError):
        csrf.validate(
            settings,
            header_token=csrf.issue_token(settings),
            cookie_token=csrf.issue_token(settings),
        )


def test_validate_acepta_un_token_correcto(settings: Settings) -> None:
    token = csrf.issue_token(settings)

    csrf.validate(settings, header_token=token, cookie_token=token)


def test_metodos_protegidos_son_los_mutadores() -> None:
    assert csrf.PROTECTED_METHODS == {"POST", "PUT", "PATCH", "DELETE"}
    assert "GET" not in csrf.PROTECTED_METHODS
