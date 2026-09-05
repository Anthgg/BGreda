"""El costeo de prototipo, contra el caso canonico del Excel v2.

La hoja «Cotizador Prototipo» trae un ejemplo completo con su resultado. Si el
motor y esa hoja discrepan, discrepa el negocio: por eso el caso va primero y
con los numeros exactos, no aproximados.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

import pytest

from app.core import prototype_pricing
from app.core.prototype_pricing import (
    PrototypeCostingInput,
    PrototypeMaterialInput,
    PrototypePricingError,
    price_prototype,
)


def _caso_excel(**cambios: object) -> PrototypeCostingInput:
    """El caso de la hoja «Cotizador Prototipo»: taza personalizada, 1 muestra."""
    base: dict[str, object] = {
        "quantity": 1,
        "design_days": Decimal(3),
        "design_rate": Decimal(80),
        "artist_days": Decimal(2),
        "artist_rate": Decimal(100),
        "mold_maker_price": Decimal(0),
        "mold_maker_days": Decimal(0),
        "materials": (
            PrototypeMaterialInput(
                product_id=1,
                description="Pasta / barro",
                quantity_per_prototype=Decimal("1.25"),
                uom_code="kg",
                unit_cost=Decimal(8),
            ),
        ),
        "firing_rate": Decimal(350),
        "firing_batches": 1,
        "firing_days_per_batch": 3,
        "drying_days": Decimal(1),
        "adjustment_days": Decimal(0),
        "fixed_cost": Decimal(0),
        "tax_percent": Decimal(18),
        "rounding_step": Decimal("0.50"),
    }
    base.update(cambios)
    return PrototypeCostingInput(**base)  # type: ignore[arg-type]


def test_el_caso_canonico_del_excel_da_800_144_944_y_9_dias() -> None:
    """EXCEL_V2_REFERENCE.

    3x80=240, 2x100=200, matricero 0, 1.25x1x8=10, 350x1=350 -> 800.
    IGV 18% = 144. Total 944. Plazo 3+2+0+1+3+0 = 9.
    """
    resultado = price_prototype(_caso_excel())

    assert resultado.design_cost == Decimal("240.00")
    assert resultado.artist_cost == Decimal("200.00")
    assert resultado.mold_maker_cost == Decimal("0.00")
    assert resultado.materials_cost == Decimal("10.00")
    assert resultado.firing_cost == Decimal("350.00")
    assert resultado.base_cost == Decimal("800.00")
    assert resultado.commercial_net_total == Decimal("800.00")
    assert resultado.commercial_tax_total == Decimal("144.00")
    assert resultado.commercial_gross_total == Decimal("944.00")
    assert resultado.total_per_prototype == Decimal("944.00")
    assert resultado.firing_days == 3
    assert resultado.estimated_days == Decimal(9)


def test_el_matricero_cuesta_un_precio_fijo_y_sus_dias_solo_alargan_el_plazo() -> None:
    """El error facil del modelo: los otros dos conceptos SI multiplican.

    `D13 = C13` en la hoja. Cinco dias de matricero a 500 cuestan 500, no 2500,
    y esos cinco dias se suman al plazo.
    """
    resultado = price_prototype(
        _caso_excel(mold_maker_price=Decimal(500), mold_maker_days=Decimal(5))
    )

    assert resultado.mold_maker_cost == Decimal("500.00")
    assert resultado.base_cost == Decimal("1300.00")
    assert resultado.estimated_days == Decimal(14)


def test_el_material_se_multiplica_por_las_muestras_y_el_total_se_reparte() -> None:
    """Cuatro tazas gastan cuatro veces la pasta; el resto del trabajo no."""
    resultado = price_prototype(_caso_excel(quantity=4))

    assert resultado.materials_cost == Decimal("40.00")
    assert resultado.base_cost == Decimal("830.00")
    # 830 x 1.18 = 979.40, que no cae en el paso de 0.50 y sube a 979.50.
    assert resultado.raw_gross_total == Decimal("979.40")
    assert resultado.commercial_gross_total == Decimal("979.50")
    assert resultado.total_per_prototype == Decimal("244.88")


def test_los_dias_de_quema_salen_del_horno_y_no_de_una_constante() -> None:
    """Horno grande: 4 dias por hornada. Dos hornadas, 8 dias y doble tarifa."""
    resultado = price_prototype(
        _caso_excel(firing_days_per_batch=4, firing_batches=2, firing_rate=Decimal(600))
    )

    assert resultado.firing_cost == Decimal("1200.00")
    assert resultado.firing_days == 8
    assert resultado.estimated_days == Decimal(14)


def test_ni_factor_ni_margen_tocan_el_costo_base() -> None:
    """PROTOTYPE_PRODUCTION_FACTOR_APPLIED: 0.

    El costo base INTERNO es la suma de conceptos, sin factor ni margen. Si
    alguien enchufa el x3 de produccion, este numero se triplica.

    Ojo: ya NO se afirma `neto == costo base`. El neto comercial se reconstruye
    desde el bruto redondeado, asi que coincide con el base solo cuando el
    bruto ya cae en el escalon —como en este caso—.
    """
    resultado = price_prototype(_caso_excel())
    assert resultado.base_cost == Decimal("800.00")
    assert resultado.raw_gross_total == Decimal("944.00")


def test_el_secado_y_el_ajuste_alargan_el_plazo_sin_costar_dinero() -> None:
    antes = price_prototype(_caso_excel())
    despues = price_prototype(_caso_excel(drying_days=Decimal(5), adjustment_days=Decimal(2)))

    assert despues.base_cost == antes.base_cost
    assert despues.estimated_days == antes.estimated_days + Decimal(6)


def test_la_fecha_objetivo_sale_del_plazo_y_no_del_reloj_del_navegador() -> None:
    resultado = price_prototype(_caso_excel(requested_at=date(2026, 9, 1)))
    assert resultado.target_date == date(2026, 9, 10)


def test_sin_fecha_de_solicitud_no_se_inventa_una_fecha_objetivo() -> None:
    assert price_prototype(_caso_excel()).target_date is None


def test_el_impuesto_sale_del_parametro_y_no_de_un_0_18_escrito_a_mano() -> None:
    resultado = price_prototype(_caso_excel(tax_percent=Decimal(0)))
    assert resultado.commercial_tax_total == Decimal("0.00")
    assert resultado.commercial_gross_total == Decimal("800.00")


def test_los_conceptos_cuantizados_suman_exactamente_el_costo_base() -> None:
    """El cliente cuadra el documento sumando lo que ve.

    Con tarifas que dan centimos partidos, sumar en crudo y cuantizar al final
    daria un total distinto de la suma de las lineas impresas.
    """
    resultado = price_prototype(
        _caso_excel(design_rate=Decimal("83.333"), artist_rate=Decimal("66.667"))
    )
    suma = (
        resultado.design_cost
        + resultado.artist_cost
        + resultado.mold_maker_cost
        + resultado.materials_cost
        + resultado.firing_cost
        + resultado.fixed_cost
    )
    assert suma == resultado.base_cost


def test_un_bruto_ya_alineado_al_escalon_no_se_mueve() -> None:
    """PROTOTYPE_COMMERCIAL_ROUNDING con el caso del Excel.

    944.00 ya es multiplo de 0.50, asi que el fixture del Excel sobrevive
    intacto a la politica de redondeo.
    """
    resultado = price_prototype(_caso_excel())
    assert resultado.raw_gross_total == Decimal("944.00")
    assert resultado.commercial_gross_total == Decimal("944.00")
    assert resultado.commercial_net_total == Decimal("800.00")
    assert resultado.commercial_tax_total == Decimal("144.00")


def test_un_bruto_desalineado_sube_al_siguiente_escalon_y_nunca_baja() -> None:
    """CEILING, no HALF_UP: el redondeo comercial solo sube.

    Con 800.18 de base el bruto matematico es 944.21, que no cae en el paso de
    0.50. Sube a 944.50 —nunca a 944.00—.
    """
    resultado = price_prototype(_caso_excel(fixed_cost=Decimal("0.18")))
    assert resultado.base_cost == Decimal("800.18")
    assert resultado.raw_gross_total == Decimal("944.21")
    assert resultado.commercial_gross_total == Decimal("944.50")
    assert resultado.commercial_gross_total > resultado.raw_gross_total


def test_el_encabezado_cuadra_siempre_tras_redondear() -> None:
    """PROTOTYPE_HEADER_RECONCILIATION.

    El numero que se firma es el bruto. Dejar el neto crudo al lado de un bruto
    redondeado daria un encabezado que no suma, y el cliente lo suma.
    """
    for ajuste in ("0", "0.18", "0.01", "0.49", "0.51", "7.77"):
        resultado = price_prototype(_caso_excel(fixed_cost=Decimal(ajuste)))
        assert (
            resultado.commercial_net_total + resultado.commercial_tax_total
            == resultado.commercial_gross_total
        ), ajuste
        assert resultado.commercial_gross_total % Decimal("0.50") == 0, ajuste


def test_el_redondeo_no_toca_los_conceptos_internos() -> None:
    """El escalon es una regla COMERCIAL: se aplica al final, una sola vez."""
    alineado = price_prototype(_caso_excel())
    desalineado = price_prototype(_caso_excel(fixed_cost=Decimal("0.18")))

    for campo in ("design_cost", "artist_cost", "mold_maker_cost", "materials_cost", "firing_cost"):
        assert getattr(alineado, campo) == getattr(desalineado, campo), campo


def test_el_paso_de_redondeo_no_esta_escrito_a_mano_en_el_motor() -> None:
    """PROTOTYPE_ROUNDING_STEP_HARDCODED: NO.

    Con paso 1.00 el mismo caso da otro bruto. Si el motor llevara 0.50 dentro,
    este numero no cambiaria.
    """
    resultado = price_prototype(
        _caso_excel(fixed_cost=Decimal("0.18"), rounding_step=Decimal("1.00"))
    )
    assert resultado.commercial_gross_total == Decimal("945.00")


def test_el_total_por_muestra_sale_del_bruto_comercial() -> None:
    resultado = price_prototype(_caso_excel(quantity=4))
    assert resultado.total_per_prototype == (resultado.commercial_gross_total / 4).quantize(
        Decimal("0.01")
    )


def test_sin_muestras_no_hay_cotizacion_que_repartir() -> None:
    with pytest.raises(PrototypePricingError):
        price_prototype(_caso_excel(quantity=0))


def test_los_dias_negativos_se_rechazan_en_vez_de_restar_plazo() -> None:
    with pytest.raises(PrototypePricingError):
        price_prototype(_caso_excel(design_days=Decimal(-1)))


def test_una_tarifa_negativa_se_rechaza_en_vez_de_descontar() -> None:
    with pytest.raises(PrototypePricingError):
        price_prototype(_caso_excel(artist_rate=Decimal(-10)))


def test_sin_materiales_el_costo_de_materiales_es_cero_y_no_falla() -> None:
    """Un prototipo puede cotizarse sin declarar pasta todavia."""
    resultado = price_prototype(_caso_excel(materials=()))
    assert resultado.materials_cost == Decimal("0.00")
    assert resultado.base_cost == Decimal("790.00")


def test_la_unidad_del_material_viaja_tal_cual_y_no_se_convierte() -> None:
    """No hay g<->ml ni densidad 1: la unidad del catalogo sale intacta."""
    resultado = price_prototype(
        _caso_excel(
            materials=(
                PrototypeMaterialInput(
                    product_id=9,
                    description="Barniz",
                    quantity_per_prototype=Decimal(30),
                    uom_code="g",
                    unit_cost=Decimal("0.02"),
                ),
            )
        )
    )
    assert resultado.materials[0].uom_code == "g"
    assert resultado.materials[0].total_quantity == Decimal(30)
    assert resultado.materials_cost == Decimal("0.60")


# -- Moneda de emision -------------------------------------------------------
#
# Paridad con el Cotizador principal: la casa cotiza en soles o en dolares, y un
# prototipo no es menos documento que una pieza de catalogo. Lo que se convierte
# es el PRECIO; el costo se queda en soles, porque en soles se paga al artista y
# se compra el barro.


def test_por_omision_se_cotiza_en_soles_y_sin_tasa() -> None:
    """Nada de lo anterior cambia: PEN sigue siendo el caso por defecto."""
    resultado = price_prototype(_caso_excel())

    assert resultado.currency == "PEN"
    assert resultado.exchange_rate is None
    assert resultado.raw_net_total == resultado.base_cost == Decimal("800.00")


def test_en_dolares_el_neto_se_divide_por_la_tasa_y_el_costo_sigue_en_soles() -> None:
    """800 soles a 4.00 son 200 dolares. IGV 36, bruto 236, ya alineado.

    Multiplicar en vez de dividir daria 3200, que tiene toda la pinta de ser un
    precio y es dieciseis veces el correcto.
    """
    resultado = price_prototype(_caso_excel(currency="USD", exchange_rate=Decimal(4)))

    assert resultado.base_cost == Decimal("800.00")
    assert resultado.raw_net_total == Decimal("200.00")
    assert resultado.raw_tax == Decimal("36.00")
    assert resultado.commercial_gross_total == Decimal("236.00")
    assert resultado.commercial_net_total == Decimal("200.00")
    assert resultado.currency == "USD"
    assert resultado.exchange_rate == Decimal(4)


def test_el_desglose_interno_no_se_convierte_concepto_a_concepto() -> None:
    """Los conceptos son costo, y el costo esta en soles.

    Convertirlos uno a uno daria un desglose cuya suma no cuadra con el total
    por los redondeos de cada division.
    """
    resultado = price_prototype(_caso_excel(currency="USD", exchange_rate=Decimal(4)))

    assert resultado.design_cost == Decimal("240.00")
    assert resultado.artist_cost == Decimal("200.00")
    assert resultado.materials_cost == Decimal("10.00")
    assert resultado.firing_cost == Decimal("350.00")


def test_el_escalon_comercial_se_aplica_sobre_el_bruto_en_dolares() -> None:
    """A 3.75 salen 213.33 netos; el bruto 251.73 sube al escalon 252.00.

    El escalon es una politica sobre el numero que se firma. Redondear en soles
    y convertir despues daria un total en dolares que no termina en escalon.
    """
    resultado = price_prototype(_caso_excel(currency="USD", exchange_rate=Decimal("3.75")))

    assert resultado.raw_net_total == Decimal("213.33")
    assert resultado.raw_gross_total == Decimal("251.73")
    assert resultado.commercial_gross_total == Decimal("252.00")
    assert (
        resultado.commercial_net_total + resultado.commercial_tax_total
        == resultado.commercial_gross_total
    )


def test_una_cotizacion_en_dolares_sin_tasa_se_rechaza() -> None:
    """Sin tasa no hay conversion, y cotizar 800 dolares por 800 soles regala
    tres cuartas partes del trabajo."""
    with pytest.raises(Exception, match="EXCHANGE_RATE_REQUIRED"):
        price_prototype(_caso_excel(currency="USD"))


def test_una_cotizacion_en_soles_con_tasa_se_rechaza() -> None:
    """Guardar una tasa en un documento en soles describiria una conversion que
    nunca ocurrio."""
    with pytest.raises(Exception, match="no lleva tipo de cambio"):
        price_prototype(_caso_excel(exchange_rate=Decimal(4)))


def test_una_moneda_que_la_casa_no_emite_se_rechaza() -> None:
    with pytest.raises(Exception, match="Moneda no admitida"):
        price_prototype(_caso_excel(currency="EUR", exchange_rate=Decimal(4)))


def test_una_tasa_de_cero_se_rechaza_en_vez_de_dividir_por_cero() -> None:
    with pytest.raises(Exception, match="mayor que cero"):
        price_prototype(_caso_excel(currency="USD", exchange_rate=Decimal(0)))


def test_el_total_por_muestra_esta_en_la_moneda_de_emision() -> None:
    """Con dos muestras solo se duplica el material: los dias no se pagan dos
    veces. 240+200+20+350 = 810 soles, que a 4.00 son 202.50 dolares; con IGV
    238.95, que sube al escalon 239.00 y sale a 119.50 por muestra."""
    resultado = price_prototype(_caso_excel(quantity=2, currency="USD", exchange_rate=Decimal(4)))

    assert resultado.base_cost == Decimal("810.00")
    assert resultado.raw_net_total == Decimal("202.50")
    assert resultado.commercial_gross_total == Decimal("239.00")
    assert resultado.total_per_prototype == Decimal("119.50")


def test_la_conversion_no_la_reimplementa_este_motor() -> None:
    """AUTHORITY_REUSE.

    El motor de prototipos no puede tener su propia aritmetica de cambio: si la
    tuviera, algun dia una de las dos cambiaria y ganaria la que nadie mira.
    """
    fuente = inspect.getsource(prototype_pricing)

    assert "convert_net_to_quote_currency" in fuente
    assert "/ entrada.exchange_rate" not in fuente
    assert "* entrada.exchange_rate" not in fuente
