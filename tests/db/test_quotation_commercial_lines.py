"""Fase 009K.1 — el cargo comercial: entra al total y no toca nada fisico.

Un cargo de prototipo se cobra como una linea mas, pero no es una pieza. Lo
que se comprueba aqui es que participa del dinero —subtotal, IGV, total, PDF—
y que es INVISIBLE para todo lo demas: no genera requerimiento de material, no
mueve inventario y no aparece en una orden de produccion.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockBalance, StockMovement
from app.models.quotations import QuotationCommercialLine
from tests.db.test_production_orders_api import confirmar
from tests.db.test_quotation_builder_api import BUILDER, _complete_payload, head

CARGO = "Prototipo PRT-2026-000099"


def _linea(**cambios: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "PROTOTYPE",
        "description": CARGO,
        "prototype_id": None,
        "quantity": 1,
        "manual_net_amount": "50",
    }
    base.update(cambios)
    return base


async def _borrador(api: httpx.AsyncClient, csrf: str, db_session: AsyncSession) -> dict[str, Any]:
    payload, _productos = await _complete_payload(api, csrf, db_session)
    creada = await api.post(BUILDER, json=payload, headers=head(csrf))
    assert creada.status_code == 201, creada.text
    return dict(creada.json())


async def _prototipo_cualquiera(api: httpx.AsyncClient, csrf: str) -> int:
    """Una muestra minima: el cargo de tipo PROTOTYPE exige apuntar a una."""
    creado = await api.post(
        "/api/v1/prototypes",
        json={"name": "E2E-009K1-cargo", "quantity": 1},
        headers=head(csrf),
    )
    assert creado.status_code == 201, creado.text
    return int(creado.json()["id"])


async def _inventario(db_session: AsyncSession) -> tuple[int, list[str]]:
    db_session.expire_all()
    movimientos = int(await db_session.scalar(select(func.count()).select_from(StockMovement)) or 0)
    saldos = [
        str(fila)
        for fila in (
            await db_session.execute(
                select(
                    StockBalance.product_id, StockBalance.location_id, StockBalance.quantity
                ).order_by(StockBalance.product_id, StockBalance.location_id)
            )
        ).all()
    ]
    return movimientos, saldos


# ---------------------------------------------------------------------------
# Persistencia y validacion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_cargo_se_guarda_con_su_descripcion_congelada(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    borrador = await _borrador(api, admin_csrf, db_session)
    proto = await _prototipo_cualquiera(api, admin_csrf)

    creada = await api.post(
        f"{BUILDER}/{borrador['id']}/commercial-lines",
        json=_linea(prototype_id=proto),
        headers=head(admin_csrf),
    )
    assert creada.status_code == 201, creada.text
    lineas = creada.json()["commercial_lines"]
    assert len(lineas) == 1
    assert lineas[0]["description"] == CARGO
    assert Decimal(lineas[0]["manual_net_amount"]) == Decimal(50)
    assert lineas[0]["prototype_id"] == proto

    db_session.expire_all()
    guardadas = await db_session.scalar(select(func.count()).select_from(QuotationCommercialLine))
    assert guardadas == 1


@pytest.mark.asyncio
async def test_un_cargo_de_una_muestra_inexistente_se_rechaza_con_un_mensaje(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """422 de dominio, no un error de integridad convertido en 500."""
    borrador = await _borrador(api, admin_csrf, db_session)
    respuesta = await api.post(
        f"{BUILDER}/{borrador['id']}/commercial-lines",
        json=_linea(prototype_id=999_999),
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 422, respuesta.text
    assert respuesta.json()["error"]["code"] == "QUOTATION_COMMERCIAL_LINE_PROTOTYPE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize("importe", ["0", "-5"])
async def test_un_cargo_no_puede_ser_gratis_ni_negativo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession, importe: str
) -> None:
    borrador = await _borrador(api, admin_csrf, db_session)
    proto = await _prototipo_cualquiera(api, admin_csrf)
    respuesta = await api.post(
        f"{BUILDER}/{borrador['id']}/commercial-lines",
        json=_linea(prototype_id=proto, manual_net_amount=importe),
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 422, respuesta.text


@pytest.mark.asyncio
async def test_un_cargo_de_prototipo_sin_prototipo_se_rechaza(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    borrador = await _borrador(api, admin_csrf, db_session)
    respuesta = await api.post(
        f"{BUILDER}/{borrador['id']}/commercial-lines",
        json=_linea(prototype_id=None),
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 422, respuesta.text


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_cargo_se_edita_se_borra_y_sobrevive_reabrir(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    borrador = await _borrador(api, admin_csrf, db_session)
    proto = await _prototipo_cualquiera(api, admin_csrf)

    creada = await api.post(
        f"{BUILDER}/{borrador['id']}/commercial-lines",
        json=_linea(prototype_id=proto),
        headers=head(admin_csrf),
    )
    linea_id = creada.json()["commercial_lines"][0]["id"]

    # Reabrir: el cargo sigue ahi con su importe.
    reabierta = await api.get(f"{BUILDER}/{borrador['id']}", headers=head(admin_csrf))
    assert reabierta.status_code == 200
    assert Decimal(reabierta.json()["commercial_lines"][0]["manual_net_amount"]) == Decimal(50)

    editada = await api.put(
        f"{BUILDER}/{borrador['id']}/commercial-lines/{linea_id}",
        json=_linea(prototype_id=proto, manual_net_amount="80"),
        headers=head(admin_csrf),
    )
    assert editada.status_code == 200, editada.text
    assert Decimal(editada.json()["commercial_lines"][0]["manual_net_amount"]) == Decimal(80)

    borrada = await api.delete(
        f"{BUILDER}/{borrador['id']}/commercial-lines/{linea_id}", headers=head(admin_csrf)
    )
    assert borrada.status_code == 200, borrada.text
    assert borrada.json()["commercial_lines"] == []


@pytest.mark.asyncio
async def test_una_cotizacion_confirmada_no_admite_cargos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Lo confirmado es un documento entregado: no se le anaden importes."""
    borrador = await _borrador(api, admin_csrf, db_session)
    proto = await _prototipo_cualquiera(api, admin_csrf)
    creada = await api.post(
        f"{BUILDER}/{borrador['id']}/commercial-lines",
        json=_linea(prototype_id=proto),
        headers=head(admin_csrf),
    )
    linea_id = creada.json()["commercial_lines"][0]["id"]
    confirmada = await confirmar(api, admin_csrf, creada.json())

    for peticion in (
        api.post(
            f"{BUILDER}/{confirmada['id']}/commercial-lines",
            json=_linea(prototype_id=proto),
            headers=head(admin_csrf),
        ),
        api.put(
            f"{BUILDER}/{confirmada['id']}/commercial-lines/{linea_id}",
            json=_linea(prototype_id=proto, manual_net_amount="90"),
            headers=head(admin_csrf),
        ),
        api.delete(
            f"{BUILDER}/{confirmada['id']}/commercial-lines/{linea_id}",
            headers=head(admin_csrf),
        ),
    ):
        respuesta = await peticion
        assert respuesta.status_code == 409, respuesta.text

    db_session.expire_all()
    intactas = await db_session.scalar(select(func.count()).select_from(QuotationCommercialLine))
    assert intactas == 1


