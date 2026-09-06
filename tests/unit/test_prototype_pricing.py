"""El costeo de prototipo.

## Sobre el caso de referencia

La hoja «Cotizador Prototipo» del Excel v2 traía un ejemplo completo cuyo
resultado era `800 / 144 / 944 / 9 días`. Ese ejemplo **incluía una hornada**:
350 de tarifa de horno y 3 días de quema.

Una regla de negocio posterior sacó la quema del Cotizador de Prototipos: lo
que se cotiza aquí es la muestra en BARRO, y una muestra en barro no pasa por
el horno. Así que el fixture del Excel dejó de describir el contrato. Los
mismos datos, sin quema, dan:

    240 + 200 + 0 + 10 + 0 = 450  de costo base
    IGV 18 %                =  81
    total                   = 531  (ya cae en el escalón de 0.50)
    plazo 3 + 2 + 0 + 1 + 0 =   6  días

`450 / 81 / 531 / 6` es la referencia canónica actual. El fixture anterior no
se mantiene sólo porque estuviera verde: describía otro negocio.
"""

from __future__ import annotations

import inspect
from dataclasses import fields
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app.core import prototype_pricing
from app.core.prototype_pricing import (
    PrototypeCosting,
    PrototypeCostingInput,
    PrototypeMaterialInput,
    PrototypePricingError,
    price_prototype,
)


def _caso_referencia(**cambios: object) -> PrototypeCostingInput:
    """Taza personalizada, 1 muestra. Los datos del Excel v2 menos la quema."""
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
        "drying_days": Decimal(1),
        "adjustment_days": Decimal(0),
        "fixed_cost": Decimal(0),
        "tax_percent": Decimal(18),
        "rounding_step": Decimal("0.50"),
    }
    base.update(cambios)
    return PrototypeCostingInput(**base)  # type: ignore[arg-type]


def test_el_caso_de_referencia_da_450_81_531_y_6_dias() -> None:
    """PROTOTYPE_REFERENCE: 3x80=240, 2x100=200, matricero 0, 1.25x1x8=10.

    Base 450, IGV 18 % = 81, total 531. Plazo 3+2+0+1+0 = 6.
    """
    resultado = price_prototype(_caso_referencia())

    assert resultado.design_cost == Decimal("240.00")
    assert resultado.artist_cost == Decimal("200.00")
    assert resultado.mold_maker_cost == Decimal("0.00")
    assert resultado.materials_cost == Decimal("10.00")
    assert resultado.base_cost == Decimal("450.00")
    assert resultado.commercial_net_total == Decimal("450.00")
    assert resultado.commercial_tax_total == Decimal("81.00")
    assert resultado.commercial_gross_total == Decimal("531.00")
    assert resultado.total_per_prototype == Decimal("531.00")
    assert resultado.estimated_days == Decimal(6)


def test_la_quema_no_participa_ni_en_el_costo_ni_en_el_plazo() -> None:
    """PROTOTYPE_QUOTATION_USES_KILN: NO.

    No basta con pasar cero: mientras el motor tenga por dónde recibir una
    tarifa de horno, alguien acabará rellenándola. Aquí se comprueba que ni la
    entrada ni la salida tienen dónde meterla, y que el módulo no nombra al
    horno. `Kiln` y `KilnRate` siguen intactos para producción.
    """
    campos = {campo.name for campo in fields(PrototypeCostingInput)} | {
        campo.name for campo in fields(PrototypeCosting)
    }
    assert not [campo for campo in campos if "firing" in campo or "kiln" in campo]

    fuente = inspect.getsource(prototype_pricing)
    for prohibido in ("KilnRate", "firing_cost", "firing_days", "firing_rate"):
        assert prohibido not in fuente, prohibido


def test_el_matricero_cuesta_un_precio_fijo_y_sus_dias_solo_alargan_el_plazo() -> None:
    """El error fácil del modelo: los otros dos conceptos SÍ multiplican.

    `D13 = C13` en la hoja. Cinco días de matricero a 500 cuestan 500, no 2500,
    y esos cinco días se suman al plazo.
    """
    resultado = price_prototype(
        _caso_referencia(mold_maker_price=Decimal(500), mold_maker_days=Decimal(5))
    )

    assert resultado.mold_maker_cost == Decimal("500.00")
    assert resultado.base_cost == Decimal("950.00")
    assert resultado.estimated_days == Decimal(11)


