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
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import StockMovement
from app.models.prototype_quotations import (
    PrototypeQuotation,
    PrototypeQuotationMaterial,
)
from app.models.prototypes import Prototype
from app.models.settings import CommercialSettings
from tests.db.test_masters_api import create_category, create_product
from tests.db.test_quotation_builder_api import head

COTIZADOR = "/api/v1/prototype-quotations"
KILNS = "/api/v1/kilns"
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


async def _horno(
    api: httpx.AsyncClient, csrf: str, sufijo: str, *, baja: str = "350", dias: int = 3
) -> dict[str, Any]:
    respuesta = await api.post(
        KILNS,
        json={"name": f"Horno prototipo{sufijo}", "capacity_volume_cm3": "1000000"},
        headers=head(csrf),
    )
    assert respuesta.status_code == 201, respuesta.text
    horno = respuesta.json()
    for tipo, importe in (("LOW", baja), ("HIGH", "600")):
        creada = await api.post(
            f"{KILNS}/{horno['id']}/rates",
            json={"firing_type": tipo, "rate": importe, "valid_from": "2026-01-01"},
            headers=head(csrf),
        )
        assert creada.status_code == 201, creada.text
    if dias != horno.get("firing_days_per_batch"):
        editado = await api.put(
            f"{KILNS}/{horno['id']}",
            json={"firing_days_per_batch": dias},
            headers=head(csrf),
        )
        if editado.status_code == 200:
            horno = editado.json()
    return dict(horno)


async def _ajustes(db_session: AsyncSession, **valores: Any) -> None:
    """Escribe la configuracion comercial de la casa."""
    fila = await db_session.scalar(select(CommercialSettings).limit(1))
    assert fila is not None
    for campo, valor in valores.items():
        setattr(fila, campo, valor)
    await db_session.commit()


async def _caso_excel(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, sufijo: str
) -> dict[str, Any]:
    """El caso canonico del Excel v2, montado con maestros reales.

    3 dias de diseno a 80, 2 de artista a 100, 1.25 kg de pasta a 8/kg, una
    hornada baja a 350 y un dia de secado. Debe dar 800 / 144 / 944 / 9.
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
    horno = await _horno(api, csrf, sufijo)
    return {
        "customer_id": cliente["id"],
        "description": "Taza personalizada",
        "quantity": 1,
        "width_cm": "15",
        "length_cm": "15",
        "height_cm": "20",
        "design_days": "3",
        "artist_days": "2",
        "kiln_id": horno["id"],
        "firing_type": "LOW",
        "firing_batches": 1,
        "drying_days": "1",
        "materials": [
            {
                "product_id": pasta["id"],
                "quantity_per_prototype": "1.25",
                "is_body_material": True,
            }
        ],
        "_pasta": pasta,
        "_horno": horno,
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
        "pq_firing_type_allowed",
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
async def test_el_caso_del_excel_da_800_144_944_y_9_dias_por_la_api(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """EXCEL_FIXTURE por el servicio completo, no solo por el motor puro."""
    caso = await _caso_excel(api, admin_csrf, db_session, "_excel")
    respuesta = await api.post(
        f"{COTIZADOR}/preview", json=_payload(caso), headers=head(admin_csrf)
    )
    assert respuesta.status_code == 200, respuesta.text
    costeo = respuesta.json()["costing"]

    assert Decimal(costeo["design_cost"]) == Decimal("240.00")
    assert Decimal(costeo["artist_cost"]) == Decimal("200.00")
    assert Decimal(costeo["materials_cost"]) == Decimal("10.00")
    assert Decimal(costeo["firing_cost"]) == Decimal("350.00")
    assert Decimal(costeo["base_cost"]) == Decimal("800.00")
    assert Decimal(costeo["commercial_net_total"]) == Decimal("800.00")
    assert Decimal(costeo["commercial_tax_total"]) == Decimal("144.00")
    assert Decimal(costeo["commercial_gross_total"]) == Decimal("944.00")
    assert Decimal(costeo["estimated_days"]) == Decimal(9)


@pytest.mark.asyncio
async def test_la_previsualizacion_no_deja_rastro(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Mirar un precio no puede gastar un correlativo ni dejar borradores."""
    caso = await _caso_excel(api, admin_csrf, db_session, "_prev")
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
    caso = await _caso_excel(api, admin_csrf, db_session, "_vivo")
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
    caso = await _caso_excel(api, admin_csrf, db_session, "_pactada")
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
    caso = await _caso_excel(api, admin_csrf, db_session, "_uom")
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
    caso = await _caso_excel(api, csrf, db_session, sufijo)
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
    assert fila.commercial_gross_total == Decimal("944.00")

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
    caso = await _caso_excel(api, admin_csrf, db_session, "_nofreeze")
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
        "firing_cost",
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
    caso = await _caso_excel(api, admin_csrf, db_session, "_conc")
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

    primera = await api.post(f"{COTIZADOR}/{documento['id']}/mark-paid", headers=head(admin_csrf))
    assert primera.status_code == 200, primera.text
    assert primera.json()["payment_status"] == "PAID"
    assert primera.json()["prototype_id"] is not None

    segunda = await api.post(f"{COTIZADOR}/{documento['id']}/mark-paid", headers=head(admin_csrf))
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
    pagada = await api.post(f"{COTIZADOR}/{documento['id']}/mark-paid", headers=head(admin_csrf))
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
    await api.post(f"{COTIZADOR}/{documento['id']}/mark-paid", headers=head(admin_csrf))
    respuesta = await api.post(f"{COTIZADOR}/{documento['id']}/cancel", headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_borrador_no_se_descarga(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    caso = await _caso_excel(api, admin_csrf, db_session, "_pdfdraft")
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
    assert "944.00" in texto
    assert "800.00" in texto

    # Y nada de la cocina interna.
    for interno in ("80.00", "Tarifa", "KilnRate", "cost_snapshot", "design_rate", "350.00"):
        assert interno not in texto, interno


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
    assert "944.00" in texto_despues
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

    caso = await _caso_excel(api, admin_csrf, db_session, "_rbac")
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
    pagada = await api.post(f"{COTIZADOR}/{documento['id']}/mark-paid", headers=head(admin_csrf))
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
    pagada = await api.post(f"{COTIZADOR}/{documento['id']}/mark-paid", headers=head(admin_csrf))
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