# ---------------------------------------------------------------------------
# El dinero
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_cargo_suma_al_total_y_no_lo_multiplica(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """COMMERCIAL_LINE_INCLUDED_IN_HEADER_* y DOUBLE_COUNT = 0.

    El delta del total tiene que ser exactamente el bruto del cargo. Ni el
    doble —que seria contarlo en los dos caminos de salida— ni multiplicado por
    el factor o el margen del producto.
    """
    borrador = await _borrador(api, admin_csrf, db_session)
    proto = await _prototipo_cualquiera(api, admin_csrf)
    neto_antes = Decimal(borrador["quotation_net_total"])
    total_antes = Decimal(borrador["total_with_tax"])

    creada = await api.post(
        f"{BUILDER}/{borrador['id']}/commercial-lines",
        json=_linea(prototype_id=proto),
        headers=head(admin_csrf),
    )
    assert creada.status_code == 201, creada.text
    cuerpo = creada.json()
    cargo = cuerpo["commercial_lines"][0]

    assert Decimal(cuerpo["quotation_net_total"]) == neto_antes + Decimal(cargo["line_total_net"])
    assert Decimal(cuerpo["total_with_tax"]) == total_antes + Decimal(cargo["line_total_gross"])

    # Y el importe del cargo es el que se escribio, no uno factorizado.
    assert Decimal(cargo["manual_net_amount"]) == Decimal(50)

    # Reabrir da los MISMOS totales: si el camino almacenado sumara aparte,
    # aqui aparecerian los cincuenta dos veces.
    reabierta = await api.get(f"{BUILDER}/{borrador['id']}", headers=head(admin_csrf))
    assert Decimal(reabierta.json()["total_with_tax"]) == Decimal(cuerpo["total_with_tax"])
    assert Decimal(reabierta.json()["quotation_net_total"]) == Decimal(
        cuerpo["quotation_net_total"]
    )


# ---------------------------------------------------------------------------
# Lo fisico: nada
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_cargo_no_mueve_un_gramo_de_inventario(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """COMMERCIAL_LINE_INVENTORY_SIDE_EFFECTS: 0, en las tres mutaciones."""
    borrador = await _borrador(api, admin_csrf, db_session)
    proto = await _prototipo_cualquiera(api, admin_csrf)
    antes = await _inventario(db_session)

    creada = await api.post(
        f"{BUILDER}/{borrador['id']}/commercial-lines",
        json=_linea(prototype_id=proto),
        headers=head(admin_csrf),
    )
    linea_id = creada.json()["commercial_lines"][0]["id"]
    assert await _inventario(db_session) == antes

    await api.put(
        f"{BUILDER}/{borrador['id']}/commercial-lines/{linea_id}",
        json=_linea(prototype_id=proto, manual_net_amount="70"),
        headers=head(admin_csrf),
    )
    assert await _inventario(db_session) == antes

    await api.delete(
        f"{BUILDER}/{borrador['id']}/commercial-lines/{linea_id}", headers=head(admin_csrf)
    )
    assert await _inventario(db_session) == antes


# ---------------------------------------------------------------------------
# El documento
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_pdf_muestra_el_cargo_y_no_ensena_nada_interno(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """COMMERCIAL_LINE_PDF_BINARY.

    Se lee el PDF de verdad, no el ViewModel: quien decide lo que acaba
    impreso es WeasyPrint, y una plantilla puede recibir el dato correcto y no
    pintarlo.
    """
    borrador = await _borrador(api, admin_csrf, db_session)
    proto = await _prototipo_cualquiera(api, admin_csrf)
    await api.post(
        f"{BUILDER}/{borrador['id']}/commercial-lines",
        json=_linea(prototype_id=proto),
        headers=head(admin_csrf),
    )

    pdf = await api.get(f"{BUILDER}/{borrador['id']}/pdf-preview", headers=head(admin_csrf))
    assert pdf.status_code == 200, pdf.text
    assert pdf.content[:4] == b"%PDF"

    import io

    texto = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf.content)).pages
    )
    assert CARGO in texto

    # Nada del dominio fisico se cuela en un documento comercial.
    for interno in (
        "PROTOTYPE_OUT",
        "quantity_actual",
        "material_role",
        "technical_cost",
        "production_factor",
    ):
        assert interno not in texto