def test_el_material_se_multiplica_por_las_muestras_y_el_total_se_reparte() -> None:
    """Cuatro tazas gastan cuatro veces la pasta; el resto del trabajo no."""
    resultado = price_prototype(_caso_referencia(quantity=4))

    assert resultado.materials_cost == Decimal("40.00")
    assert resultado.base_cost == Decimal("480.00")
    # 480 x 1.18 = 566.40, que no cae en el paso de 0.50 y sube a 566.50.
    assert resultado.raw_gross_total == Decimal("566.40")
    assert resultado.commercial_gross_total == Decimal("566.50")
    assert resultado.total_per_prototype == Decimal("141.63")


def test_ni_factor_ni_margen_tocan_el_costo_base() -> None:
    """PROTOTYPE_PRODUCTION_FACTOR_APPLIED: 0.

    El costo base INTERNO es la suma de conceptos, sin factor ni margen. Si
    alguien enchufa el x3 de producción, este número se triplica.

    Ojo: NO se afirma `neto == costo base`. El neto comercial se reconstruye
    desde el bruto redondeado, así que coincide con el base sólo cuando el
    bruto ya cae en el escalón —como en este caso—.
    """
    resultado = price_prototype(_caso_referencia())
    assert resultado.base_cost == Decimal("450.00")
    assert resultado.raw_gross_total == Decimal("531.00")


def test_el_secado_y_el_ajuste_alargan_el_plazo_sin_costar_dinero() -> None:
    antes = price_prototype(_caso_referencia())
    despues = price_prototype(_caso_referencia(drying_days=Decimal(5), adjustment_days=Decimal(2)))

    assert despues.base_cost == antes.base_cost
    assert despues.estimated_days == antes.estimated_days + Decimal(6)


def test_la_fecha_objetivo_sale_del_plazo_y_no_del_reloj_del_navegador() -> None:
    resultado = price_prototype(_caso_referencia(requested_at=date(2026, 9, 1)))
    assert resultado.target_date == date(2026, 9, 7)


def test_sin_fecha_de_solicitud_no_se_inventa_una_fecha_objetivo() -> None:
    assert price_prototype(_caso_referencia()).target_date is None


def test_el_impuesto_sale_del_parametro_y_no_de_un_0_18_escrito_a_mano() -> None:
    resultado = price_prototype(_caso_referencia(tax_percent=Decimal(0)))
    assert resultado.commercial_tax_total == Decimal("0.00")
    assert resultado.commercial_gross_total == Decimal("450.00")


def test_los_conceptos_cuantizados_suman_exactamente_el_costo_base() -> None:
    """El cliente cuadra el documento sumando lo que ve.

    Con tarifas que dan céntimos partidos, sumar en crudo y cuantizar al final
    daría un total distinto de la suma de las líneas impresas.
    """
    resultado = price_prototype(
        _caso_referencia(design_rate=Decimal("83.333"), artist_rate=Decimal("66.667"))
    )
    suma = (
        resultado.design_cost
        + resultado.artist_cost
        + resultado.mold_maker_cost
        + resultado.materials_cost
        + resultado.fixed_cost
    )
    assert suma == resultado.base_cost


def test_un_bruto_ya_alineado_al_escalon_no_se_mueve() -> None:
    """PROTOTYPE_COMMERCIAL_ROUNDING con el caso de referencia.

    531.00 ya es múltiplo de 0.50, así que el caso canónico sobrevive intacto a
    la política de redondeo.
    """
    resultado = price_prototype(_caso_referencia())
    assert resultado.raw_gross_total == Decimal("531.00")
    assert resultado.commercial_gross_total == Decimal("531.00")
    assert resultado.commercial_net_total == Decimal("450.00")
    assert resultado.commercial_tax_total == Decimal("81.00")


def test_un_bruto_desalineado_sube_al_siguiente_escalon_y_nunca_baja() -> None:
    """CEILING, no HALF_UP: el redondeo comercial sólo sube.

    Con 450.18 de base el bruto matemático es 531.21, que no cae en el paso de
    0.50. Sube a 531.50 —nunca a 531.00—.
    """
    resultado = price_prototype(_caso_referencia(fixed_cost=Decimal("0.18")))
    assert resultado.base_cost == Decimal("450.18")
    assert resultado.raw_gross_total == Decimal("531.21")
    assert resultado.commercial_gross_total == Decimal("531.50")
    assert resultado.commercial_gross_total > resultado.raw_gross_total


