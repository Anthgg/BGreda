"""Fase 009I.1 — la frontera de lo publico, comprobada como frontera.

`PublicTrackingData` no es un modelo mas: es el punto por el que pasa todo lo
que la aplicacion responde sin sesion. La constancia en PDF y la respuesta JSON
salen de ahi, asi que basta con que a esa clase le crezca un campo para que
crezca a la vez lo que ve cualquiera con un movil.

Estas pruebas no comprueban que hoy no se filtre nada —eso lo hacen las de base
de datos contra la respuesta real—; comprueban que **no se pueda** filtrar por
descuido: que el conjunto de campos publicos este escrito, sea el mismo en el
modelo y en el contrato, y no admita los nombres que 009I clasifico como
internos.
"""

from __future__ import annotations

from dataclasses import fields
from typing import get_type_hints

from app.documents.production import (
    PublicTrackingData,
    PublicTrackingItem,
    build_public_tracking_sheet,
)
from app.schemas.tracking import PublicTrackingItemOut, PublicTrackingOut

#: Lo que 009I.1 declaro publicable. Cambiar esta lista es una decision sobre
#: que deja de ser interno, no un ajuste de maquetacion.
CAMPOS_PUBLICOS = {
    "company_name",
    "order_code",
    "status",
    "created_at",
    "started_at",
    "completed_at",
    "cancelled_at",
    "items",
}

#: Nombres que NO pueden aparecer en la frontera. No es una lista de palabras
#: prohibidas por si acaso: es la clasificacion INTERNAL_ONLY de la auditoria,
#: escrita donde falla si alguien la contradice.
CAMPOS_INTERNOS = {
    "id",
    "quotation_id",
    "quotation_code",
    "production_order_id",
    "stock_location_id",
    "stock_location_name",
    "qr_token",
    "idempotency_key",
    "created_by",
    "created_by_name",
    "recipe_id",
    "recipe_version_id",
    "prepared_product_id",
    "prepared_product_name",
    "required_material_quantity",
    "required_material_uom",
    "material_grams_per_piece",
    "readiness",
    "customer",
    "quotation_customer_name",
}


def test_la_frontera_publica_tiene_exactamente_los_campos_declarados() -> None:
    assert {campo.name for campo in fields(PublicTrackingData)} == CAMPOS_PUBLICOS


def test_el_contrato_de_la_api_dice_lo_mismo_que_la_frontera() -> None:
    """La lista esta escrita dos veces; tienen que decir lo mismo.

    El esquema se construye con `model_validate` sobre el dataclass, asi que un
    campo que sobre en uno y falte en el otro no fallaria en tiempo de
    ejecucion: simplemente se dejaria de responder, o se responderia de mas.
    """
    assert set(PublicTrackingOut.model_fields) == CAMPOS_PUBLICOS


def test_ni_la_frontera_ni_el_contrato_admiten_un_campo_interno() -> None:
    """QR_PUBLIC_SENSITIVE_DATA_LEAK / QR_PUBLIC_INTERNAL_IDS_VISIBLE."""
    for nombres in (
        {campo.name for campo in fields(PublicTrackingData)},
        set(PublicTrackingOut.model_fields),
        {campo.name for campo in fields(PublicTrackingItem)},
        set(PublicTrackingItemOut.model_fields),
    ):
        assert not nombres & CAMPOS_INTERNOS


def test_la_pieza_publica_lleva_nombre_y_cantidad_y_nada_mas() -> None:
    """Ni el codigo interno del producto, ni las medidas, ni la receta.

    El codigo `LAB50032` no le dice nada a quien escanea y si dice como esta
    organizado el maestro por dentro.
    """
    assert {campo.name for campo in fields(PublicTrackingItem)} == {
        "product_name",
        "quantity",
    }


def test_la_hoja_publica_no_puede_ver_mas_que_la_frontera() -> None:
    """El PDF publico se compone desde el dato publico, no desde la orden.

    Se comprueba por la firma: si `build_public_tracking_sheet` aceptara una
    `ProductionOrder`, la plantilla podria imprimir el almacen con solo anadir
    una fila, y la frontera dejaria de ser una frontera.
    """
    anotaciones = get_type_hints(build_public_tracking_sheet)
    assert anotaciones["data"] is PublicTrackingData
