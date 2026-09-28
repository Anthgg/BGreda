"""Prepare the Solo Quema W3 browser case against a disposable local database.

The quotation, line, handoff, location, and control stock are created through
the real local API. W3 has no ProductionOrderService factory for a V2 firing
handoff, so the final order row uses the same minimal ORM arrangement as the
focused PostgreSQL contract test. This module is test-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
import uuid
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.session import normalize_database_url
from app.models.firing_quotation_v2 import (
    V2FiringProductionHandoff,
    V2FiringQuotationLine,
)
from app.models.production import ProductionOrder, ProductionOrderStatus
from tests.e2e.servidor_revision import (
    ARGUMENTOS_PERMITIDOS,
    HOSTS_LOCALES,
    _comprobar_entorno,
    argumentos_de_conexion,
    hosts_de_conexion,
)

EXPECTED_REVISION = "0045"
DATABASE_NAME_PATTERN = re.compile(r"bgreda_test_010p_sq_[0-9a-f]{12}\Z")
FQ = "/api/v1/firing-quotations-v2"
COMMERCIAL = "/api/v1/settings/commercial"
KILNS = "/api/v1/kilns"
V2_SETTINGS = "/api/v1/quoter-v2/settings"
LOCATIONS = "/api/v1/inventory/locations"


def _local_database_url() -> tuple[str, str]:
    if os.environ.get("GREDA_E2E_REVISION") != "1":
        raise RuntimeError("GREDA_E2E_REVISION=1 is required")

    get_settings.cache_clear()
    settings = get_settings()
    if settings.is_production:
        raise RuntimeError("The Solo Quema helper refuses production settings")

    raw_url = settings.DATABASE_URL.get_secret_value()
    if not raw_url:
        raise RuntimeError("DATABASE_URL is required")
    extra = sorted(set(argumentos_de_conexion(raw_url)) - ARGUMENTOS_PERMITIDOS)
    if extra:
        raise RuntimeError(f"DATABASE_URL has unsupported connection options: {extra!r}")
    remote_hosts = [host for host in hosts_de_conexion(raw_url) if host not in HOSTS_LOCALES]
    if remote_hosts:
        raise RuntimeError("DATABASE_URL must connect only to loopback")

    database_name = make_url(normalize_database_url(raw_url)).database
    expected_name = os.environ.get("SOLO_QUEMA_E2E_DATABASE_NAME", "")
    if (
        not database_name
        or database_name != expected_name
        or DATABASE_NAME_PATTERN.fullmatch(database_name) is None
        or database_name == "bgreda_test_010p"
        or "test" not in database_name.lower()
    ):
        raise RuntimeError("DATABASE_URL must name this run's disposable Solo Quema test database")

    if os.environ.get("SOLO_QUEMA_E2E_EXPECTED_REVISION") != EXPECTED_REVISION:
        raise RuntimeError("The runner must pin this case to Alembic revision 0045")
    return normalize_database_url(raw_url), database_name


async def _database_state(database_url: str, expected_name: str) -> dict[str, str]:
    engine = create_async_engine(
        database_url,
        connect_args={"statement_cache_size": 0},
    )
    try:
        async with engine.connect() as connection:
            database_name = str(await connection.scalar(text("SELECT current_database()")))
            revision = str(await connection.scalar(text("SELECT version_num FROM alembic_version")))
    finally:
        await engine.dispose()

    if database_name != expected_name:
        raise RuntimeError("Connected database name does not match the disposable test database")
    if revision != EXPECTED_REVISION:
        raise RuntimeError(f"Expected Alembic {EXPECTED_REVISION}; found {revision}")
    return {"database": database_name, "revision": revision}


def _backend_url() -> str:
    raw_url = os.environ.get("E2E_BACKEND_URL", "")
    url = httpx.URL(raw_url)
    expected_port = int(os.environ.get("SOLO_QUEMA_E2E_BACKEND_PORT", "0"))
    if (
        url.scheme != "http"
        or url.host not in HOSTS_LOCALES
        or url.port is None
        or url.port != expected_port
        or url.path not in ("", "/")
        or url.query
        or url.fragment
        or url.userinfo
    ):
        raise RuntimeError("E2E_BACKEND_URL must be the runner's local backend origin")
    return str(url).rstrip("/")


async def _api_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    payload: Any | None = None,
    csrf: str | None = None,
) -> Any:
    headers = {"X-CSRF-Token": csrf} if csrf is not None else None
    response = await client.request(
        method,
        path,
        json=payload if payload is not None else ({} if method in {"POST", "PUT"} else None),
        headers=headers,
    )
    if not response.is_success:
        raise RuntimeError(f"{method} {path} -> HTTP {response.status_code}: {response.text}")
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


async def _create_order(
    database_url: str,
    handoff_id: int,
    line_id: int,
    location_id: int,
) -> dict[str, Any]:
    engine = create_async_engine(database_url, connect_args={"statement_cache_size": 0})
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as session:
            handoff = await session.get(V2FiringProductionHandoff, handoff_id)
            if handoff is None:
                raise RuntimeError("The API-created Solo Quema handoff does not exist")
            lines = (
                await session.scalars(
                    select(V2FiringQuotationLine)
                    .where(
                        V2FiringQuotationLine.v2_firing_quotation_id
                        == handoff.v2_firing_quotation_id
                    )
                    .order_by(V2FiringQuotationLine.sort_order, V2FiringQuotationLine.id)
                )
            ).all()
            if len(lines) != 1 or lines[0].id != line_id or Decimal(lines[0].quantity) != 10:
                raise RuntimeError(
                    "The API-created quotation must contain its single 10-piece line"
                )

            existing = await session.scalar(
                select(ProductionOrder.id).where(ProductionOrder.v2_firing_handoff_id == handoff_id)
            )
            if existing is not None:
                raise RuntimeError("A production order already exists for this handoff")

            # No create_for_v2_firing service exists on this branch. Keep this
            # insert inside test support; never add a production route for setup.
            tag = uuid.uuid4().hex[:12].upper()
            order = ProductionOrder(
                code=f"PO-W3-SQ-{tag}",
                v2_firing_handoff_id=handoff_id,
                stock_location_id=location_id,
                status=ProductionOrderStatus.CREATED,
                qr_token=secrets.token_urlsafe(32),
            )
            session.add(order)
            await session.commit()
            await session.refresh(order)
            return {"order_id": order.id, "order_code": order.code}
    finally:
        await engine.dispose()


async def prepare() -> dict[str, Any]:
    database_url, database_name = _local_database_url()
    await _database_state(database_url, database_name)
    email, password, _, _ = _comprobar_entorno()
    backend_url = _backend_url()
    suffix = uuid.uuid4().hex[:10].upper()

    async with httpx.AsyncClient(base_url=backend_url, timeout=45.0) as api:
        csrf = (await _api_json(api, "GET", "/api/v1/auth/csrf"))["csrf_token"]
        await _api_json(
            api,
            "POST",
            "/api/v1/auth/login",
            payload={"email": email, "password": password},
            csrf=csrf,
        )
        csrf = str(api.cookies.get("greda_csrf") or "")
        if not csrf:
            raise RuntimeError("The local test login did not issue its CSRF cookie")

        commercial = await _api_json(api, "GET", COMMERCIAL)
        await _api_json(
            api,
            "PUT",
            COMMERCIAL,
            payload={
                "version": commercial["version"],
                "tax_percent": "18",
                "rounding_step": "0.5",
            },
            csrf=csrf,
        )

        kilns: dict[str, int] = {}
        for name, capacity, gas, external, student in (
            ("Chico", "17000", ("35", "70"), ("200", "250"), ("90", "180")),
            ("Grande", "200000", ("55", "110"), ("700", "1200"), ("1000", "2000")),
        ):
            kiln = await _api_json(
                api,
                "POST",
                KILNS,
                payload={
                    "name": f"Horno {name} Solo Quema W3 {suffix}",
                    "capacity_volume_cm3": capacity,
                },
                csrf=csrf,
            )
            kiln_id = int(kiln["id"])
            kilns[name] = kiln_id
            for index, kind in enumerate(("LOW", "HIGH")):
                await _api_json(
                    api,
                    "PUT",
                    f"{V2_SETTINGS}/kiln-rates/{kiln_id}/{kind}",
                    payload={
                        "gas_cost": gas[index],
                        "external_rate": external[index],
                        "student_rate": student[index],
                    },
                    csrf=csrf,
                )

        v2_settings = (await _api_json(api, "GET", V2_SETTINGS))["settings"]
        await _api_json(
            api,
            "PUT",
            V2_SETTINGS,
            payload={
                "expected_version": v2_settings["version"],
                "retail_kiln_id": kilns["Chico"],
                "wholesale_kiln_id": kilns["Grande"],
                "piece_separation_cm": "3",
                "quotation_validity_days": 20,
                "default_customer_kind": "EXTERNAL",
            },
            csrf=csrf,
        )

        category = await _api_json(
            api,
            "POST",
            "/api/v1/categories",
            payload={"name": f"Categoría Solo Quema W3 {suffix}", "parent_id": None},
            csrf=csrf,
        )
        category_id = int(category["id"])
        glaze = await _api_json(
            api,
            "POST",
            "/api/v1/products",
            payload={
                "name": f"Esmalte Solo Quema W3 {suffix}",
                "product_type": "RAW_MATERIAL",
                "product_category_id": category_id,
                "base_uom_code": "g",
                "purchasable": True,
            },
            csrf=csrf,
        )
        await _api_json(
            api,
            "PUT",
            f"/api/v1/quoter-v2/materials/{int(glaze['id'])}",
            payload={
                "material_kind": "GLAZE",
                "origin": "PURCHASE",
                "purchase_quantity": "1000",
                "purchase_cost": "120",
                "transport_cost": "0",
            },
            csrf=csrf,
        )
        customer = await _api_json(
            api,
            "POST",
            "/api/v1/partners",
            payload={"name": f"Cliente Solo Quema W3 {suffix}", "role": "CLIENT"},
            csrf=csrf,
        )
        finished_product = await _api_json(
            api,
            "POST",
            "/api/v1/products",
            payload={
                "name": f"Control terminado Solo Quema W3 {suffix}",
                "product_type": "FINISHED_PRODUCT",
                "product_category_id": category_id,
                "base_uom_code": "unit",
            },
            csrf=csrf,
        )
        location = await _api_json(
            api,
            "POST",
            LOCATIONS,
            payload={"name": f"Taller Solo Quema W3 {suffix}"},
            csrf=csrf,
        )
        await _api_json(
            api,
            "POST",
            "/api/v1/inventory/adjustments",
            payload={
                "product_id": int(finished_product["id"]),
                "location_id": int(location["id"]),
                "quantity": "5",
                "reason": "Stock inicial de control para E2E Solo Quema W3",
            },
            csrf=csrf,
        )

        quotation = await _api_json(
            api,
            "POST",
            FQ,
            payload={
                "name": f"Solo Quema W3 {suffix}",
                "customer_id": int(customer["id"]),
            },
            csrf=csrf,
        )
        quotation_id = int(quotation["id"])
        line = await _api_json(
            api,
            "POST",
            f"{FQ}/{quotation_id}/lines",
            payload={
                "product_name": "Pieza demo Solo Quema W3",
                "quantity": 10,
                "length_cm": "8",
                "width_cm": "8",
                "height_cm": "8",
            },
            csrf=csrf,
        )
        line_id = int(line["id"])
        quotation_detail = await _api_json(api, "GET", f"{FQ}/{quotation_id}")
        if len(quotation_detail.get("lines", [])) != 1:
            raise RuntimeError("The prepared firing quotation must have exactly one line")
        if Decimal(str(quotation_detail["lines"][0]["quantity"])) != 10:
            raise RuntimeError("The prepared firing line must start at 10")

        preview = await _api_json(api, "GET", f"{FQ}/{quotation_id}/preview")
        await _api_json(
            api,
            "POST",
            f"{FQ}/{quotation_id}/confirm",
            payload={"expected_fingerprint": preview["fingerprint"]},
            csrf=csrf,
        )
        sent = await _api_json(
            api,
            "POST",
            f"{FQ}/{quotation_id}/send-to-production",
            csrf=csrf,
        )
        handoff_id = int(sent["handoff"]["id"])

    order = await _create_order(
        database_url,
        handoff_id,
        line_id,
        int(location["id"]),
    )
    return {
        **order,
        "quotation_id": quotation_id,
        "line_id": line_id,
        "handoff_id": handoff_id,
        "location_id": int(location["id"]),
        "location_name": location["name"],
        "finished_product_id": int(finished_product["id"]),
        "database": database_name,
        "revision": EXPECTED_REVISION,
    }


async def _run(command: str) -> dict[str, Any]:
    database_url, database_name = _local_database_url()
    if command == "verify":
        return await _database_state(database_url, database_name)
    if command == "prepare":
        return await prepare()
    raise RuntimeError("Unknown command")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "prepare"))
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(_run(args.command)), separators=(",", ":")))
    except Exception as error:
        print(f"[solo_quema_e2e] {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
