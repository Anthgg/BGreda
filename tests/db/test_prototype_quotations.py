"""El Cotizador de Prototipos contra PostgreSQL real.

Aqui se prueba lo que una base en memoria no puede demostrar: que la migracion
0023 deja el esquema que dice dejar, que el correlativo aguanta veinte
peticiones a la vez, que un documento emitido no cambia aunque cambie la
configuracion, y que cobrar habilita la produccion sin gastar un gramo.

Todo se comprueba RELEYENDO de la base. Una respuesta puede devolver lo que se
le mando en vez de lo que se guardo.
"""

from __future__ import annotations

import asyncio
import io
import re
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.prototype_quotation_pdf
from app.models.inventory import StockMovement
from app.models.masters import Product
from app.models.prototype_quotations import (
    PrototypeQuotation,
    PrototypeQuotationMaterial,
)
from app.models.prototypes import Prototype
from app.models.settings import CommercialSettings
from tests.db.test_masters_api import create_category, create_product
from tests.db.test_quotation_builder_api import head

COTIZADOR = "/api/v1/prototype-quotations"


async def cobrar(
    api: httpx.AsyncClient,
    csrf: str,
    quotation_id: int,
    *,
    stock_location_id: int | None = None,
) -> httpx.Response:
    """Cobra una cotizacion de prototipo diciendo de que almacen sale.

    Fase 009K.4: el almacen es obligatorio y explicito. Se crea uno propio
    cuando la prueba no trae el suyo, porque el punto de estas pruebas es el
    cobro, no de donde sale el barro; las que SI comprueban el almacen lo pasan
    a mano.
    """
    if stock_location_id is None:
        creada = await api.post(
            "/api/v1/inventory/locations",
            json={"name": f"Almacen cobro {quotation_id}-{uuid4().hex[:8]}"},
            headers=head(csrf),
        )
        assert creada.status_code == 201, creada.text
        stock_location_id = int(creada.json()["id"])
    return await api.post(
        f"{COTIZADOR}/{quotation_id}/mark-paid",
        json={"stock_location_id": stock_location_id},
        headers=head(csrf),
    )


#: Se miran al importar, no dentro de una prueba asincrona: tocar el disco
#: desde una corrutina bloquearia el bucle de eventos.
_PLANTILLAS = Path(app.services.prototype_quotation_pdf.__file__).resolve().parents[1]
_EXISTE_PLANTILLA_PROPIA = (_PLANTILLAS / "templates" / "prototype_quotations").exists()
_EXISTE_DOCUMENTO_PROPIO = (_PLANTILLAS / "documents" / "prototype_quotation.py").exists()
PARTNERS = "/api/v1/partners"


# ---------------------------------------------------------------------------
# Montaje
# ---------------------------------------------------------------------------
async def _cliente(api: httpx.AsyncClient, csrf: str, sufijo: str) -> dict[str, Any]:
    respuesta = await api.post(
        PARTNERS,
        json={
            "name": f"Cliente prototipo{sufijo}",
            "role": "CLIENT",
            "document_type": "RUC",
            "document_number": f"206{abs(hash(sufijo)) % 100000000:08d}",
        },
        headers=head(csrf),
    )
    assert respuesta.status_code == 201, respuesta.text
    return dict(respuesta.json())


async def _pasta(
    api: httpx.AsyncClient, csrf: str, sufijo: str, *, costo: str = "8"
) -> dict[str, Any]:
    """Un barro con costo real en el catalogo, en kg."""
    categoria = await create_category(api, csrf, f"Pastas{sufijo}")
    respuesta = await create_product(
        api,
        csrf,
        product_category_id=categoria["id"],
        product_type="RAW_MATERIAL",
        name=f"Pasta prototipo{sufijo}",
        base_uom_code="kg",
        cost=costo,
    )
    assert respuesta.status_code == 201, respuesta.text
    return dict(respuesta.json())


async def _ajustes(db_session: AsyncSession, **valores: Any) -> None:
    """Escribe la configuracion comercial de la casa."""
    fila = await db_session.scalar(select(CommercialSettings).limit(1))
    assert fila is not None
    for campo, valor in valores.items():
        setattr(fila, campo, valor)
    await db_session.commit()


async def _caso_referencia(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, sufijo: str
) -> dict[str, Any]:
    """El caso de referencia, montado con maestros reales.

    3 dias de diseno a 80, 2 de artista a 100, 1.25 kg de pasta a 8/kg y un dia
    de secado. Debe dar 450 / 81 / 531 / 6.

    El ejemplo del Excel v2 daba 800 / 144 / 944 / 9 porque incluia una hornada
    de 350 y 3 dias de quema. Una regla de negocio posterior saco la quema del
    Cotizador de Prototipos —lo que se cotiza es la muestra en BARRO—, asi que
    ese fixture dejo de describir el contrato.
    """
    await _ajustes(
        db_session,
        prototype_design_rate=Decimal(80),
        prototype_artist_rate=Decimal(100),
        prototype_mold_maker_price=Decimal(0),
        prototype_mold_maker_days=Decimal(0),
        prototype_fixed_cost=Decimal(0),
        tax_percent=Decimal(18),
        rounding_step=Decimal("0.50"),
    )
    cliente = await _cliente(api, csrf, sufijo)
    pasta = await _pasta(api, csrf, sufijo)
    familia = await create_category(api, csrf, f"Piezas{sufijo}")
    return {
        "customer_id": cliente["id"],
        "product_category_id": familia["id"],
        "description": "Taza personalizada",
        "quantity": 1,
        "width_cm": "15",
        "length_cm": "15",
        "height_cm": "20",
        "design_days": "3",
        "artist_days": "2",
        "drying_days": "1",
        "materials": [
            {
                "product_id": pasta["id"],
                "quantity_per_prototype": "1.25",
                "is_body_material": True,
            }
        ],
        "_pasta": pasta,
        "_familia": familia,
    }


def _payload(caso: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in caso.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# PARTE C — la migracion dejo el esquema que dice
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_esquema_tiene_las_tablas_columnas_y_restricciones(
    db_session: AsyncSession,
) -> None:
    """El esquema que producen los modelos, leido del catalogo de PostgreSQL.

    Ojo con lo que esto prueba y lo que no. La base de pruebas se crea desde
    los modelos con `create_all`: aqui NO corre ninguna migracion. Que la 0023
    deje este mismo esquema lo demuestran su ejecucion real en el CI —la linea
    «Running upgrade 0022 -> 0023»— y sus siete auto-guardas.

    Lo que si se comprueba aqui es que los modelos declaran de verdad la
    nulabilidad, los tipos y las restricciones que el dominio necesita: sin
    esto, la migracion podria ser perfecta y el modelo mentir.
    """
    tablas = set(
        (
            await db_session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_name IN ('prototype_quotations', 'prototype_quotation_materials')"
                )
            )
        ).scalars()
    )
    assert tablas == {"prototype_quotations", "prototype_quotation_materials"}

    columnas = {
        nombre: (tipo, nulo, defecto)
        for nombre, tipo, nulo, defecto in (
            await db_session.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_name = 'prototype_quotations'"
                )
            )
        ).all()
    }
    # El codigo nace nulo: un borrador que nunca se emite no gasta numero.
    assert columnas["code"][1] == "YES"
    assert columnas["status"][1] == "NO"
    assert columnas["commercial_gross_total"][1] == "YES"
    assert columnas["rounding_step_snapshot"][1] == "YES"
    assert columnas["cost_snapshot"][0] == "jsonb"

    restricciones = set(
        (
            await db_session.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid = "
                    "'prototype_quotations'::regclass"
                )
            )
        ).scalars()
    )
    # La convencion de nombres del proyecto antepone `ck_<tabla>_`, asi que se
    # busca por sufijo en vez de fijar el nombre completo a mano.
    for esperada in (
        "pq_status_allowed",
        "pq_payment_status_allowed",
        "pq_quantity_positive",
        "pq_confirmed_has_code",
    ):
        assert any(nombre.endswith(esperada) for nombre in restricciones), (
            esperada,
            sorted(restricciones),
        )
    assert any("code" in nombre and nombre.startswith("uq_") for nombre in restricciones)