def test_el_encabezado_cuadra_siempre_tras_redondear() -> None:
    """PROTOTYPE_HEADER_RECONCILIATION.

    El número que se firma es el bruto. Dejar el neto crudo al lado de un bruto
    redondeado daría un encabezado que no suma, y el cliente lo suma.
    """
    for ajuste in ("0", "0.18", "0.01", "0.49", "0.51", "7.77"):
        resultado = price_prototype(_caso_referencia(fixed_cost=Decimal(ajuste)))
        assert (
            resultado.commercial_net_total + resultado.commercial_tax_total
            == resultado.commercial_gross_total
        ), ajuste
        assert resultado.commercial_gross_total % Decimal("0.50") == 0, ajuste


def test_el_redondeo_no_toca_los_conceptos_internos() -> None:
    """El escalón es una regla COMERCIAL: se aplica al final, una sola vez."""
    alineado = price_prototype(_caso_referencia())
    desalineado = price_prototype(_caso_referencia(fixed_cost=Decimal("0.18")))

    for campo in ("design_cost", "artist_cost", "mold_maker_cost", "materials_cost"):
        assert getattr(alineado, campo) == getattr(desalineado, campo), campo


def test_el_paso_de_redondeo_no_esta_escrito_a_mano_en_el_motor() -> None:
    """PROTOTYPE_ROUNDING_STEP_HARDCODED: NO.

    Con paso 1.00 el mismo caso da otro bruto. Si el motor llevara 0.50 dentro,
    este número no cambiaría.
    """
    resultado = price_prototype(
        _caso_referencia(fixed_cost=Decimal("0.18"), rounding_step=Decimal("1.00"))
    )
    assert resultado.commercial_gross_total == Decimal("532.00")


def test_el_total_por_muestra_sale_del_bruto_comercial() -> None:
    """Y se redondea al mas cercano, no al par.

    566.50 entre cuatro son 141.625. `quantize` por omision usa HALF_EVEN y
    daria 141.62; el motor usa HALF_UP a proposito, que es como se redondea el
    dinero en un documento.
    """
    resultado = price_prototype(_caso_referencia(quantity=4))
    assert resultado.total_per_prototype == (resultado.commercial_gross_total / 4).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def test_sin_muestras_no_hay_cotizacion_que_repartir() -> None:
    with pytest.raises(PrototypePricingError):
        price_prototype(_caso_referencia(quantity=0))


def test_los_dias_negativos_se_rechazan_en_vez_de_restar_plazo() -> None:
    with pytest.raises(PrototypePricingError):
        price_prototype(_caso_referencia(design_days=Decimal(-1)))


def test_una_tarifa_negativa_se_rechaza_en_vez_de_descontar() -> None:
    with pytest.raises(PrototypePricingError):
        price_prototype(_caso_referencia(artist_rate=Decimal(-10)))


def test_sin_materiales_el_costo_de_materiales_es_cero_y_no_falla() -> None:
    """Un prototipo puede cotizarse sin declarar pasta todavía."""
    resultado = price_prototype(_caso_referencia(materials=()))
    assert resultado.materials_cost == Decimal("0.00")
    assert resultado.base_cost == Decimal("440.00")


