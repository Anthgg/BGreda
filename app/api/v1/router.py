"""Agregador de rutas de la version 1 de la API."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth.router)