@pytest.mark.asyncio
async def test_las_tarifas_de_prototipo_nacen_en_cero(db_session: AsyncSession) -> None:
    """Los numeros del Excel son EJEMPLOS; sembrarlos seria fijar precios.

    Se lee el valor por defecto de la columna, que es el que recibe una casa
    que todavia no ha configurado nada.
    """
    defectos = dict(
        (
            await db_session.execute(
                text(
                    "SELECT column_name, column_default FROM information_schema.columns "
                    "WHERE table_name = 'commercial_settings' "
                    "AND column_name LIKE 'prototype_%'"
                )
            )
        ).all()
    )
    assert len(defectos) == 5, defectos
    for nombre, defecto in defectos.items():
        assert defecto is not None and defecto.startswith("0"), (nombre, defecto)


@pytest.mark.asyncio
async def test_el_talonario_cpr_existe_y_es_suyo(db_session: AsyncSession) -> None:
    """Contador propio: agotar cotizaciones de producto no mueve el de muestras."""
    fila = (
        await db_session.execute(
            text(
                "SELECT prefix, padding, reset_policy FROM document_sequences "
                "WHERE sequence_type = 'PROTOTYPE_QUOTE'"
            )
        )
    ).one_or_none()
    assert fila is not None, "falta el talonario CPR"
    assert fila[0] == "CPR"


@pytest.mark.asyncio
async def test_la_muestra_gano_el_vinculo_sin_perder_el_viejo(
    db_session: AsyncSession,
) -> None:
    """OLD_PROTOTYPE_QUOTATION_LINK_COMPATIBLE: los dos conviven."""
    columnas = set(
        (
            await db_session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'prototypes' "
                    "AND column_name IN ('quotation_id', 'prototype_quotation_id')"
                )
            )
        ).scalars()
    )
    assert columnas == {"quotation_id", "prototype_quotation_id"}


# ---------------------------------------------------------------------------
# El caso del Excel, extremo a extremo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_caso_de_referencia_da_450_81_531_y_6_dias_por_la_api(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_REFERENCE por el servicio completo, no solo por el motor puro."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_referencia")
    respuesta = await api.post(
        f"{COTIZADOR}/preview", json=_payload(caso), headers=head(admin_csrf)
    )
    assert respuesta.status_code == 200, respuesta.text
    costeo = respuesta.json()["costing"]

    assert Decimal(costeo["design_cost"]) == Decimal("240.00")
    assert Decimal(costeo["artist_cost"]) == Decimal("200.00")
    assert Decimal(costeo["materials_cost"]) == Decimal("10.00")
    assert Decimal(costeo["base_cost"]) == Decimal("450.00")
    assert Decimal(costeo["commercial_net_total"]) == Decimal("450.00")
    assert Decimal(costeo["commercial_tax_total"]) == Decimal("81.00")
    assert Decimal(costeo["commercial_gross_total"]) == Decimal("531.00")
    assert Decimal(costeo["estimated_days"]) == Decimal(6)
    # PROTOTYPE_QUOTATION_FIRING_COST / _FIRING_DAYS: ni siquiera hay campo.
    assert "firing_cost" not in costeo
    assert "firing_days" not in costeo


@pytest.mark.asyncio
async def test_la_previsualizacion_no_deja_rastro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Mirar un precio no puede gastar un correlativo ni dejar borradores."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_prev")
    antes = await db_session.scalar(select(func.count()).select_from(PrototypeQuotation))

    for _ in range(3):
        respuesta = await api.post(
            f"{COTIZADOR}/preview", json=_payload(caso), headers=head(admin_csrf)
        )
        assert respuesta.status_code == 200, respuesta.text

    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(PrototypeQuotation)) == antes


# ---------------------------------------------------------------------------
# D2 — el borrador sigue a la casa; la emitida, no
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_borrador_sin_tarifa_pactada_sigue_a_la_configuracion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NULL_OVERRIDE_FOLLOWS_LIVE_CONFIG_IN_DRAFT.

    Copiar el valor por defecto dentro del borrador lo habria dejado anclado a
    la tarifa del dia que se creo, sin que nadie lo notara.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_vivo")
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    identificador = creada.json()["id"]
    assert Decimal(creada.json()["costing"]["design_cost"]) == Decimal("240.00")

    await _ajustes(db_session, prototype_design_rate=Decimal(120))

    relectura = await api.get(f"{COTIZADOR}/{identificador}", headers=head(admin_csrf))
    assert relectura.status_code == 200, relectura.text
    # 3 dias x 120 = 360, no los 240 de antes.
    assert Decimal(relectura.json()["costing"]["design_cost"]) == Decimal("360.00")


