"""Fase 010B — dos personas configurando a la vez.

Son las pruebas que justifican el diseno de la escritura. Sin ellas, los dos
fallos que cubren solo aparecerian en produccion: uno como un 500 al azar, y el
otro —peor— como un cambio que desaparece sin que nadie se entere de que lo
habia hecho.

Van en su propio fichero, como `test_sequence_concurrency.py`, porque necesitan
sesiones independientes de verdad: comprobar una carrera dentro de una sola
transaccion no comprueba nada.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import APIError
from app.models.firings import FiringType, Kiln
from app.models.profile import UserRole
from app.models.quoter_v2_settings import V2CommercialSettings, V2KilnRate
from app.models.settings import SINGLETON_ID
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder
from app.services.quoter_v2_settings import V2SettingsConflictError, V2SettingsService
from tests.db.conftest import TEST_USER_ID

PETICIONES = 10

USUARIO = AuthenticatedUser(
    id=TEST_USER_ID,
    email="admin@empresa.com",
    display_name="Administrador",
    role=UserRole.ADMIN,
)


async def _horno(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with sessionmaker() as session:
        horno = Kiln(
            code="KILN-CONC",
            name="Horno de concurrencia",
            capacity_volume_cm3=Decimal(17000),
        )
        session.add(horno)
        await session.commit()
        return horno.id


async def _fijar_tarifa(
    sessionmaker: async_sessionmaker[AsyncSession], kiln_id: int, gas: str
) -> None:
    """Una peticion completa: su propia sesion y su propio commit."""
    async with sessionmaker() as session:
        servicio = V2SettingsService(session, AuditRecorder(session))
        await servicio.set_kiln_rate(
            kiln_id, FiringType.LOW, {"gas_cost": Decimal(gas)}, user=USUARIO
        )
        await session.commit()


async def _editar_configuracion(
    sessionmaker: async_sessionmaker[AsyncSession], version: int, horas: str
) -> str:
    """Devuelve 'ok' o el codigo del error, para poder contarlos."""
    async with sessionmaker() as session:
        servicio = V2SettingsService(session, AuditRecorder(session))
        try:
            await servicio.update(
                {"workday_hours": Decimal(horas)}, expected_version=version, user=USUARIO
            )
            await session.commit()
        except APIError as error:
            await session.rollback()
            return error.code
        return "ok"


# ---------------------------------------------------------------------------
# Tarifas de horno
# ---------------------------------------------------------------------------
async def test_diez_peticiones_simultaneas_dejan_una_sola_tarifa(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    """La carrera que un `SELECT` previo no puede cerrar.

    Entre mirar si existe y crearla cabe otra peticion. Con diez a la vez,
    nueve chocarian contra el UNIQUE y devolverian un 500 sin explicacion. El
    upsert lo resuelve en la base, que es el unico sitio donde se puede.
    """
    kiln_id = await _horno(sessionmaker_for_tests)

    await asyncio.gather(
        *(_fijar_tarifa(sessionmaker_for_tests, kiln_id, str(30 + i)) for i in range(PETICIONES))
    )

    async with sessionmaker_for_tests() as session:
        total = await session.scalar(select(func.count()).select_from(V2KilnRate))
    assert total == 1, "las peticiones simultaneas crearon tarifas duplicadas"


async def test_actualizar_una_tarifa_mueve_su_fecha(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    """`updated_at` tiene que avanzar tambien cuando escribe el upsert.

    El `onupdate` del modelo es un gancho del ORM y una sentencia Core no lo
    dispara: sin ponerlo explicito, la fila cambia de importe y conserva una
    fecha de actualizacion que ya no corresponde a nada.
    """
    kiln_id = await _horno(sessionmaker_for_tests)
    await _fijar_tarifa(sessionmaker_for_tests, kiln_id, "35")

    async with sessionmaker_for_tests() as session:
        antes = await session.scalar(text("SELECT updated_at FROM v2_kiln_rates"))

    await asyncio.sleep(0.01)
    await _fijar_tarifa(sessionmaker_for_tests, kiln_id, "40")

    async with sessionmaker_for_tests() as session:
        fila = (await session.execute(text("SELECT gas_cost, updated_at FROM v2_kiln_rates"))).one()
    assert fila.gas_cost == Decimal(40)
    assert fila.updated_at > antes, "la tarifa cambio pero su fecha se quedo atras"


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
async def test_dos_ediciones_con_la_misma_version_solo_dejan_pasar_una(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    """La comprobacion de version tiene que bloquear, no solo leer.

    Sin `with_for_update`, las dos transacciones leen version 1, las dos pasan
    la comprobacion y la ultima en confirmar pisa a la primera: el cambio de
    alguien desaparece y nadie ve un conflicto.
    """
    resultados = await asyncio.gather(
        _editar_configuracion(sessionmaker_for_tests, 1, "7"),
        _editar_configuracion(sessionmaker_for_tests, 1, "9"),
    )

    assert sorted(resultados) == sorted(["ok", V2SettingsConflictError.code]), (
        f"se esperaba un exito y un conflicto, hubo {resultados}"
    )

    async with sessionmaker_for_tests() as session:
        fila = await session.get(V2CommercialSettings, SINGLETON_ID)
        assert fila is not None
        assert fila.version == 2, "la version no avanzo exactamente una vez"


@pytest.mark.parametrize("horas", ["7", "9"])
async def test_una_edicion_sola_siempre_pasa(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession], horas: str
) -> None:
    """El bloqueo no puede convertir el caso normal en un conflicto."""
    assert await _editar_configuracion(sessionmaker_for_tests, 1, horas) == "ok"
