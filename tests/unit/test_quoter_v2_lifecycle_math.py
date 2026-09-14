"""Fase 010H — el calendario de la vigencia y la huella comercial, sin base de datos.

Las fronteras de la vigencia son las que un error convierte en dinero: una
oferta que vence cinco horas antes de lo que dice el papel se pierde, y una que
vence un dia despues se acepta con un precio que ya no valia.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest

from app.core.quoter_v2_lifecycle import (
    BUSINESS_TZ,
    MAX_VALIDITY_DAYS,
    V2EffectiveStatus,
    ValidityError,
    business_date,
    commercial_fingerprint,
    compute_validity,
    effective_status,
    is_expired,
)


def lima(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=BUSINESS_TZ)


# ---------------------------------------------------------------------------
# Vigencia
# ---------------------------------------------------------------------------
class TestVigencia:
    def test_el_ejemplo_del_excel(self) -> None:
        """Emitida el 10/09 con 20 dias: vale hasta el 30/09 y vence el 01/10."""
        hasta, vence = compute_validity(lima(2026, 9, 10, 11, 30), 20)
        assert hasta == date(2026, 9, 30)
        assert vence == lima(2026, 10, 1).astimezone(UTC)
        assert vence == datetime(2026, 10, 1, 5, 0, tzinfo=UTC)

    def test_vigencia_de_un_dia(self) -> None:
        hasta, vence = compute_validity(lima(2026, 9, 14, 9), 1)
        assert hasta == date(2026, 9, 15)
        assert vence == lima(2026, 9, 16)

    def test_vigencia_de_365_dias(self) -> None:
        hasta, _ = compute_validity(lima(2026, 1, 1, 12), 365)
        assert hasta == date(2027, 1, 1)

    def test_el_tope_es_el_de_la_configuracion(self) -> None:
        """La configuracion de V2 admite hasta 3650 dias; emitir no puede admitir menos."""
        from app.schemas.quoter_v2_settings import V2SettingsUpdateIn

        tope = V2SettingsUpdateIn.model_fields["quotation_validity_days"].metadata
        assert any(getattr(regla, "le", None) == MAX_VALIDITY_DAYS for regla in tope)
        hasta, _ = compute_validity(lima(2026, 1, 1, 12), MAX_VALIDITY_DAYS)
        assert hasta > date(2035, 1, 1)

    @pytest.mark.parametrize("dias", [0, -1, MAX_VALIDITY_DAYS + 1])
    def test_vigencias_imposibles(self, dias: int) -> None:
        with pytest.raises(ValidityError):
            compute_validity(lima(2026, 9, 10), dias)

    def test_booleano_no_es_un_numero_de_dias(self) -> None:
        with pytest.raises(ValidityError):
            compute_validity(lima(2026, 9, 10), True)

    def test_emitida_a_las_23_59_de_lima_cuenta_ese_dia(self) -> None:
        """En UTC ya es el dia siguiente; en el papel, no."""
        emision = lima(2026, 9, 10, 23, 59, 59)
        assert emision.astimezone(UTC).date() == date(2026, 9, 11)
        hasta, _ = compute_validity(emision, 20)
        assert hasta == date(2026, 9, 30)

    def test_emitida_exactamente_a_medianoche_de_lima(self) -> None:
        hasta, _ = compute_validity(lima(2026, 9, 10, 0, 0, 0), 20)
        assert hasta == date(2026, 9, 30)

    def test_servidor_en_utc_no_adelanta_el_dia(self) -> None:
        """Las 02:00 UTC del 11 son las 21:00 de Lima del 10."""
        emision = datetime(2026, 9, 11, 2, 0, tzinfo=UTC)
        assert business_date(emision) == date(2026, 9, 10)

    def test_instante_sin_zona_se_rechaza(self) -> None:
        with pytest.raises(ValidityError):
            business_date(datetime(2026, 9, 10, 12, 0))

    def test_la_zona_no_tiene_horario_de_verano(self) -> None:
        """Peru es UTC-5 todo el año: enero y julio dan el mismo desplazamiento."""
        assert lima(2026, 1, 15).utcoffset() == lima(2026, 7, 15).utcoffset()
        assert lima(2026, 1, 15).utcoffset() == timedelta(hours=-5)


class TestVencimiento:
    def test_expira_hoy_sigue_valida_todo_el_dia(self) -> None:
        _, vence = compute_validity(lima(2026, 9, 10, 10), 20)
        assert not is_expired(vence, lima(2026, 9, 30, 23, 59, 59))

    def test_en_el_limite_exacto_ya_vencio(self) -> None:
        _, vence = compute_validity(lima(2026, 9, 10, 10), 20)
        assert is_expired(vence, vence)
        assert not is_expired(vence, vence - timedelta(microseconds=1))

    def test_sin_fecha_de_vencimiento_no_vence(self) -> None:
        assert not is_expired(None, lima(2099, 1, 1))


class TestEstadoEfectivo:
    AHORA = lima(2026, 10, 5)
    VENCIDA = lima(2026, 10, 1)
    VIGENTE = lima(2026, 10, 20)

    def _estado(self, status: str, expires_at: datetime | None, puente: bool) -> V2EffectiveStatus:
        return effective_status(
            status=status, expires_at=expires_at, has_production_handoff=puente, now=self.AHORA
        )

    def test_borrador(self) -> None:
        assert self._estado("DRAFT", None, False) is V2EffectiveStatus.DRAFT

    def test_emitida_vigente(self) -> None:
        assert self._estado("CONFIRMED", self.VIGENTE, False) is V2EffectiveStatus.CONFIRMED

    def test_emitida_vencida(self) -> None:
        assert self._estado("CONFIRMED", self.VENCIDA, False) is V2EffectiveStatus.EXPIRED

    def test_en_produccion_no_vence(self) -> None:
        """El cliente acepto dentro de plazo: pasar el dia no la devuelve a vencida."""
        assert (
            self._estado("CONFIRMED", self.VENCIDA, True) is V2EffectiveStatus.READY_FOR_PRODUCTION
        )

    def test_cancelada_gana_aunque_haya_vencido(self) -> None:
        assert self._estado("CANCELLED", self.VENCIDA, False) is V2EffectiveStatus.CANCELLED


# ---------------------------------------------------------------------------
# Huella
# ---------------------------------------------------------------------------
class TestHuella:
    BASE: ClassVar[dict[str, object]] = {
        "total": Decimal("123.50"),
        "lines": [{"id": 1, "unit_price": Decimal("10.500000"), "quantity": 3}],
        "currency_code": "USD",
    }

    def test_es_sha256_hexadecimal(self) -> None:
        huella = commercial_fingerprint(self.BASE)
        assert len(huella) == 64
        int(huella, 16)

    def test_la_escala_decimal_no_cambia_la_huella(self) -> None:
        """Leer de la base `10.500000` o calcular `10.5` es el mismo importe."""
        otra = {
            "currency_code": "USD",
            "lines": [{"quantity": 3, "unit_price": Decimal("10.5"), "id": 1}],
            "total": Decimal("123.5000000000"),
        }
        assert commercial_fingerprint(otra) == commercial_fingerprint(self.BASE)

    def test_el_cero_con_escala_es_cero(self) -> None:
        assert commercial_fingerprint({"x": Decimal("0E-18")}) == commercial_fingerprint(
            {"x": Decimal(0)}
        )

    def test_cambiar_una_cantidad_cambia_la_huella(self) -> None:
        otra = {**self.BASE, "lines": [{"id": 1, "unit_price": Decimal("10.5"), "quantity": 4}]}
        assert commercial_fingerprint(otra) != commercial_fingerprint(self.BASE)

    def test_cambiar_el_unitario_cambia_la_huella(self) -> None:
        otra = {**self.BASE, "lines": [{"id": 1, "unit_price": Decimal("11"), "quantity": 3}]}
        assert commercial_fingerprint(otra) != commercial_fingerprint(self.BASE)

    def test_nulo_y_cero_son_distintos(self) -> None:
        assert commercial_fingerprint({"x": None}) != commercial_fingerprint({"x": Decimal(0)})

    def test_nan_se_rechaza(self) -> None:
        with pytest.raises(ValueError):
            commercial_fingerprint({"x": Decimal("NaN")})
