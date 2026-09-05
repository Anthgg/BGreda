"""Agregador de rutas de la version 1 de la API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    firings,
    identity,
    imports,
    inventory,
    masters,
    production,
    prototype_quotations,
    prototypes,
    quotation_builder,
    quotations,
    recipes,
    settings,
    tracking,
)

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth.router)
api_v1_router.include_router(settings.router)
api_v1_router.include_router(masters.router)
api_v1_router.include_router(inventory.router)
api_v1_router.include_router(imports.router)
api_v1_router.include_router(recipes.router)
api_v1_router.include_router(firings.router)
api_v1_router.include_router(quotations.router)
api_v1_router.include_router(quotation_builder.router)
api_v1_router.include_router(production.router)
#: Superficie PUBLICA de seguimiento (Fase 009I.1). Va aparte de
#: `production.router` a proposito: no comparte esquemas, no exige sesion y
#: no tiene una sola operacion que escriba.
api_v1_router.include_router(tracking.router)
#: Fase 009K. Prototipos: dominio propio, no una variante de produccion.
api_v1_router.include_router(prototypes.router)
api_v1_router.include_router(prototype_quotations.router)
api_v1_router.include_router(identity.router)
