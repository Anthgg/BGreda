"""Fase 010K — la aritmetica de Solo Quema V2 con los numeros de la hoja «Solo Quema»."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.firing_quotation_v2 import (
    CycleRates,
    FiringQuotationMathError,
    glaze_material_cost,
    kiln_quote,
    service_price,
    validate_factor,
    volume_shares,
)
from app.core.quoter_v2_firing import piece_volume, total_volume

#: Horno chico y grande del Excel final, tarifas externas.
CHICO = Decimal(17000)
GRANDE = Decimal(200000)
TARIFA_CHICO = CycleRates(Decimal(200), Decimal(250), Decimal(35), Decimal(70))
TARIFA_GRANDE = CycleRates(Decimal(700), Decimal(1200), Decimal(55), Decimal(110))
ALUMNO_GRANDE = CycleRates(Decimal(1000), Decimal(2000), Decimal(55), Decimal(110))
M = Decimal("0.000001")


def _vol(porcentaje: str, capacidad: Decimal = Decimal(100)) -> Decimal:
    return Decimal(porcentaje) * capacidad / Decimal(100)


# ---------------------------------------------------------------------------
# El caso de la hoja
# ---------------------------------------------------------------------------
def test_caso_del_excel_cien_piezas_de_ocho_centimetros() -> None:
    """100 piezas 8x8x8 con 3 cm: 133100 cm3; Chico 782,94 %, 8 hornadas."""
    volumen = total_volume(piece_volume(Decimal(8), Decimal(8), Decimal(8), Decimal(3)), 100)
    assert volumen == Decimal(133100)

    chico = kiln_quote(volumen, CHICO, TARIFA_CHICO, low=True, high=True)
    assert chico.occupancy_percent == Decimal("782.941176")
    assert chico.firing_count == 8
    assert chico.shared is not None and chico.exclusive is not None
    assert chico.shared.commercial == Decimal("3523.235294")
    assert chico.shared.gas == Decimal("822.088235")
    assert chico.exclusive.commercial == Decimal(3600)
    assert chico.exclusive.gas == Decimal(840)
    assert chico.batch_loads[-1] == Decimal("82.941176")

    grande = kiln_quote(volumen, GRANDE, TARIFA_GRANDE, low=True, high=True)
    assert grande.occupancy_percent == Decimal("66.55")
    assert grande.firing_count == 1
    assert grande.shared is not None
    assert grande.shared.commercial == Decimal("1264.45")
    assert grande.shared.gas.quantize(M) == Decimal("109.8075")

    precio = service_price(
        firing_commercial=chico.shared.commercial,
        firing_gas=chico.shared.gas,
        glaze_material=Decimal(0),
        glaze_labor=Decimal(0),
        factor=Decimal(1),
        tax_percent=Decimal(18),
        rounding_step=Decimal("0.5"),
        exchange_rate=None,
    )
    assert precio.subtotal == Decimal("3523.5")
    assert precio.tax == Decimal("634.23")
    assert precio.total == Decimal("4157.73")
    assert precio.real_cost == Decimal("822.088235")
    assert precio.profit == Decimal("2701.411765")


# ---------------------------------------------------------------------------
# Compartida y exclusiva, por ciclo
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("porcentaje", "compartida", "exclusiva"),
    [
        ("10", "45", "450"),
        ("50", "225", "450"),
        ("100", "450", "450"),
        ("101", "454.5", "900"),
        ("120", "540", "900"),
        ("250", "1125", "1350"),
    ],
)
def test_tabla_de_ocupaciones(porcentaje: str, compartida: str, exclusiva: str) -> None:
    quote = kiln_quote(_vol(porcentaje), Decimal(100), TARIFA_CHICO, low=True, high=True)
    assert quote.shared is not None and quote.exclusive is not None
    assert quote.shared.commercial == Decimal(compartida)
    assert quote.exclusive.commercial == Decimal(exclusiva)


def test_quinientos_uno_con_ochenta_y_ocho() -> None:
    quote = kiln_quote(Decimal(85320), CHICO, TARIFA_CHICO, low=True, high=True)
    assert quote.firing_count == 6
    assert quote.shared is not None and quote.exclusive is not None
    assert quote.shared.commercial == Decimal("2258.470588")
    assert quote.exclusive.commercial == Decimal(2700)


def test_ciento_veinte_son_una_hornada_llena_y_otra_al_veinte() -> None:
    """Se muestra 100 % + 20 %, aunque en compartida se cobre 1,20 tarifas."""
    quote = kiln_quote(_vol("120"), Decimal(100), TARIFA_CHICO, low=True, high=False)
    assert quote.batch_loads == (Decimal(100), Decimal(20))
    assert quote.shared is not None
    assert quote.shared.billed_load == Decimal("1.2")
    assert quote.shared.commercial == Decimal(240)


def test_solo_baja_solo_alta_y_ambas() -> None:
    """20 %: baja 200 x 0,2 = 40; alta 250 x 0,2 = 50; ambas 90."""
    baja = kiln_quote(_vol("20"), Decimal(100), TARIFA_CHICO, low=True, high=False).shared
    alta = kiln_quote(_vol("20"), Decimal(100), TARIFA_CHICO, low=False, high=True).shared
    ambas = kiln_quote(_vol("20"), Decimal(100), TARIFA_CHICO, low=True, high=True).shared
    assert baja is not None and alta is not None and ambas is not None
    assert (baja.commercial, baja.gas) == (Decimal(40), Decimal(7))
    assert (alta.commercial, alta.gas) == (Decimal(50), Decimal(14))
    assert ambas.commercial == baja.commercial + alta.commercial
    assert ambas.gas == baja.gas + alta.gas


def test_exclusiva_veinte_por_ciento_es_una_hornada_completa() -> None:
    quote = kiln_quote(_vol("20"), Decimal(100), TARIFA_CHICO, low=True, high=False)
    assert quote.exclusive is not None
    assert quote.exclusive.commercial == Decimal(200)


def test_el_alumno_paga_su_tarifa_y_el_gas_es_el_mismo() -> None:
    externo = kiln_quote(Decimal(133100), GRANDE, TARIFA_GRANDE, low=True, high=True).shared
    alumno = kiln_quote(Decimal(133100), GRANDE, ALUMNO_GRANDE, low=True, high=True).shared
    assert externo is not None and alumno is not None
    assert alumno.commercial == Decimal("1996.5")
    assert alumno.gas == externo.gas


def test_sin_tarifa_de_un_ciclo_encendido_no_hay_precio() -> None:
    incompleta = CycleRates(Decimal(200), None, Decimal(35), None)
    assert kiln_quote(_vol("20"), Decimal(100), incompleta, low=True, high=True).shared is None
    solo_baja = kiln_quote(_vol("20"), Decimal(100), incompleta, low=True, high=False)
    assert solo_baja.shared is not None


def test_capacidad_cero_se_rechaza() -> None:
    with pytest.raises(FiringQuotationMathError):
        kiln_quote(Decimal(10), Decimal(0), TARIFA_CHICO, low=True, high=True)


# ---------------------------------------------------------------------------
# Multiproducto y separacion
# ---------------------------------------------------------------------------
def test_tres_productos_suman_su_volumen() -> None:
    piezas = [
        (20, Decimal(18), Decimal(12), Decimal(3)),
        (50, Decimal(1), Decimal(15), Decimal(3)),
        (12, Decimal(15), Decimal(12), Decimal(5)),
    ]
    volumenes = [total_volume(piece_volume(lg, a, h, Decimal(3)), q) for q, lg, a, h in piezas]
    assert volumenes == [Decimal(37800), Decimal(21600), Decimal(25920)]
    assert sum(volumenes) == Decimal(85320)
    cuotas = volume_shares(volumenes)
    assert sum(cuotas) == pytest.approx(Decimal(100), abs=Decimal("0.00001"))


@pytest.mark.parametrize(
    ("separacion", "volumen"),
    [("0", "512"), ("3", "1331"), ("5", "2197"), ("1.5", "857.375")],
)
def test_separacion(separacion: str, volumen: str) -> None:
    assert piece_volume(Decimal(8), Decimal(8), Decimal(8), Decimal(separacion)) == Decimal(volumen)


# ---------------------------------------------------------------------------
# Factor, vidriado y precio
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("valor", ["1", "1.00", "1.10", "1.17", "1.25", "1.5", "2", "2.00"])
def test_factores_validos(valor: str) -> None:
    assert validate_factor(Decimal(valor)) == Decimal(valor)


@pytest.mark.parametrize("valor", ["0.99", "2.01", "0", "3"])
def test_factores_fuera_de_rango(valor: str) -> None:
    with pytest.raises(FiringQuotationMathError):
        validate_factor(Decimal(valor))


def test_el_factor_se_aplica_una_vez_sobre_la_base() -> None:
    precio = service_price(
        firing_commercial=Decimal(1000),
        firing_gas=Decimal(200),
        glaze_material=Decimal(100),
        glaze_labor=Decimal(0),
        factor=Decimal("1.17"),
        tax_percent=Decimal(18),
        rounding_step=Decimal("0.5"),
        exchange_rate=None,
    )
    assert precio.base_amount == Decimal(1100)
    assert precio.commercial_price == Decimal("1287.00")
    assert precio.subtotal == Decimal("1287")
    assert precio.real_cost == Decimal(300)
    assert precio.profit == Decimal(987)


def test_el_gas_no_entra_en_la_base() -> None:
    precio = service_price(
        firing_commercial=Decimal(100),
        firing_gas=Decimal(9999),
        glaze_material=Decimal(0),
        glaze_labor=Decimal(0),
        factor=Decimal(1),
        tax_percent=Decimal(18),
        rounding_step=Decimal("0.5"),
        exchange_rate=None,
    )
    assert precio.base_amount == Decimal(100)
    assert precio.profit < 0


def test_el_redondeo_es_sobre_el_total_al_escalon() -> None:
    precio = service_price(
        firing_commercial=Decimal("100.01"),
        firing_gas=Decimal(0),
        glaze_material=Decimal(0),
        glaze_labor=Decimal(0),
        factor=Decimal(1),
        tax_percent=Decimal(18),
        rounding_step=Decimal("0.5"),
        exchange_rate=None,
    )
    assert precio.subtotal == Decimal("100.5")
    assert precio.total == precio.subtotal + precio.tax


def test_en_dolares_el_subtotal_va_en_dolares_y_la_ganancia_en_soles() -> None:
    precio = service_price(
        firing_commercial=Decimal(350),
        firing_gas=Decimal(35),
        glaze_material=Decimal(0),
        glaze_labor=Decimal(0),
        factor=Decimal(1),
        tax_percent=Decimal(18),
        rounding_step=Decimal("0.5"),
        exchange_rate=Decimal("3.5"),
    )
    assert precio.subtotal == Decimal(100)
    assert precio.profit == Decimal(315)


def test_vidriado_gramos_por_costo() -> None:
    assert glaze_material_cost(Decimal(0), Decimal("0.12")) == Decimal(0)
    assert glaze_material_cost(Decimal(500), Decimal("0.12")) == Decimal(60)
    with pytest.raises(FiringQuotationMathError):
        glaze_material_cost(Decimal(-1), Decimal("0.12"))


def test_el_modulo_no_usa_float() -> None:
    import inspect

    import app.core.firing_quotation_v2 as modulo

    assert "float(" not in inspect.getsource(modulo)
