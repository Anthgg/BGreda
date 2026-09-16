"""Fase 010H — el PDF del cliente V2: lo permitido esta, lo prohibido no.

Se llenan TODAS las columnas internas de la cotizacion y de sus lineas con
cifras reconocibles —costo real, gas, tarifas, factor, ganancia, margen, costo
por gramo, jornal— y se comprueba que ninguna llega al documento. No basta con
que la plantilla «no las pinte»: un campo nuevo en el ViewModel las pintaria
manana sin que nadie tocara esta prueba.

Se mira el HTML y, si WeasyPrint puede dibujar en esta maquina, tambien el
texto extraido del PDF real.
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.documents.common import CompanyDocInfo
from app.models.quoter_v2 import V2Quotation, V2QuotationProduct, V2QuotationStatus
from app.services.quotation_pdf import QuotationPdfService
from app.services.quoter_v2_pdf import (
    V2QuotationPdfDraftBlockedError,
    V2QuotationPdfNotIssuedError,
    build_v2_pdf_document,
)

#: Terminos que el cliente no puede leer. Se comparan sin tildes ni mayusculas.
PROHIBIDOS = (
    "costo real",
    "costo de produccion",
    "costo produccion",
    "gas real",
    "gas",
    "tarifa por hora",
    "tarifa/h",
    "tarifa interna",
    "diferencia de quema",
    "ganancia",
    "margen",
    "utilidad",
    "sueldo",
    "jornal",
    "rendimiento",
    "stock",
    "existencia",
    "costo por gramo",
    "costo/g",
    "administracion",
    "espacio",
    "factor",
    "x2",
    "x3",
    "×2",  # noqa: RUF001 - el signo real que un documento podria imprimir
    "×3",  # noqa: RUF001
    "precio minimo",
    "precio objetivo",
    "notas internas",
    "huella",
    "fingerprint",
)

#: Cifras internas sembradas. Ninguna coincide con un importe comercial.
INTERNAS = {
    "real_cost_total": Decimal("777.111111"),
    "production_cost_total": Decimal("888.222222"),
    "price_min": Decimal("1776.444444"),
    "price_target": Decimal("2664.666666"),
    "negotiated_price": Decimal("2664.666666"),
    "estimated_profit": Decimal("4321.987654"),
    "effective_margin_percent": Decimal("61.234567"),
    "materials_cost_total": Decimal("123.456789"),
    "labor_cost_total": Decimal("234.567891"),
    "space_cost": Decimal("560"),
    "administrative_cost_snapshot": Decimal("200"),
    "space_service_cost_per_day_snapshot": Decimal("140"),
    "firing_gas_total": Decimal("70"),
    "firing_commercial_total": Decimal("450"),
    "gas_cost_low_snapshot": Decimal("35"),
    "gas_cost_high_snapshot": Decimal("70"),
    "commercial_rate_low_snapshot": Decimal("200"),
    "commercial_rate_high_snapshot": Decimal("250"),
    "commercial_factor": Decimal("3"),
    "commercial_factor_min_snapshot": Decimal("2"),
    "commercial_factor_max_snapshot": Decimal("3"),
    "illustration_cost": Decimal("95.555555"),
    "rounding_adjustment": Decimal("3.141592"),
}

SECRETO_NOTAS = "NOTA-INTERNA-NO-IMPRIMIR-9f3a"


def _sin_tildes(texto: str) -> str:
    return (
        texto.lower()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
    )


def _cotizacion(**overrides: object) -> tuple[V2Quotation, list[V2QuotationProduct]]:
    q = V2Quotation(
        id=41,
        code="CTZ-V2-2026-000041",
        status=V2QuotationStatus.CONFIRMED,
        customer_name_snapshot="Cerámicas del Sur SAC",
        customer_document_type_snapshot="RUC",
        customer_document_number_snapshot="20123456789",
        customer_address_snapshot="Av. Arcilla 123, Lima",
        customer_email_snapshot="compras@ceramicasdelsur.pe",
        customer_phone_snapshot="999111222",
        name="Pedido feria octubre",
        notes=SECRETO_NOTAS,
        client_notes="Entrega en taller.",
        conditions_snapshot="Adelanto del 50 %.",
        payment_notes_snapshot="Transferencia bancaria.",
        currency_code_snapshot="PEN",
        currency_symbol_snapshot="S/",
        exchange_rate_snapshot=None,
        tax_percent_snapshot=Decimal("18"),
        rounding_step_snapshot=Decimal("0.5"),
        validity_days_snapshot=20,
        issued_at=datetime(2026, 9, 10, 16, 0, tzinfo=UTC),
        valid_until=date(2026, 9, 30),
        expires_at=datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
        issued_by=uuid.uuid4(),
        issued_by_name="Ana Emisora",
        created_by_name="Creador",
        subtotal_amount=Decimal("1350.000000"),
        tax_amount=Decimal("243.000000"),
        total_amount=Decimal("1593.000000"),
        **INTERNAS,
    )
    for clave, valor in overrides.items():
        setattr(q, clave, valor)
    lineas = [
        V2QuotationProduct(
            id=1,
            sort_order=0,
            product_name_snapshot="Plato hondo",
            quantity=100,
            length_cm=Decimal("22"),
            width_cm=Decimal("22"),
            height_cm=Decimal("5"),
            unit_price=Decimal("9.500000"),
            unit_price_raw=Decimal("9.374219"),
            line_subtotal=Decimal("950"),
            line_tax=Decimal("171"),
            line_total=Decimal("1121"),
            client_observation="Esmalte azul cobalto.",
            body_cost_per_unit_snapshot=Decimal("0.0013"),
            glaze_cost_per_unit_snapshot=Decimal("0.1234"),
            body_cost=Decimal("321.654987"),
            direct_cost=Decimal("456.789123"),
            allocated_production_cost=Decimal("312.456789"),
            allocated_real_cost=Decimal("298.765432"),
            allocated_profit=Decimal("651.234568"),
            line_price=Decimal("937.370367"),
        ),
        V2QuotationProduct(
            id=2,
            sort_order=1,
            product_name_snapshot="Taza de té",
            quantity=50,
            length_cm=Decimal("9"),
            width_cm=Decimal("9"),
            height_cm=Decimal("8"),
            unit_price=Decimal("8.000000"),
            unit_price_raw=Decimal("7.812345"),
            line_subtotal=Decimal("400"),
            line_tax=Decimal("72"),
            line_total=Decimal("472"),
            client_observation=None,
        ),
    ]
    return q, lineas


def _html(q: V2Quotation, lineas: list[V2QuotationProduct], *, expired: bool = False) -> str:
    doc = build_v2_pdf_document(
        q,
        lineas,
        company=CompanyDocInfo(trade_name="Taller Greda"),
        bank_accounts=[],
        document_footer=None,
        expired=expired,
    )
    return QuotationPdfService(session=None).render_html(doc)  # type: ignore[arg-type]


def _texto_visible(html: str) -> str:
    """El texto que se lee: sin etiquetas, sin CSS y sin comentarios."""
    sin_estilos = re.sub(r"<style.*?</style>", " ", html, flags=re.S)
    sin_etiquetas = re.sub(r"<[^>]+>", " ", sin_estilos)
    return re.sub(r"\s+", " ", sin_etiquetas)


# ---------------------------------------------------------------------------
# Lo prohibido
# ---------------------------------------------------------------------------
class TestProhibido:
    def test_ningun_termino_interno_en_el_texto(self) -> None:
        q, lineas = _cotizacion()
        texto = _sin_tildes(_texto_visible(_html(q, lineas)))
        for termino in PROHIBIDOS:
            assert not re.search(rf"\b{re.escape(_sin_tildes(termino))}\b", texto), termino

    def test_ninguna_cifra_interna_en_el_html(self) -> None:
        """Las cifras internas sembradas tienen decimales imposibles de confundir."""
        q, lineas = _cotizacion()
        html = _html(q, lineas)
        for secreto in (
            "777.11",
            "888.22",
            "1,776.44",
            "1776.44",
            "2,664.66",
            "2664.66",
            "2,664.67",
            "4,321.98",
            "4321.98",
            "61.23",
            "123.45",
            "234.56",
            "95.55",
            "95.56",
            "3.14",
            "321.65",
            "456.78",
            "312.45",
            "298.76",
            "651.23",
            "937.37",
            "9.37",
            "7.81",
            "0.0013",
            "0.1234",
        ):
            assert secreto not in _texto_visible(html), secreto

    def test_las_notas_internas_no_salen(self) -> None:
        q, lineas = _cotizacion()
        assert SECRETO_NOTAS not in _html(q, lineas)

    def test_el_nombre_interno_de_la_cotizacion_no_sale(self) -> None:
        """La pantalla lo declara «para reconocerla en el listado»."""
        q, lineas = _cotizacion(name="Cliente pesado, apretar precio")
        texto = _texto_visible(_html(q, lineas))
        assert "apretar precio" not in texto
        assert "Referencia / Nombre" not in texto

    def test_sin_imagenes_de_producto(self) -> None:
        q, lineas = _cotizacion()
        html = _html(q, lineas)
        # El unico <img> admisible es el logo, y aqui no hay logo.
        assert "<img" not in html


# ---------------------------------------------------------------------------
# Lo permitido
# ---------------------------------------------------------------------------
class TestPermitido:
    def test_contiene_lo_comercial(self) -> None:
        q, lineas = _cotizacion()
        texto = _texto_visible(_html(q, lineas))
        for esperado in (
            "CTZ-V2-2026-000041",
            "Cerámicas del Sur SAC",
            "20123456789",
            "Plato hondo",
            "Taza de té",
            "Largo: 22 cm",
            "Alto: 5 cm",
            "100",
            "50",
            "S/ 9.50",
            "S/ 8.00",
            "S/ 950.00",
            "S/ 400.00",
            "S/ 171.00",
            "S/ 1,121.00",
            "S/ 1,350.00",
            "S/ 243.00",
            "S/ 1,593.00",
            "IGV (18%)",
            "PEN",
            "30/09/2026",
            "Válida hasta el 30/09/2026",
            "10/09/2026",
            "Esmalte azul cobalto.",
            "Entrega en taller.",
            "Adelanto del 50 %.",
            "Transferencia bancaria.",
            "Ana Emisora",
        ):
            assert esperado in texto, esperado

    def test_la_fecha_de_emision_es_la_de_lima(self) -> None:
        """Emitida a las 23:30 de Lima: en UTC ya es el dia siguiente."""
        q, lineas = _cotizacion(issued_at=datetime(2026, 9, 11, 4, 30, tzinfo=UTC))
        texto = _texto_visible(_html(q, lineas))
        assert "10/09/2026" in texto
        assert "11/09/2026" not in texto

    def test_usd_conserva_el_tipo_de_cambio_congelado(self) -> None:
        q, lineas = _cotizacion(
            currency_code_snapshot="USD",
            currency_symbol_snapshot="US$",
            exchange_rate_snapshot=Decimal("3.700000"),
        )
        texto = _texto_visible(_html(q, lineas))
        assert "USD" in texto
        assert "3.70" in texto
        assert "US$ 9.50" in texto

    def test_vencida_se_dice_con_texto(self) -> None:
        q, lineas = _cotizacion()
        assert "COTIZACIÓN VENCIDA" in _texto_visible(_html(q, lineas, expired=True))
        assert "COTIZACIÓN VENCIDA" not in _texto_visible(_html(q, lineas, expired=False))

    def test_anulada_manda_sobre_vencida(self) -> None:
        q, lineas = _cotizacion(
            status=V2QuotationStatus.CANCELLED, cancelled_at=datetime(2026, 10, 2, tzinfo=UTC)
        )
        texto = _texto_visible(_html(q, lineas, expired=True))
        assert "COTIZACIÓN ANULADA" in texto
        assert "COTIZACIÓN VENCIDA" not in texto

    def test_un_borrador_no_tiene_documento(self) -> None:
        q, lineas = _cotizacion(status=V2QuotationStatus.DRAFT, issued_at=None)
        with pytest.raises(V2QuotationPdfDraftBlockedError):
            _html(q, lineas)

    def test_una_cancelada_que_nunca_se_emitio_no_tiene_documento(self) -> None:
        q, lineas = _cotizacion(status=V2QuotationStatus.CANCELLED, issued_at=None)
        with pytest.raises(V2QuotationPdfNotIssuedError):
            _html(q, lineas)


# ---------------------------------------------------------------------------
# Historico
# ---------------------------------------------------------------------------
class TestHistorico:
    def test_regenerar_da_el_mismo_documento(self) -> None:
        q, lineas = _cotizacion()
        assert _html(q, lineas) == _html(q, lineas)

    def test_no_lee_ningun_maestro(self) -> None:
        """El constructor es puro: sin sesion, sin cliente ni producto cargados."""
        q, lineas = _cotizacion()
        assert q.customer is None
        assert "Cerámicas del Sur SAC" in _html(q, lineas)


class TestLegacyIntacto:
    def test_la_tabla_compartida_no_gana_columnas_por_defecto(self) -> None:
        """Legacy y CPR no declaran `show_line_tax`: su tabla sigue igual."""
        from app.documents.quotation import QuotationDocItem, QuotationPdfDocument

        doc = QuotationPdfDocument(
            company=CompanyDocInfo(trade_name="Taller"),
            customer=__import__("app.documents.quotation", fromlist=["x"]).CustomerDocInfo(
                name="Cliente"
            ),
            document=__import__("app.documents.quotation", fromlist=["x"]).DocumentHeaderInfo(
                code="CTZ-000001"
            ),
            items=[QuotationDocItem(item_number=1, product_name="Jarra")],
        )
        html = QuotationPdfService(session=None).render_html(doc)  # type: ignore[arg-type]
        assert 'class="col-tax"' not in html
        assert '<table class="doc-table quotation-items">' in html
        assert "COTIZACIÓN VENCIDA" not in html


# ---------------------------------------------------------------------------
# El PDF real
# ---------------------------------------------------------------------------
def test_el_pdf_real_no_contiene_datos_internos() -> None:
    """Texto extraido del binario. Se omite si WeasyPrint no puede dibujar aqui."""
    q, lineas = _cotizacion()
    html = _html(q, lineas)
    try:
        pdf = QuotationPdfService(session=None).render_pdf_from_html(html)  # type: ignore[arg-type]
    except OSError as error:  # pragma: no cover - depende de las librerias del sistema
        pytest.skip(f"WeasyPrint no puede dibujar en esta maquina: {error}")
    from pypdf import PdfReader

    texto = " ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages)
    compacto = _sin_tildes(re.sub(r"\s+", "", texto))
    assert "ctz-v2-2026-000041" in compacto
    assert "cer" in compacto and "micasdelsur" in compacto
    assert "1,593.00" in compacto
    for termino in (
        "costoreal",
        "costodeproduccion",
        "gasreal",
        "ganancia",
        "margen",
        "tarifaporhora",
        "factor",
        "rendimiento",
        "stock",
    ):
        assert termino not in compacto, termino
    assert SECRETO_NOTAS.lower().replace("-", "") not in compacto.replace("-", "")
