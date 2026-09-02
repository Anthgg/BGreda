"""Contrato de la superficie publica de seguimiento.

Estos son los unicos campos que la aplicacion devuelve sin sesion. La lista
esta escrita dos veces a proposito —aqui y en `PublicTrackingData`— y las dos
tienen que decir lo mismo: hay una prueba que compara los dos conjuntos de
campos y falla si alguien anade uno a un lado y se olvida del otro.

No hay estados, ni frases traducidas, ni tonos de color: el backend manda
codigos y fechas. Traducir aqui obligaria a desplegar el backend para corregir
una errata de la interfaz.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.production import ProductionOrderStatus


class PublicTrackingItemOut(BaseModel):
    """Una pieza. Su nombre comercial y cuantas; ni codigo interno ni medidas."""

    model_config = ConfigDict(from_attributes=True)

    product_name: str
    quantity: int | None


class PublicTrackingOut(BaseModel):
    """Estado publico de una orden de produccion.

    Lo que NO esta aqui es la parte importante: ni cotizacion de origen, ni
    almacen, ni material preparado, ni gramos, ni receta, ni saldos, ni cliente,
    ni quien la manejo, ni identificadores de base de datos, ni el token del QR.
    """

    model_config = ConfigDict(from_attributes=True)

    company_name: str
    order_code: str
    status: ProductionOrderStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    items: list[PublicTrackingItemOut]


class TrackingInternalLinkOut(BaseModel):
    """Puente del seguimiento publico hacia la vista interna.

    Solo se responde a quien ya tiene sesion. Es el unico sitio de toda la
    superficie de seguimiento donde aparece un identificador interno, y por eso
    exige autenticacion: sin ella, el seguimiento publico seria un traductor de
    tokens a identificadores.
    """

    production_order_id: int
