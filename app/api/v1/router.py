"""Agregador de rutas de la version 1 de la API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, imports, inventory, masters, recipes, settings

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth.router)
api_v1_router.include_router(settings.router)
api_v1_router.include_router(masters.router)
api_v1_router.include_router(inventory.router)
api_v1_router.include_router(imports.router)
api_v1_router.include_router(recipes.router)