@pytest.mark.asyncio
async def test_una_tarifa_pactada_gana_a_la_de_la_casa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    caso = await _caso_referencia(api, admin_csrf, db_session, "_pactada")
    payload = _payload(caso) | {"design_rate_override": "90"}
    creada = await api.post(COTIZADOR, json=payload, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    assert Decimal(creada.json()["costing"]["design_cost"]) == Decimal("270.00")

    await _ajustes(db_session, prototype_design_rate=Decimal(500))
    relectura = await api.get(f"{COTIZADOR}/{creada.json()['id']}", headers=head(admin_csrf))
    # Lo pactado no se mueve porque la casa suba su tarifa.
    assert Decimal(relectura.json()["costing"]["design_cost"]) == Decimal("270.00")


@pytest.mark.asyncio
async def test_la_unidad_la_manda_el_catalogo_y_no_el_navegador(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """FRONT_PROTOTYPE_UOM_AUTHORITY: 0.

    El esquema ni siquiera acepta una unidad de entrada. Si la aceptara, se
    podrian cotizar kilos de algo que se lleva en gramos.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_uom")
    payload = _payload(caso)
    payload["materials"][0]["uom_code"] = "g"

    respuesta = await api.post(COTIZADOR, json=payload, headers=head(admin_csrf))
    assert respuesta.status_code == 422, respuesta.text


# ---------------------------------------------------------------------------
# D1 — el documento emitido no cambia
# ---------------------------------------------------------------------------
async def _confirmada(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, sufijo: str
) -> dict[str, Any]:
    caso = await _caso_referencia(api, csrf, db_session, sufijo)
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(csrf))
    assert creada.status_code == 201, creada.text
    confirmada = await api.post(f"{COTIZADOR}/{creada.json()['id']}/confirm", headers=head(csrf))
    assert confirmada.status_code == 200, confirmada.text
    return dict(confirmada.json())


@pytest.mark.asyncio
async def test_al_emitir_se_congela_todo_lo_que_hizo_falta_para_el_numero(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    documento = await _confirmada(api, admin_csrf, db_session, "_freeze")
    assert documento["status"] == "CONFIRMED"
    assert documento["code"] is not None and documento["code"].startswith("CPR-")

    db_session.expire_all()
    fila = await db_session.get(PrototypeQuotation, documento["id"])
    assert fila is not None
    assert fila.cost_snapshot is not None
    congelado = fila.cost_snapshot["effective"]
    # Se comparan como Decimal, no como cadena: lo guardado conserva la escala
    # de la columna —«80.000000000000»— y afirmar el texto exacto ataria la
    # prueba a la precision de PostgreSQL en vez de al importe.
    assert Decimal(congelado["design_rate"]) == Decimal(80)
    assert Decimal(congelado["rounding_step"]) == Decimal("0.50")
    assert fila.rounding_source_snapshot == "COMMERCIAL_SETTINGS"
    assert fila.commercial_gross_total == Decimal("531.00")

    # El costo del material queda escrito EN SU LINEA.
    lineas = list(
        (
            await db_session.execute(
                select(PrototypeQuotationMaterial).where(
                    PrototypeQuotationMaterial.prototype_quotation_id == fila.id
                )
            )
        ).scalars()
    )
    assert lineas and all(linea.unit_cost_snapshot is not None for linea in lineas)


@pytest.mark.asyncio
async def test_un_borrador_no_tiene_todavia_el_costo_del_material_congelado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    caso = await _caso_referencia(api, admin_csrf, db_session, "_nofreeze")
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    db_session.expire_all()
    lineas = list(
        (
            await db_session.execute(
                select(PrototypeQuotationMaterial).where(
                    PrototypeQuotationMaterial.prototype_quotation_id == creada.json()["id"]
                )
            )
        ).scalars()
    )
    assert lineas and all(linea.unit_cost_snapshot is None for linea in lineas)


@pytest.mark.asyncio
async def test_lo_emitido_no_cambia_aunque_cambie_el_mundo(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """SNAPSHOT_IMMUTABILITY.

    Se mueve TODO lo que interviene en el precio y el documento sigue diciendo
    lo mismo. Un papel firmado que cambia solo no es un papel firmado.
    """
    documento = await _confirmada(api, admin_csrf, db_session, "_inmut")
    antes = documento["costing"]

    await _ajustes(
        db_session,
        prototype_design_rate=Decimal(999),
        prototype_artist_rate=Decimal(999),
        prototype_mold_maker_price=Decimal(999),
        prototype_fixed_cost=Decimal(999),
        tax_percent=Decimal(30),
        rounding_step=Decimal("1.00"),
    )

    relectura = await api.get(f"{COTIZADOR}/{documento['id']}", headers=head(admin_csrf))
    assert relectura.status_code == 200, relectura.text
    despues = relectura.json()["costing"]

    for campo in (
        "design_cost",
        "artist_cost",
        "materials_cost",
        "base_cost",
        "commercial_net_total",
        "commercial_tax_total",
        "commercial_gross_total",
        "estimated_days",
        "rounding_step",
        "tax_percent",
    ):
        assert Decimal(str(despues[campo])) == Decimal(str(antes[campo])), campo


@pytest.mark.asyncio
async def test_una_emitida_ya_no_se_edita(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    documento = await _confirmada(api, admin_csrf, db_session, "_noedit")
    respuesta = await api.put(
        f"{COTIZADOR}/{documento['id']}",
        json={"description": "Otra cosa", "quantity": 5},
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 409, respuesta.text


# ---------------------------------------------------------------------------
# D3 — el talonario aguanta veinte a la vez
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_veinte_emisiones_simultaneas_dan_veinte_codigos_distintos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CPR_CONCURRENCY_20.

    Se prueba el mecanismo real: veinte confirmaciones a la vez contra el
    mismo talonario. Serializarlas desde la prueba no demostraria nada.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_conc")
    borradores = []
    for _ in range(20):
        creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
        assert creada.status_code == 201, creada.text
        borradores.append(creada.json()["id"])

    respuestas = await asyncio.gather(
        *(
            api.post(f"{COTIZADOR}/{identificador}/confirm", headers=head(admin_csrf))
            for identificador in borradores
        )
    )
    assert all(r.status_code == 200 for r in respuestas), [
        r.status_code for r in respuestas if r.status_code != 200
    ]

    codigos = [r.json()["code"] for r in respuestas]
    assert len(codigos) == 20
    assert len(set(codigos)) == 20, sorted(codigos)
    assert all(codigo.startswith("CPR-") for codigo in codigos)
    print("CPR emitidos:", sorted(codigos))


# ---------------------------------------------------------------------------
# D4 — cobrar habilita, arrancar consume
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cobrar_no_mueve_inventario_y_deja_una_sola_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_PAYMENT_STOCK_DELTA: 0, y creacion idempotente."""
    documento = await _confirmada(api, admin_csrf, db_session, "_pago")
    db_session.expire_all()
    movimientos_antes = await db_session.scalar(select(func.count()).select_from(StockMovement))

    primera = await cobrar(api, admin_csrf, documento["id"])
    assert primera.status_code == 200, primera.text
    assert primera.json()["payment_status"] == "PAID"
    assert primera.json()["prototype_id"] is not None

    segunda = await cobrar(api, admin_csrf, documento["id"])
    assert segunda.status_code == 200, segunda.text
    assert segunda.json()["prototype_id"] == primera.json()["prototype_id"]

    db_session.expire_all()
    assert (
        await db_session.scalar(select(func.count()).select_from(StockMovement))
        == movimientos_antes
    )
    muestras = await db_session.scalar(
        select(func.count())
        .select_from(Prototype)
        .where(Prototype.prototype_quotation_id == documento["id"])
    )
    assert muestras == 1


@pytest.mark.asyncio
async def test_la_muestra_hereda_lo_tecnico_y_nada_del_dinero(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    documento = await _confirmada(api, admin_csrf, db_session, "_tecnico")
    pagada = await cobrar(api, admin_csrf, documento["id"])
    db_session.expire_all()
    muestra = await db_session.get(Prototype, pagada.json()["prototype_id"])
    assert muestra is not None
    assert muestra.name == "Taza personalizada"
    assert muestra.quantity == 1
    assert muestra.prototype_quotation_id == documento["id"]
    assert muestra.lines and muestra.lines[0].quantity_planned == Decimal("1.25")
    # Y ni un campo comercial: el precio es asunto del documento.
    assert not hasattr(muestra, "commercial_gross_total")


@pytest.mark.asyncio
async def test_una_pagada_no_se_anula(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Deshacer un cobro exige devolucion o nota de credito, y no existen."""
    documento = await _confirmada(api, admin_csrf, db_session, "_anula")
    await cobrar(api, admin_csrf, documento["id"])
    respuesta = await api.post(f"{COTIZADOR}/{documento['id']}/cancel", headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_borrador_no_se_descarga(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    caso = await _caso_referencia(api, admin_csrf, db_session, "_pdfdraft")
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    respuesta = await api.get(f"{COTIZADOR}/{creada.json()['id']}/pdf", headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text


@pytest.mark.asyncio
async def test_una_cotizacion_que_no_existe_da_404(api: httpx.AsyncClient, admin_csrf: str) -> None:
    respuesta = await api.get(f"{COTIZADOR}/99999999/pdf", headers=head(admin_csrf))
    assert respuesta.status_code == 404, respuesta.text


@pytest.mark.asyncio
async def test_el_pdf_ensena_el_precio_y_no_como_se_compone(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_INTERNAL_COST_LEAK: 0.

    Se lee el PDF de verdad, no el ViewModel: quien decide lo que acaba impreso
    es la plantilla, y puede recibir el dato correcto y pintarlo igual.
    """
    documento = await _confirmada(api, admin_csrf, db_session, "_pdfok")
    respuesta = await api.get(f"{COTIZADOR}/{documento['id']}/pdf", headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.headers["content-type"].startswith("application/pdf")
    assert "filename=" in respuesta.headers.get("content-disposition", "")
    assert respuesta.content[:4] == b"%PDF"

    texto = "\n".join(
        pagina.extract_text() or "" for pagina in PdfReader(io.BytesIO(respuesta.content)).pages
    )
    assert documento["code"] in texto
    assert "Desarrollo de prototipo" in texto
    # Los totales del papel son los del documento.
    assert "531.00" in texto
    assert "450.00" in texto

    # Y nada de la cocina interna.
    for interno in ("80.00", "Tarifa", "cost_snapshot", "design_rate", "240.00", "200.00"):
        assert interno not in texto, interno

    # Ni la quema, que ya no participa en este documento.
    for quema in ("Horno", "Quema", "Hornada", "hornada"):
        assert quema not in texto, quema


@pytest.mark.asyncio
async def test_el_pdf_de_una_emitida_no_cambia_aunque_cambie_la_configuracion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    documento = await _confirmada(api, admin_csrf, db_session, "_pdfinm")
    primero = await api.get(f"{COTIZADOR}/{documento['id']}/pdf", headers=head(admin_csrf))
    texto_antes = "\n".join(
        p.extract_text() or "" for p in PdfReader(io.BytesIO(primero.content)).pages
    )

    await _ajustes(
        db_session,
        prototype_design_rate=Decimal(999),
        tax_percent=Decimal(30),
        rounding_step=Decimal("1.00"),
    )

    segundo = await api.get(f"{COTIZADOR}/{documento['id']}/pdf", headers=head(admin_csrf))
    texto_despues = "\n".join(
        p.extract_text() or "" for p in PdfReader(io.BytesIO(segundo.content)).pages
    )
    assert "531.00" in texto_despues
    assert texto_antes == texto_despues


@pytest.mark.asyncio
async def test_generar_el_pdf_no_toca_la_cotizacion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    documento = await _confirmada(api, admin_csrf, db_session, "_pdfpuro")
    db_session.expire_all()
    fila = await db_session.get(PrototypeQuotation, documento["id"])
    assert fila is not None
    antes = (fila.status, fila.code, fila.commercial_gross_total, fila.updated_at)

    await api.get(f"{COTIZADOR}/{documento['id']}/pdf", headers=head(admin_csrf))

    db_session.expire_all()
    fila = await db_session.get(PrototypeQuotation, documento["id"])
    assert fila is not None
    assert (fila.status, fila.code, fila.commercial_gross_total, fila.updated_at) == antes


@pytest.mark.asyncio
async def test_el_taller_no_cotiza_prototipos(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Poner un precio es administracion, igual que en el Cotizador de producto."""
    from tests.db.test_prototypes import _como_operario

    caso = await _caso_referencia(api, admin_csrf, db_session, "_rbac")
    operario = await _como_operario(api)
    respuesta = await api.post(COTIZADOR, json=_payload(caso), headers=head(operario))
    assert respuesta.status_code == 403, respuesta.text


# ---------------------------------------------------------------------------
# D5, D6, D7 — las tres vias de readiness
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sin_cobrar_la_muestra_no_arranca_y_al_cobrar_si(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NEW_PROTOTYPE_QUOTATION_PAYMENT_GATE.

    La muestra nace al cobrar, asi que la via nueva se comprueba mirando que
    la readiness deja de quejarse por el pago una vez cobrada.
    """
    documento = await _confirmada(api, admin_csrf, db_session, "_ready")
    pagada = await cobrar(api, admin_csrf, documento["id"])
    muestra_id = pagada.json()["prototype_id"]

    detalle = await api.get(f"/api/v1/prototypes/{muestra_id}", headers=head(admin_csrf))
    assert detalle.status_code == 200, detalle.text
    codigos = {issue["code"] for issue in detalle.json()["readiness"]["issues"]}
    # Ya no falta pagar. Puede faltar almacen o existencia: eso es otra cosa.
    assert "QUOTATION_UNPAID" not in codigos
    assert "NO_QUOTATION" not in codigos


@pytest.mark.asyncio
async def test_una_muestra_vinculada_a_una_cpr_impagada_no_arranca(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La via CPR manda: sin cobrar, bloquea."""
    documento = await _confirmada(api, admin_csrf, db_session, "_impaga")
    pagada = await cobrar(api, admin_csrf, documento["id"])
    muestra_id = pagada.json()["prototype_id"]

    # Se deshace el cobro por la base para poder observar la via bloqueada:
    # el servicio no lo permite, y con razon.
    db_session.expire_all()
    fila = await db_session.get(PrototypeQuotation, documento["id"])
    assert fila is not None
    fila.payment_status = fila.payment_status.__class__.UNPAID
    await db_session.commit()

    detalle = await api.get(f"/api/v1/prototypes/{muestra_id}", headers=head(admin_csrf))
    codigos = {issue["code"] for issue in detalle.json()["readiness"]["issues"]}
    assert "QUOTATION_UNPAID" in codigos


@pytest.mark.asyncio
async def test_la_via_legacy_sigue_funcionando_igual(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """LEGACY_READINESS: las muestras de 009K no se endurecen."""
    from tests.db.test_prototypes import _muestra_lista

    datos = await _muestra_lista(api, admin_csrf, db_session, suffix="_legacy0231")
    detalle = await api.get(
        f"/api/v1/prototypes/{datos['prototipo']['id']}", headers=head(admin_csrf)
    )
    assert detalle.status_code == 200, detalle.text
    cuerpo = detalle.json()
    assert cuerpo["prototype_quotation_id"] is None
    codigos = {issue["code"] for issue in cuerpo["readiness"]["issues"]}
    # Su CTZ esta pagada, asi que por esa via no hay queja de pago.
    assert "QUOTATION_UNPAID" not in codigos


@pytest.mark.asyncio
async def test_una_muestra_sin_ningun_vinculo_dice_que_falta_pedido(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """HISTORICAL_READINESS: el comportamiento de siempre, sin endurecer."""
    from tests.db.test_prototypes import crear_prototipo

    creado = await crear_prototipo(api, admin_csrf, name="Suelta 009K11", quantity=1)
    assert creado.status_code == 201, creado.text
    detalle = await api.get(f"/api/v1/prototypes/{creado.json()['id']}", headers=head(admin_csrf))
    codigos = {issue["code"] for issue in detalle.json()["readiness"]["issues"]}
    assert "NO_QUOTATION" in codigos


# ---------------------------------------------------------------------------
# PARIDAD MONEDA / TIPO DE CAMBIO
#
# El Cotizador principal emite en soles o en dolares desde 009F. Un prototipo
# se le vende al mismo cliente y lo firma la misma casa: si uno puede y el otro
# no, la limitacion no es del negocio sino del software.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_por_omision_la_cotizacion_de_prototipo_sale_en_la_moneda_de_la_casa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CURRENCY_DEFAULT: no mandar moneda sigue tomando la de Configuracion."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_moneda_defecto")

    vista = await api.post(f"{COTIZADOR}/preview", json=_payload(caso), headers=head(admin_csrf))
    assert vista.status_code == 200, vista.text
    cuerpo = vista.json()

    assert cuerpo["currency_code"] == "PEN"
    assert cuerpo["exchange_rate"] is None
    assert Decimal(cuerpo["costing"]["raw_net_total"]) == Decimal(450)
    assert Decimal(cuerpo["costing"]["commercial_gross_total"]) == Decimal(531)


@pytest.mark.asyncio
async def test_se_puede_cotizar_un_prototipo_en_dolares(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CURRENCY_PARITY: la misma capacidad comercial que el Cotizador principal.

    450 soles a 4.50 son 100 dolares; IGV 18; total 118. El costo sigue en
    soles porque en soles se paga al artista.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_moneda_usd")
    datos = _payload(caso) | {"currency_code": "USD", "exchange_rate": "4.5"}

    vista = await api.post(f"{COTIZADOR}/preview", json=datos, headers=head(admin_csrf))
    assert vista.status_code == 200, vista.text
    cuerpo = vista.json()

    assert cuerpo["currency_code"] == "USD"
    assert cuerpo["currency_symbol"] == "US$"
    assert Decimal(cuerpo["exchange_rate"]) == Decimal("4.5")
    assert Decimal(cuerpo["costing"]["base_cost"]) == Decimal(450)
    assert Decimal(cuerpo["costing"]["raw_net_total"]) == Decimal(100)
    assert Decimal(cuerpo["costing"]["commercial_gross_total"]) == Decimal(118)


@pytest.mark.asyncio
async def test_en_dolares_sin_tipo_de_cambio_no_se_cotiza(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CURRENCY_GUARD: el mismo 422 y el mismo motivo que el Cotizador principal."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_moneda_sin_tasa")
    datos = _payload(caso) | {"currency_code": "USD"}

    vista = await api.post(f"{COTIZADOR}/preview", json=datos, headers=head(admin_csrf))
    assert vista.status_code == 422, vista.text
    assert "EXCHANGE_RATE_REQUIRED" in vista.text


@pytest.mark.asyncio
async def test_en_soles_con_tipo_de_cambio_no_se_cotiza(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Una tasa guardada en un documento en soles describiria una conversion
    que nunca ocurrio."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_moneda_tasa_sobrante")
    datos = _payload(caso) | {"currency_code": "PEN", "exchange_rate": "4"}

    vista = await api.post(f"{COTIZADOR}/preview", json=datos, headers=head(admin_csrf))
    assert vista.status_code == 422, vista.text


@pytest.mark.asyncio
async def test_la_moneda_del_borrador_se_guarda_y_se_relee(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La eleccion sobrevive al viaje a la base, no solo a la respuesta."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_moneda_persiste")
    datos = _payload(caso) | {"currency_code": "USD", "exchange_rate": "3.75"}

    creada = await api.post(COTIZADOR, json=datos, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    identificador = creada.json()["id"]

    fila = await db_session.get(PrototypeQuotation, identificador)
    assert fila is not None
    await db_session.refresh(fila)
    assert fila.currency_code_snapshot == "USD"
    assert fila.currency_symbol_snapshot == "US$"
    assert fila.exchange_rate_snapshot == Decimal("3.75")

    leida = await api.get(f"{COTIZADOR}/{identificador}", headers=head(admin_csrf))
    assert leida.json()["currency_code"] == "USD"
    assert Decimal(leida.json()["exchange_rate"]) == Decimal("3.75")


@pytest.mark.asyncio
async def test_volver_a_soles_borra_la_tasa_del_borrador(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Dejarla puesta describiria una conversion que ya no ocurre."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_moneda_vuelta")
    datos = _payload(caso) | {"currency_code": "USD", "exchange_rate": "4.5"}
    creada = await api.post(COTIZADOR, json=datos, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    identificador = creada.json()["id"]

    editada = await api.put(
        f"{COTIZADOR}/{identificador}",
        json=_payload(caso) | {"currency_code": "PEN"},
        headers=head(admin_csrf),
    )
    assert editada.status_code == 200, editada.text

    fila = await db_session.get(PrototypeQuotation, identificador)
    assert fila is not None
    await db_session.refresh(fila)
    assert fila.currency_code_snapshot == "PEN"
    assert fila.exchange_rate_snapshot is None


@pytest.mark.asyncio
async def test_al_emitir_se_congela_la_moneda_del_borrador_y_no_la_de_configuracion(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CURRENCY_FREEZE.

    Sobrescribir aqui con la moneda de la casa emitiria en soles un documento
    que se pacto en dolares: el numero no cambiaria y la etiqueta mentiria.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_moneda_congela")
    datos = _payload(caso) | {"currency_code": "USD", "exchange_rate": "4.5"}
    creada = await api.post(COTIZADOR, json=datos, headers=head(admin_csrf))
    identificador = creada.json()["id"]

    emitida = await api.post(
        f"{COTIZADOR}/{identificador}/confirm", json={}, headers=head(admin_csrf)
    )
    assert emitida.status_code == 200, emitida.text

    fila = await db_session.get(PrototypeQuotation, identificador)
    assert fila is not None
    await db_session.refresh(fila)
    assert fila.currency_code_snapshot == "USD"
    assert fila.exchange_rate_snapshot == Decimal("4.5")
    assert fila.commercial_gross_total == Decimal(118)
    assert fila.cost_snapshot["effective"]["currency"] == "USD"
    assert Decimal(fila.cost_snapshot["effective"]["exchange_rate"]) == Decimal("4.5")


@pytest.mark.asyncio
async def test_una_emitida_en_dolares_no_se_revalora_si_cambia_la_tasa_de_la_casa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """El documento firmado no cambia de precio porque cambie el dolar."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_moneda_inmutable")
    datos = _payload(caso) | {"currency_code": "USD", "exchange_rate": "4.5"}
    creada = await api.post(COTIZADOR, json=datos, headers=head(admin_csrf))
    identificador = creada.json()["id"]
    await api.post(f"{COTIZADOR}/{identificador}/confirm", json={}, headers=head(admin_csrf))

    await _ajustes(db_session, currency_code="USD", currency_symbol="US$")

    leida = await api.get(f"{COTIZADOR}/{identificador}", headers=head(admin_csrf))
    cuerpo = leida.json()
    assert cuerpo["currency_code"] == "USD"
    assert Decimal(cuerpo["exchange_rate"]) == Decimal("4.5")
    assert Decimal(cuerpo["costing"]["commercial_gross_total"]) == Decimal(118)


@pytest.mark.asyncio
async def test_el_listado_dice_en_que_moneda_esta_cada_total(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Sin esto el listado pondria `S/` delante de un importe en dolares."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_moneda_listado")
    datos = _payload(caso) | {"currency_code": "USD", "exchange_rate": "4.5"}
    creada = await api.post(COTIZADOR, json=datos, headers=head(admin_csrf))
    identificador = creada.json()["id"]
    await api.post(f"{COTIZADOR}/{identificador}/confirm", json={}, headers=head(admin_csrf))

    listado = await api.get(COTIZADOR, headers=head(admin_csrf))
    assert listado.status_code == 200, listado.text
    fila = next(item for item in listado.json()["items"] if item["id"] == identificador)
    assert fila["currency_code"] == "USD"
    assert fila["currency_symbol"] == "US$"
    assert Decimal(fila["commercial_gross_total"]) == Decimal(118)


@pytest.mark.asyncio
async def test_el_pdf_en_dolares_lleva_el_tipo_de_cambio_y_el_simbolo_correcto(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CURRENCY_PDF.

    Un documento en dolares sin la tasa deja al cliente sin saber con que
    numero se convirtio lo que esta firmando; y un `S/` delante de dolares es
    exactamente el error que nadie detecta hasta que ya esta firmado.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_moneda_pdf")
    datos = _payload(caso) | {"currency_code": "USD", "exchange_rate": "3.75"}
    creada = await api.post(COTIZADOR, json=datos, headers=head(admin_csrf))
    identificador = creada.json()["id"]
    await api.post(f"{COTIZADOR}/{identificador}/confirm", json={}, headers=head(admin_csrf))

    pdf = await api.get(f"{COTIZADOR}/{identificador}/pdf", headers=head(admin_csrf))
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"] == "application/pdf"
    paginas = PdfReader(io.BytesIO(pdf.content)).pages
    texto = "\n".join(page.extract_text() or "" for page in paginas)

    assert "1 USD = S/ 3.75" in texto
    assert "US$" in texto
    # 450 / 3.75 = 120 netos; con IGV 141.60, que sube al escalon 142.00.
    assert "142.00" in texto
    assert "S/ 142" not in texto


# ---------------------------------------------------------------------------
# El producto interno nace al COBRAR, no antes
#
# Una cotizacion que nadie acepto ni pago no puede ensuciar el maestro con
# piezas que quiza no se fabriquen nunca. Y cuando por fin se cobra, el codigo
# no se inventa aqui: sale de la misma puerta por la que entra un alta manual.
# ---------------------------------------------------------------------------
async def _contar_productos(db_session: AsyncSession) -> int:
    return int(await db_session.scalar(select(func.count()).select_from(Product)) or 0)


@pytest.mark.asyncio
async def test_un_borrador_de_concepto_nuevo_no_crea_producto(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NEW_CONCEPT_PRODUCT_CREATED_AT_DRAFT: NO."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_prod_draft")
    antes = await _contar_productos(db_session)

    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    assert creada.json()["product_id"] is None
    assert creada.json()["product_code"] is None

    db_session.expire_all()
    assert await _contar_productos(db_session) == antes


@pytest.mark.asyncio
async def test_emitir_un_concepto_nuevo_tampoco_crea_producto(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NEW_CONCEPT_PRODUCT_CREATED_AT_CONFIRM: NO.

    Emitir congela el precio. Que el cliente reciba el papel no significa que
    lo vaya a aceptar.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_prod_confirm")
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    antes = await _contar_productos(db_session)

    emitida = await api.post(
        f"{COTIZADOR}/{creada.json()['id']}/confirm", json={}, headers=head(admin_csrf)
    )
    assert emitida.status_code == 200, emitida.text
    assert emitida.json()["product_id"] is None

    db_session.expire_all()
    assert await _contar_productos(db_session) == antes


@pytest.mark.asyncio
async def test_un_concepto_nuevo_sin_familia_no_se_puede_emitir(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """La categoria es lo unico que el maestro pide y la cotizacion no deduce.

    Se exige al emitir y no al cobrar: descubrirlo con el dinero ya cobrado
    dejaria un documento firmado que no puede entrar a produccion.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_prod_sinfam")
    datos = _payload(caso)
    datos.pop("product_category_id")
    creada = await api.post(COTIZADOR, json=datos, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text

    emitida = await api.post(
        f"{COTIZADOR}/{creada.json()['id']}/confirm", json={}, headers=head(admin_csrf)
    )
    assert emitida.status_code == 422, emitida.text
    assert "PRODUCT_CATEGORY_REQUIRED" in emitida.text


@pytest.mark.asyncio
async def test_cobrar_un_concepto_nuevo_crea_el_producto_con_el_codigo_de_la_casa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NEW_CONCEPT_PRODUCT_MATERIALIZED_FOR_PRODUCTION: PASS.

    Y el codigo lo emite la MISMA autoridad que un alta manual: la secuencia
    PRODUCT_50 con prefijo LAB50. Un segundo generador daria dos series que
    algun dia se cruzan.
    """
    documento = await _confirmada(api, admin_csrf, db_session, "_prod_paid")
    antes = await _contar_productos(db_session)

    cobrada = await cobrar(api, admin_csrf, documento["id"])
    assert cobrada.status_code == 200, cobrada.text
    cuerpo = cobrada.json()

    db_session.expire_all()
    assert await _contar_productos(db_session) == antes + 1

    assert cuerpo["product_id"] is not None
    assert cuerpo["product_code"] is not None
    assert cuerpo["product_code"].startswith("LAB50")
    # El nombre comercial del documento es el nombre inicial del producto.
    assert cuerpo["product_name"] == "Taza personalizada"

    producto = await db_session.get(Product, cuerpo["product_id"])
    assert producto is not None
    assert producto.product_type.value == "FINISHED_PRODUCT"
    # Las medidas acordadas sirven de punto de partida del maestro...
    assert producto.width == Decimal("15.000000")
    assert producto.height == Decimal("20.000000")
    # ...pero el dinero del prototipo no: incluye dias de diseno que no se
    # repiten en la segunda pieza.
    assert producto.cost is None
    assert producto.sale_price is None

    # PROTOTYPE_QUOTATION_PRODUCT_LINK / PHYSICAL_PROTOTYPE_PRODUCT_LINK:
    # una sola identidad de producto.
    muestra = await db_session.scalar(
        select(Prototype).where(Prototype.prototype_quotation_id == documento["id"]).limit(1)
    )
    assert muestra is not None
    assert muestra.product_id == cuerpo["product_id"]


@pytest.mark.asyncio
async def test_cobrar_dos_veces_no_crea_un_segundo_producto(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCT_CREATION_ON_PAYMENT_IDEMPOTENT: PASS."""
    documento = await _confirmada(api, admin_csrf, db_session, "_prod_retry")
    primera = await cobrar(api, admin_csrf, documento["id"])
    assert primera.status_code == 200, primera.text
    db_session.expire_all()
    antes = await _contar_productos(db_session)

    segunda = await cobrar(api, admin_csrf, documento["id"])
    assert segunda.status_code == 200, segunda.text

    db_session.expire_all()
    assert await _contar_productos(db_session) == antes
    assert segunda.json()["product_id"] == primera.json()["product_id"]
    assert segunda.json()["product_code"] == primera.json()["product_code"]
    assert segunda.json()["prototype_id"] == primera.json()["prototype_id"]

    muestras = await db_session.scalar(
        select(func.count())
        .select_from(Prototype)
        .where(Prototype.prototype_quotation_id == documento["id"])
    )
    assert muestras == 1


@pytest.mark.asyncio
async def test_cobrar_un_prototipo_de_un_producto_existente_no_duplica_el_maestro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """EXISTING_PRODUCT_DUPLICATED_ON_PAYMENT: NO.

    Si la muestra reproduce una pieza que ya esta en el catalogo, pagar el
    prototipo no puede darle un segundo codigo ni tocar el que tiene.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_prod_exist")
    pieza = await create_product(
        api,
        admin_csrf,
        product_category_id=caso["_familia"]["id"],
        product_type="FINISHED_PRODUCT",
        name="Taza de catalogo",
        base_uom_code="unit",
    )
    assert pieza.status_code == 201, pieza.text
    maestro = pieza.json()

    creada = await api.post(
        COTIZADOR,
        json=_payload(caso) | {"product_id": maestro["id"]},
        headers=head(admin_csrf),
    )
    assert creada.status_code == 201, creada.text
    await api.post(f"{COTIZADOR}/{creada.json()['id']}/confirm", json={}, headers=head(admin_csrf))

    db_session.expire_all()
    antes = await _contar_productos(db_session)
    cobrada = await cobrar(api, admin_csrf, creada.json()["id"])
    assert cobrada.status_code == 200, cobrada.text

    db_session.expire_all()
    assert await _contar_productos(db_session) == antes
    assert cobrada.json()["product_id"] == maestro["id"]
    assert cobrada.json()["product_code"] == maestro["internal_reference"]

    # Y el maestro no cambia de nombre por haberse prototipado.
    producto = await db_session.get(Product, maestro["id"])
    assert producto is not None
    assert producto.name == "Taza de catalogo"


@pytest.mark.asyncio
async def test_veinte_cobros_simultaneos_dan_un_producto_y_una_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PAYMENT_CONCURRENCY_DUPLICATE_PRODUCT: 0.

    El `SELECT ... FOR UPDATE` de `get` serializa los cobros sobre la misma
    fila. Sin el, veinte reintentos darian veinte codigos internos.
    """
    documento = await _confirmada(api, admin_csrf, db_session, "_prod_conc")
    db_session.expire_all()
    antes = await _contar_productos(db_session)
    # El MISMO almacen en los veinte: la concurrencia que se prueba es la del
    # cobro, no la de dos personas eligiendo sitios distintos.
    almacen = await api.post(
        "/api/v1/inventory/locations",
        json={"name": "Almacen cobro concurrente"},
        headers=head(admin_csrf),
    )
    assert almacen.status_code == 201, almacen.text
    location_id = int(almacen.json()["id"])

    respuestas = await asyncio.gather(
        *(
            api.post(
                f"{COTIZADOR}/{documento['id']}/mark-paid",
                json={"stock_location_id": location_id},
                headers=head(admin_csrf),
            )
            for _ in range(20)
        ),
        return_exceptions=True,
    )
    correctas = [r for r in respuestas if isinstance(r, httpx.Response) and r.status_code == 200]
    assert correctas, [str(r) for r in respuestas][:3]
    # Ningun 500: un choque de concurrencia no puede salir como error interno.
    assert not [r for r in respuestas if isinstance(r, httpx.Response) and r.status_code >= 500]

    db_session.expire_all()
    assert await _contar_productos(db_session) == antes + 1

    identificadores = {r.json()["product_id"] for r in correctas}
    assert len(identificadores) == 1

    muestras = await db_session.scalar(
        select(func.count())
        .select_from(Prototype)
        .where(Prototype.prototype_quotation_id == documento["id"])
    )
    assert muestras == 1


@pytest.mark.asyncio
async def test_materializar_el_producto_no_mueve_inventario(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PRODUCT_CREATION_STOCK_DELTA: 0.

    Generar el producto, cobrar y crear la muestra no sacan un gramo del
    almacen. El consumo es al ARRANCAR, y eso no ha pasado todavia.
    """
    documento = await _confirmada(api, admin_csrf, db_session, "_prod_stock")
    antes = await db_session.scalar(select(func.count()).select_from(StockMovement))

    cobrada = await cobrar(api, admin_csrf, documento["id"])
    assert cobrada.status_code == 200, cobrada.text
    assert cobrada.json()["product_id"] is not None

    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(StockMovement)) == antes


# ---------------------------------------------------------------------------
# El documento es el MISMO que el de una cotizacion de producto
#
# Hubo una plantilla propia para el CPR. Extendia `base_document.html`, si, pero
# redeclaraba el grid del cliente, la caja de totales y las tarjetas de
# condiciones con clases propias: mismo membrete, otro sistema visual. Un CPR
# tiene que ser hermano directo de un CTZ.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_el_pdf_del_prototipo_usa_la_plantilla_global_de_cotizaciones(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_PDF_USES_GLOBAL_QUOTATION_LAYOUT: PASS.

    No se comprueba mirando: se comprueba que el render pasa por el MISMO
    `QuotationPdfService.render_html` que dibuja un CTZ, y que en el papel
    aparecen los rotulos que pone esa plantilla y no otra.
    """
    import inspect

    import app.services.prototype_quotation_pdf as modulo

    fuente = inspect.getsource(modulo)
    # PROTOTYPE_PDF_SEPARATE_LAYOUT_CREATED: NO.
    assert "get_template" not in fuente
    assert "Environment(" not in fuente
    assert "self._base.render_html(" in fuente

    # PROTOTYPE_PDF_DUPLICATED_BASE_TEMPLATE: NO. La plantilla propia ya no existe.
    assert not _EXISTE_PLANTILLA_PROPIA
    assert not _EXISTE_DOCUMENTO_PROPIO

    documento = await _confirmada(api, admin_csrf, db_session, "_pdfglobal")
    respuesta = await api.get(f"{COTIZADOR}/{documento['id']}/pdf", headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    texto = _texto_pdf(respuesta.content)

    # Los rotulos son los de `quotations/quotation.html`, literalmente. Se
    # comparan sin distinguir mayusculas porque el sistema documental las
    # transforma al dibujar: lo que se afirma es el rotulo, no su caja.
    for rotulo in (
        "Datos del Cliente",
        "Descripción del Producto",
        "P. Unitario",
        "Subtotal",
        "TOTAL",
        "Página 1 de 1",
    ):
        assert contiene(texto, rotulo), f"{rotulo} — el papel dice: {texto}"

    # Lo unico que cambia es el tipo de documento y el correlativo.
    assert "COTIZACIÓN DE PROTOTIPO" in texto
    assert documento["code"].startswith("CPR-")


@pytest.mark.asyncio
async def test_el_plazo_y_lo_acordado_salen_en_las_condiciones_del_documento_global(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Sin bloques inventados: van en la tarjeta de Condiciones Comerciales.

    Son compromisos, no atributos de un producto de catalogo: en la tabla no
    tendrian columna.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_pdfcond")
    datos = _payload(caso) | {
        "technical_specifications": {"finish": "Mate", "color": "Crema", "technique": "Torno"}
    }
    creada = await api.post(COTIZADOR, json=datos, headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    await api.post(f"{COTIZADOR}/{creada.json()['id']}/confirm", json={}, headers=head(admin_csrf))

    respuesta = await api.get(f"{COTIZADOR}/{creada.json()['id']}/pdf", headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    texto = _texto_pdf(respuesta.content)

    for acordado in (
        "Plazo estimado de desarrollo: 6 días",
        "Acabado: Mate",
        "Color: Crema",
        "Técnica: Torno",
    ):
        assert contiene(texto, acordado), f"{acordado} — el papel dice: {texto}"


# ---------------------------------------------------------------------------
# Regresion documental: un CTZ y un CPR salen del mismo sistema de documentos
#
# La comprobacion que faltaba era mirar los dos papeles uno al lado del otro.
# Hacerlo a mano exigia un CTZ valido, y un CTZ valido exige horno con factores
# de ocupacion, receta activa y gramos por pieza: por eso no salia en un
# navegador local. Pero la suite ya sabe sembrar todo eso, asi que la
# comparacion deja de ser una captura de una tarde y pasa a ser una prueba.
#
# Ningun dato productivo participa: se siembra en el esquema de pruebas.
# ---------------------------------------------------------------------------

#: Los rotulos que imprime `base_document.html` con sus componentes. No son
#: adornos: son las secciones del documento comercial de la casa. Si el CPR
#: perdiera una, o el CTZ tuviera una que el CPR no tiene, serian dos sistemas
#: visuales distintos con el mismo membrete —que es exactamente el defecto que
#: esta correccion vino a arreglar.
ESQUELETO_DOCUMENTAL = (
    "Datos del Cliente",
    "Descripción del Producto",
    "Cant.",
    "P. Unitario",
    "Subtotal",
    "IGV",
    "TOTAL",
    "Condiciones Comerciales",
    "Página 1 de 1",
)


def aplanar(texto: str) -> str:
    """Espacios colapsados. Para leer, y para afirmar AUSENCIAS."""
    return re.sub(r"\s+", " ", texto)


def _comparable(texto: str) -> str:
    """Sin ningun espacio y en minusculas.

    `pypdf` no lee palabras: lee glifos con coordenadas, y decide poner un
    espacio cuando el hueco horizontal le parece bastante. Con otras fuentes
    instaladas ese hueco cambia, y «P. UNITARIO» sale «P.UNITARIO». Por eso
    en la CI fallaba justo esa etiqueta mientras «Cant.» y «Subtotal» —de la
    MISMA fila y de una sola palabra— pasaban.

    Quitar los espacios de los DOS lados afirma lo que el rotulo dice sin
    afirmar nada sobre como el extractor repartio los huecos. Se usa solo
    para presencias: para una ausencia seria mas laxo de la cuenta, y ahi
    interesa lo contrario.
    """
    return re.sub(r"\s+", "", texto).lower()


def contiene(texto: str, rotulo: str) -> bool:
    """Si el papel dice eso, sin importar mayusculas ni espaciado."""
    return _comparable(rotulo) in _comparable(texto)


def _texto_pdf(contenido: bytes) -> str:
    """El texto del papel, con los espacios aplanados.

    Un rotulo se busca por lo que DICE, no por donde el renderizador
    decidio partir la linea. La primera version de estas pruebas no
    aplanaba, y en la CI fallaba «P. Unitario» —la unica etiqueta de dos
    palabras de una columna estrecha— mientras «Cant.» y «Subtotal», de la
    MISMA fila, si aparecian: sin las mismas fuentes instaladas el ancho de
    esa celda cambia y el corte cae entre «P.» y «Unitario». Atar la
    asercion a eso probaba las fuentes de la maquina, no el documento.
    """
    crudo = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(contenido)).pages)
    return aplanar(crudo)


def _secciones(texto: str) -> set[str]:
    """Que secciones del sistema documental aparecen en este papel.

    Se compara en minusculas porque el sistema documental pone los titulos en
    mayusculas por CSS: el rotulo se dibuja «DATOS DEL CLIENTE» aunque en la
    plantilla diga «Datos del Cliente».
    """
    return {rotulo for rotulo in ESQUELETO_DOCUMENTAL if contiene(texto, rotulo)}


@pytest.mark.asyncio
async def test_el_cpr_y_el_ctz_son_el_mismo_documento(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """GLOBAL_PDF_BASE_REUSED: PASS. La regresion visual, comparada de verdad.

    No se afirma que cada documento «contiene» los rotulos por separado, que
    pasaria aunque a los dos les faltara la misma seccion. Se afirma que el
    conjunto de secciones es EL MISMO conjunto, y que ese conjunto es el
    completo.
    """
    from tests.db.test_quotation_pdf_api import _setup_confirmed_quotation

    ctz = await _setup_confirmed_quotation(api, admin_csrf, db_session)
    papel_ctz = await api.get(f"/api/v1/quotations/{ctz['id']}/pdf", headers=head(admin_csrf))
    assert papel_ctz.status_code == 200, papel_ctz.text

    cpr = await _confirmada(api, admin_csrf, db_session, "_regresion")
    papel_cpr = await api.get(f"{COTIZADOR}/{cpr['id']}/pdf", headers=head(admin_csrf))
    assert papel_cpr.status_code == 200, papel_cpr.text

    # Quedan en disco para poder mirarlos, que era la peticion original.
    (tmp_path / "CTZ.pdf").write_bytes(papel_ctz.content)
    (tmp_path / "CPR.pdf").write_bytes(papel_cpr.content)

    texto_ctz = _texto_pdf(papel_ctz.content)
    texto_cpr = _texto_pdf(papel_cpr.content)

    # Que esto sobreviva a otra maquina no se da por supuesto: en la CI fallaba
    # con el documento perfecto. Se comprueban aqui las tres formas en que un
    # extractor puede repartir los huecos de «P. Unitario», para que quitar
    # `contiene` falle en esta maquina y no dentro de veinte minutos.
    for reparto in ("P. Unitario", "P.Unitario", "P .  Unitario", "P.\nUnitario"):
        assert contiene(f"CANT. UNIDAD {reparto} SUBTOTAL", "P. Unitario"), reparto

    secciones_ctz = _secciones(texto_ctz)
    assert secciones_ctz == set(ESQUELETO_DOCUMENTAL), sorted(
        set(ESQUELETO_DOCUMENTAL) - secciones_ctz
    )
    # PROTOTYPE_PDF_SEPARATE_LAYOUT: NO.
    assert _secciones(texto_cpr) == secciones_ctz

    # Lo unico que cambia es de que documento se trata y su correlativo.
    assert "COTIZACIÓN DE PROTOTIPO" in texto_cpr
    assert "COTIZACIÓN DE PROTOTIPO" not in texto_ctz
    assert cpr["code"].startswith("CPR-")
    assert ctz["code"].startswith("CTZ-")


@pytest.mark.asyncio
async def test_el_cpr_no_ensena_al_cliente_lo_que_le_cuesta_al_taller(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """PROTOTYPE_INTERNAL_COST_LEAK: 0.

    Un CTZ no le dice al cliente cuanto cuesta el barro; un CPR tampoco puede
    decirle cuanto se le paga al artista. Lo que se vende es el desarrollo, y
    su precio es uno solo.
    """
    documento = await _confirmada(api, admin_csrf, db_session, "_sinfugas")
    papel = await api.get(f"{COTIZADOR}/{documento['id']}/pdf", headers=head(admin_csrf))
    assert papel.status_code == 200, papel.text
    texto = _texto_pdf(papel.content)

    # Las tarifas del caso de referencia: 200 diseño, 150 artista, 100
    # matricero, 30 fijos. Ninguna cifra intermedia sale en el papel.
    for interno in ("200.00", "150.00", "100.00", "30.00"):
        assert interno not in texto, interno
    for rotulo in ("Diseño", "Artista", "Matricero", "Tarifa", "Costo", "Horno", "Quema"):
        assert rotulo.lower() not in texto.lower(), rotulo

    # Lo que si sale: el total acordado.
    assert "531.00" in texto


# ---------------------------------------------------------------------------
# LA TARIFA DE CASA Y EL OVERRIDE
#
# Nulo NO es cero. Nulo significa «cobra lo que cobre la casa», y por eso un
# borrador con override vacio tiene que seguir viendo la tarifa VIGENTE: si al
# crearlo se copiara el valor de Configuracion dentro del override, subir la
# tarifa no alcanzaria a los borradores abiertos y nadie sabria por que.
#
# Al emitir cambia la regla, y tambien a proposito: lo que se firma se congela.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_borrador_sin_override_usa_la_tarifa_vigente_de_la_casa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NULL_OVERRIDE_USES_LIVE_SETTING: PASS."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_vivo")
    datos = _payload(caso)
    assert datos.get("design_rate_override") is None

    respuesta = await api.post(f"{COTIZADOR}/preview", json=datos, headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    # 3 dias a la tarifa de la casa (80).
    assert Decimal(respuesta.json()["costing"]["design_rate"]) == Decimal(80)


@pytest.mark.asyncio
async def test_subir_la_tarifa_alcanza_a_un_borrador_ya_guardado(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """NULL_OVERRIDE_USES_LIVE_SETTING sobre un DRAFT que ya existe.

    Este es el caso que de verdad importa: el borrador se guardo AYER con la
    tarifa vieja y hoy la casa cobra otra cosa. Mientras nadie lo firme, vale
    la de hoy.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_sube")
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    documento = creada.json()
    assert Decimal(documento["costing"]["design_rate"]) == Decimal(80)

    await _ajustes(db_session, prototype_design_rate=Decimal(90))

    devuelta = await api.get(f"{COTIZADOR}/{documento['id']}", headers=head(admin_csrf))
    assert devuelta.status_code == 200, devuelta.text
    assert Decimal(devuelta.json()["costing"]["design_rate"]) == Decimal(90)


@pytest.mark.asyncio
async def test_crear_un_borrador_no_copia_la_tarifa_dentro_del_override(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """DEFAULT_VALUE_COPIED_INTO_OVERRIDE: NO.

    Se mira la COLUMNA, no la respuesta: copiar el valor ahi convertiria una
    herencia en un precio pactado, y el sintoma solo aparecerian semanas
    despues, cuando alguien subiera la tarifa y los borradores no se movieran.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_nocopia")
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text

    db_session.expire_all()
    fila = await db_session.get(PrototypeQuotation, creada.json()["id"])
    assert fila is not None
    assert fila.design_rate_override is None
    assert fila.artist_rate_override is None
    assert fila.mold_maker_price_override is None
    assert fila.fixed_cost_override is None


@pytest.mark.asyncio
async def test_un_override_explicito_manda_sobre_la_tarifa_de_la_casa(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """EXPLICIT_OVERRIDE_WINS_OVER_SETTING: PASS."""
    caso = await _caso_referencia(api, admin_csrf, db_session, "_manda")
    datos = _payload(caso) | {"design_rate_override": "95"}

    respuesta = await api.post(f"{COTIZADOR}/preview", json=datos, headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    assert Decimal(respuesta.json()["costing"]["design_rate"]) == Decimal(95)

    # Y sigue mandando aunque la casa cambie la suya.
    await _ajustes(db_session, prototype_design_rate=Decimal(120))
    otra = await api.post(f"{COTIZADOR}/preview", json=datos, headers=head(admin_csrf))
    assert Decimal(otra.json()["costing"]["design_rate"]) == Decimal(95)


@pytest.mark.asyncio
async def test_al_emitir_se_congela_la_tarifa_efectiva_y_no_la_de_manana(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """CONFIRMED_EFFECTIVE_RATE_SNAPSHOTTED y ..._IGNORES_FUTURE_SETTING_CHANGE.

    Un documento emitido es un compromiso. Que cambiar una tarifa moviera el
    total de una cotizacion ya firmada no seria una funcionalidad: seria que
    el papel y el sistema dicen cosas distintas.
    """
    caso = await _caso_referencia(api, admin_csrf, db_session, "_congela2")
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text
    confirmada = await api.post(
        f"{COTIZADOR}/{creada.json()['id']}/confirm", headers=head(admin_csrf)
    )
    assert confirmada.status_code == 200, confirmada.text
    emitida = confirmada.json()
    total_emitido = Decimal(emitida["costing"]["commercial_gross_total"])
    assert Decimal(emitida["costing"]["design_rate"]) == Decimal(80)
    assert total_emitido == Decimal("531.00")

    # La casa sube sus tarifas al dia siguiente.
    await _ajustes(
        db_session,
        prototype_design_rate=Decimal(120),
        prototype_artist_rate=Decimal(200),
        prototype_fixed_cost=Decimal(500),
    )

    db_session.expire_all()
    devuelta = await api.get(f"{COTIZADOR}/{emitida['id']}", headers=head(admin_csrf))
    assert devuelta.status_code == 200, devuelta.text
    costeo = devuelta.json()["costing"]
    assert Decimal(costeo["design_rate"]) == Decimal(80)
    assert Decimal(costeo["commercial_gross_total"]) == total_emitido

    # Y el papel tampoco se mueve.
    papel = await api.get(f"{COTIZADOR}/{emitida['id']}/pdf", headers=head(admin_csrf))
    assert papel.status_code == 200, papel.text
    assert contiene(_texto_pdf(papel.content), "531.00")
