"""Backend de UNA revision, para las pruebas E2E del frontend.

Fase 010G. Las E2E de una rama tienen que ejecutarse contra codigo que contenga
esa rama. Hasta aqui la unica suite E2E del proyecto apuntaba a produccion, asi
que una pantalla nueva solo podia «probarse» despues de desplegarla: la prueba
llegaba cuando ya no podia impedir nada.

Este modulo levanta el backend de la revision contra un PostgreSQL efimero y lo
deja listo para que el frontend construido desde la misma revision hable con el.

## Por que la autenticacion es un doble

El backend autentica contra Supabase, un servicio externo. En CI no hay —ni debe
haber— credenciales de Supabase de produccion, y aunque las hubiera, iniciar
sesion en el Supabase real desde una rama sin revisar es exactamente lo que no
se quiere. Asi que Supabase, los perfiles y el almacen de objetos se sustituyen
por los MISMOS dobles con los que corren las pruebas de base de datos
(`tests/fakes.py`, `tests/db/fakes.py`). Todo lo demas —rutas, servicios, motor
de calculo, migraciones, PostgreSQL— es el codigo real de la revision.

## Por que no puede llegar a produccion

- vive en `tests/`, y el `Dockerfile` solo copia `app` y `alembic`;
- se niega a arrancar sin `GREDA_E2E_REVISION=1`;
- se niega a arrancar si `DATABASE_URL` no apunta a localhost.

Las credenciales del administrador llegan por entorno y no estan escritas en
ninguna parte: el workflow las genera al azar en cada corrida. El correo tiene
que usar un dominio no reservado —`example.com` sirve—: la API valida el formato
y rechaza `.local`, `.test` o `.localhost`.

## Uso

    GREDA_E2E_REVISION=1 DATABASE_URL=postgresql://.../greda_e2e \\
    E2E_EMAIL=... E2E_PASSWORD=... \\
    python -m tests.e2e.servidor_revision --port 8000

Antes de servir siembra, por la propia API, lo minimo que el flujo necesita para
recorrerse: un cliente, una pasta valorizada, dos hornos con tarifas y la
configuracion comercial.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from typing import NoReturn

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy.dialects.postgresql.asyncpg import dialect as AsyncpgDialect
from sqlalchemy.engine import make_url

from app.api.deps import get_object_storage, get_profile_repository, get_supabase_auth_client
from app.core.config import get_settings
from app.db.session import normalize_database_url
from app.main import create_app
from app.models.profile import Profile, UserRole
from tests.db.fakes import FakeObjectStorage
from tests.fakes import FakeProfileRepository, FakeSupabaseAuthClient

#: Identidad fija del administrador de la corrida. No es secreta: sin la
#: contrasena, que es aleatoria por corrida, no sirve para nada.
ADMIN_ID = uuid.UUID("0e2e0e2e-0000-4000-8000-000000000010")

HOSTS_LOCALES = {"localhost", "127.0.0.1", "::1"}

#: Lo UNICO que la URL de la base puede entregarle a asyncpg. Cualquier parametro
#: de la query llega tal cual a la conexion —`hostaddr`, `service`, `passfile`—,
#: y aunque hoy ninguno de esos lleve a un servidor remoto con un host local
#: explicito (la re-revision de Codex lo comprobo), una base de pruebas no
#: necesita ninguno. Lista blanca y no lista negra: lo que no se conoce, fuera.
ARGUMENTOS_PERMITIDOS = {"host", "port", "user", "password", "database"}


def _abortar(motivo: str) -> NoReturn:
    print(f"[servidor_revision] {motivo}", file=sys.stderr)
    raise SystemExit(2)


def argumentos_de_conexion(url: str) -> dict[str, object]:
    """Lo que el dialecto de SQLAlchemy le entregara a asyncpg con esta URL."""
    _, argumentos = AsyncpgDialect().create_connect_args(make_url(normalize_database_url(url)))
    return dict(argumentos)


def hosts_de_conexion(url: str) -> list[str | None]:
    """Los hosts a los que asyncpg se conectara DE VERDAD con esta URL.

    No se lee el host de la cadena: se le pide al propio dialecto de SQLAlchemy
    los argumentos que entregara a asyncpg, porque la cadena miente. En
    `postgresql://u:p@localhost/db?host=db.remoto` el `hostname` que ve
    `urlparse` es `localhost`, pero asyncpg recibe `host="db.remoto"` y se
    conecta alli. Lo encontro la revision de Codex y se comprobo antes de
    corregirlo. Tambien cubre las URLs multi-host, que llegan como lista.
    """
    argumentos = argumentos_de_conexion(url)
    host = argumentos.get("host")
    if isinstance(host, (list, tuple)):
        return [str(h) for h in host]
    return [None if host is None else str(host)]


def _comprobar_entorno() -> tuple[str, str]:
    """Las condiciones sin las cuales este modulo no arranca."""
    if os.environ.get("GREDA_E2E_REVISION") != "1":
        _abortar("GREDA_E2E_REVISION=1 es obligatorio: este backend usa autenticacion simulada.")
    # Se valida la URL que usara LA APLICACION, no la variable de entorno: la
    # configuracion tambien puede cargarla de un `.env`, y una guardia que mira
    # un sitio mientras la aplicacion lee de otro no guarda nada.
    get_settings.cache_clear()
    url = get_settings().DATABASE_URL.get_secret_value()
    if not url:
        _abortar("DATABASE_URL no esta definida.")
    try:
        extra = sorted(set(argumentos_de_conexion(url)) - ARGUMENTOS_PERMITIDOS)
        hosts = hosts_de_conexion(url)
    except Exception as error:
        # Cualquier URL que no se entienda se rechaza: no se arranca a ciegas.
        _abortar(f"DATABASE_URL no se pudo interpretar: {type(error).__name__}.")
    if extra:
        _abortar(f"DATABASE_URL lleva parametros de conexion no permitidos: {extra!r}.")
    # Sin host explicito asyncpg cae en PGHOST o en un socket: se exige que lo diga.
    remotos = [h for h in hosts if h not in HOSTS_LOCALES]
    if remotos:
        _abortar(f"DATABASE_URL tiene que conectar solo a localhost y conectaria a {remotos!r}.")
    email = os.environ.get("E2E_EMAIL", "")
    clave = os.environ.get("E2E_PASSWORD", "")
    if not email or not clave:
        _abortar("E2E_EMAIL y E2E_PASSWORD son obligatorios.")
    return email, clave


def construir_app(email: str, clave: str) -> FastAPI:
    """La aplicacion real de la revision, con los tres dobles externos."""
    get_settings.cache_clear()
    aplicacion = create_app(get_settings())

    supabase = FakeSupabaseAuthClient()
    supabase.register(email=email, password=clave, user_id=ADMIN_ID)

    perfil = Profile()
    perfil.id = ADMIN_ID
    perfil.display_name = "Administrador E2E"
    perfil.role = UserRole.ADMIN
    perfil.active = True
    perfiles = FakeProfileRepository({ADMIN_ID: perfil})
    almacen = FakeObjectStorage()

    aplicacion.dependency_overrides[get_supabase_auth_client] = lambda: supabase
    aplicacion.dependency_overrides[get_profile_repository] = lambda: perfiles
    aplicacion.dependency_overrides[get_object_storage] = lambda: almacen
    return aplicacion


async def _ok(respuesta: httpx.Response, que: str) -> httpx.Response:
    if respuesta.status_code >= 300:
        _abortar(f"sembrando {que}: HTTP {respuesta.status_code} {respuesta.text}")
    return respuesta


async def sembrar(aplicacion: FastAPI, email: str, clave: str) -> None:
    """Lo minimo para recorrer los siete pasos, creado por la API.

    Por la API y no con SQL: asi la siembra pasa por las mismas validaciones y
    snapshots que usara la prueba, y un cambio de contrato la rompe aqui, con
    un mensaje, en vez de dejar datos que la API ya no aceptaria.
    """
    transporte = httpx.ASGITransport(app=aplicacion)
    async with httpx.AsyncClient(transport=transporte, base_url="http://revision") as api:
        csrf = (await api.get("/api/v1/auth/csrf")).json()["csrf_token"]
        await _ok(
            await api.post(
                "/api/v1/auth/login",
                json={"email": email, "password": clave},
                headers={"X-CSRF-Token": csrf},
            ),
            "inicio de sesion",
        )
        cabeceras = {"X-CSRF-Token": str(api.cookies.get("greda_csrf"))}

        # Configuracion comercial: IGV y escalon de redondeo.
        vigente = (await api.get("/api/v1/settings/commercial")).json()
        await _ok(
            await api.put(
                "/api/v1/settings/commercial",
                json={"version": vigente["version"], "tax_percent": "18", "rounding_step": "0.5"},
                headers=cabeceras,
            ),
            "configuracion comercial",
        )

        # Un cliente.
        await _ok(
            await api.post(
                "/api/v1/partners",
                json={"name": "Cliente E2E", "role": "CLIENT"},
                headers=cabeceras,
            ),
            "cliente",
        )

        # Una pasta valorizada.
        categoria = await _ok(
            await api.post(
                "/api/v1/categories",
                json={"name": "Pastas E2E", "parent_id": None},
                headers=cabeceras,
            ),
            "categoria",
        )
        pasta = await _ok(
            await api.post(
                "/api/v1/products",
                json={
                    "name": "Arcilla E2E",
                    "product_type": "RAW_MATERIAL",
                    "product_category_id": int(categoria.json()["id"]),
                    "base_uom_code": "g",
                    "purchasable": True,
                },
                headers=cabeceras,
            ),
            "pasta",
        )
        await _ok(
            await api.put(
                f"/api/v1/quoter-v2/materials/{pasta.json()['id']}",
                json={
                    "material_kind": "BODY",
                    "origin": "PURCHASE",
                    "purchase_quantity": "100000",
                    "purchase_cost": "100",
                    "transport_cost": "30",
                },
                headers=cabeceras,
            ),
            "valorizacion de la pasta",
        )

        # Dos hornos con sus tarifas: el chico para por menor, el grande para
        # por mayor. Son los numeros del Excel aprobado.
        hornos: dict[str, int] = {}
        for nombre, capacidad in (("Horno chico E2E", 17000), ("Horno grande E2E", 200000)):
            horno = await _ok(
                await api.post(
                    "/api/v1/kilns",
                    json={"name": nombre, "capacity_volume_cm3": str(capacidad)},
                    headers=cabeceras,
                ),
                nombre,
            )
            hornos[nombre] = int(horno.json()["id"])
            for indice, tipo in enumerate(("LOW", "HIGH")):
                await _ok(
                    await api.put(
                        f"/api/v1/quoter-v2/settings/kiln-rates/{horno.json()['id']}/{tipo}",
                        json={
                            "gas_cost": ("35", "70")[indice],
                            "external_rate": ("200", "250")[indice],
                            "student_rate": ("90", "180")[indice],
                        },
                        headers=cabeceras,
                    ),
                    f"tarifas de {nombre}",
                )

        ajustes = (await api.get("/api/v1/quoter-v2/settings")).json()["settings"]
        await _ok(
            await api.put(
                "/api/v1/quoter-v2/settings",
                json={
                    "expected_version": ajustes["version"],
                    "workday_hours": "8",
                    "space_service_cost_per_day": "140",
                    "administrative_cost_per_quote": "200",
                    "commercial_factor_min": "2",
                    "commercial_factor_default": "3",
                    "commercial_factor_max": "3",
                    "retail_kiln_id": hornos["Horno chico E2E"],
                    "wholesale_kiln_id": hornos["Horno grande E2E"],
                    "illustration_daily_rate": "88",
                    "illustration_pieces_per_workday": "8",
                },
                headers=cabeceras,
            ),
            "configuracion del Cotizador V2",
        )
    print("[servidor_revision] siembra completa", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backend de una revision para las E2E del frontend."
    )
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--sin-siembra", action="store_true")
    argumentos = parser.parse_args()

    email, clave = _comprobar_entorno()
    aplicacion = construir_app(email, clave)

    async def principal() -> None:
        # Siembra y servidor en el MISMO bucle de eventos: el pool de asyncpg
        # queda atado al bucle que lo abre, y sembrar con `asyncio.run` para
        # luego servir con `uvicorn.run` dejaba al servidor con conexiones de
        # un bucle ya cerrado.
        if not argumentos.sin_siembra:
            await sembrar(aplicacion, email, clave)
        configuracion = uvicorn.Config(
            aplicacion, host="127.0.0.1", port=argumentos.port, log_level="warning"
        )
        await uvicorn.Server(configuracion).serve()

    asyncio.run(principal())


if __name__ == "__main__":
    main()