def test_la_unidad_del_material_viaja_tal_cual_y_no_se_convierte() -> None:
    """No hay g<->ml ni densidad 1: la unidad del catálogo sale intacta."""
    resultado = price_prototype(
        _caso_referencia(
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
    """PEN sigue siendo el caso por defecto."""
    resultado = price_prototype(_caso_referencia())

    assert resultado.currency == "PEN"
    assert resultado.exchange_rate is None
    assert resultado.raw_net_total == resultado.base_cost == Decimal("450.00")


def test_en_dolares_el_neto_se_divide_por_la_tasa_y_el_costo_sigue_en_soles() -> None:
    """450 soles a 4.50 son 100 dólares. IGV 18, bruto 118, ya alineado.

    Multiplicar en vez de dividir daría 2025, que tiene toda la pinta de ser un
    precio y es veinte veces el correcto.
    """
    resultado = price_prototype(_caso_referencia(currency="USD", exchange_rate=Decimal("4.5")))

    assert resultado.base_cost == Decimal("450.00")
    assert resultado.raw_net_total == Decimal("100.00")
    assert resultado.raw_tax == Decimal("18.00")
    assert resultado.commercial_gross_total == Decimal("118.00")
    assert resultado.commercial_net_total == Decimal("100.00")
    assert resultado.currency == "USD"
    assert resultado.exchange_rate == Decimal("4.5")


def test_el_desglose_interno_no_se_convierte_concepto_a_concepto() -> None:
    """Los conceptos son costo, y el costo está en soles.

    Convertirlos uno a uno daría un desglose cuya suma no cuadra con el total
    por los redondeos de cada división.
    """
    resultado = price_prototype(_caso_referencia(currency="USD", exchange_rate=Decimal("4.5")))

    assert resultado.design_cost == Decimal("240.00")
    assert resultado.artist_cost == Decimal("200.00")
    assert resultado.materials_cost == Decimal("10.00")


def test_el_escalon_comercial_se_aplica_sobre_el_bruto_en_dolares() -> None:
    """A 4.00 salen 112.50 netos; el bruto 132.75 sube al escalón 133.00.

    El escalón es una política sobre el número que se firma. Redondear en soles
    y convertir después daría un total en dólares que no termina en escalón.
    """
    resultado = price_prototype(_caso_referencia(currency="USD", exchange_rate=Decimal(4)))

    assert resultado.raw_net_total == Decimal("112.50")
    assert resultado.raw_gross_total == Decimal("132.75")
    assert resultado.commercial_gross_total == Decimal("133.00")
    assert (
        resultado.commercial_net_total + resultado.commercial_tax_total
        == resultado.commercial_gross_total
    )


def test_una_cotizacion_en_dolares_sin_tasa_se_rechaza() -> None:
    """Sin tasa no hay conversión, y cotizar 450 dólares por 450 soles regala
    tres cuartas partes del trabajo."""
    with pytest.raises(Exception, match="EXCHANGE_RATE_REQUIRED"):
        price_prototype(_caso_referencia(currency="USD"))


def test_una_cotizacion_en_soles_con_tasa_se_rechaza() -> None:
    """Guardar una tasa en un documento en soles describiría una conversión que
    nunca ocurrió."""
    with pytest.raises(Exception, match="no lleva tipo de cambio"):
        price_prototype(_caso_referencia(exchange_rate=Decimal(4)))


def test_una_moneda_que_la_casa_no_emite_se_rechaza() -> None:
    with pytest.raises(Exception, match="Moneda no admitida"):
        price_prototype(_caso_referencia(currency="EUR", exchange_rate=Decimal(4)))


def test_una_tasa_de_cero_se_rechaza_en_vez_de_dividir_por_cero() -> None:
    with pytest.raises(Exception, match="mayor que cero"):
        price_prototype(_caso_referencia(currency="USD", exchange_rate=Decimal(0)))


def test_el_total_por_muestra_esta_en_la_moneda_de_emision() -> None:
    """Con dos muestras sólo se duplica el material: los días no se pagan dos
    veces. 240+200+20 = 460 soles, que a 4.00 son 115 dólares; con IGV 135.70,
    que sube al escalón 136.00 y sale a 68.00 por muestra."""
    resultado = price_prototype(
        _caso_referencia(quantity=2, currency="USD", exchange_rate=Decimal(4))
    )

    assert resultado.base_cost == Decimal("460.00")
    assert resultado.raw_net_total == Decimal("115.00")
    assert resultado.commercial_gross_total == Decimal("136.00")
    assert resultado.total_per_prototype == Decimal("68.00")


def test_la_conversion_no_la_reimplementa_este_motor() -> None:
    """AUTHORITY_REUSE.

    El motor de prototipos no puede tener su propia aritmética de cambio: si la
    tuviera, algún día una de las dos cambiaría y ganaría la que nadie mira.
    """
    fuente = inspect.getsource(prototype_pricing)

    assert "convert_net_to_quote_currency" in fuente
    assert "/ entrada.exchange_rate" not in fuente
    assert "* entrada.exchange_rate" not in fuente
