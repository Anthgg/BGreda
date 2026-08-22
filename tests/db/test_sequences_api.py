"""Configuracion de secuencias y su relacion con los numeros ya emitidos."""

from __future__ import annotations

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.sequence import DocumentSequence, DocumentSequenceIssue, SequenceType
from app.services.sequences import SequenceService

SEQUENCES = "/api/v1/settings/sequences"


def _config(version: int, **campos: object) -> dict[str, object]:
    base: dict[str, object] = {
        "prefix": "CTZ",
        "pattern": "{PREFIX}-{YYYY}-{NUMBER}",
        "padding": 6,
        "reset_policy": "YEARLY",
        "active": True,
        "version": version,
    }
    base.update(campos)
    return base


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------
async def test_sin_sesion_no_se_puede_leer(api: httpx.AsyncClient) -> None:
    assert (await api.get(SEQUENCES)).status_code == 401


async def test_se_listan_las_dos_secuencias_del_plan(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    body = (await api.get(SEQUENCES)).json()

    por_tipo = {item["sequence_type"]: item for item in body["sequences"]}
    assert set(por_tipo) == {"QUOTE", "FIRING"}
    assert por_tipo["QUOTE"]["prefix"] == "CTZ"
    assert por_tipo["FIRING"]["prefix"] == "HR"
    assert por_tipo["QUOTE"]["padding"] == 6
    assert por_tipo["QUOTE"]["reset_policy"] == "YEARLY"


async def test_la_vista_previa_usa_el_formato_configurado(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    body = (await api.get(SEQUENCES)).json()
    cotizacion = next(s for s in body["sequences"] if s["sequence_type"] == "QUOTE")

    assert cotizacion["preview"].startswith("CTZ-")
    assert cotizacion["preview"].endswith("000001")


async def test_operator_puede_consultar(api: httpx.AsyncClient, operator_csrf: str) -> None:
    assert (await api.get(SEQUENCES)).status_code == 200


# ---------------------------------------------------------------------------
# La vista previa no consume
# ---------------------------------------------------------------------------
async def test_consultar_la_vista_previa_no_consume_ningun_numero(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Es el requisito clave: mirar no gasta correlativos."""
    for _ in range(5):
        await api.get(SEQUENCES)

    contador = (
        await db_session.execute(
            select(DocumentSequence.current_value).where(
                DocumentSequence.sequence_type == SequenceType.QUOTE
            )
        )
    ).scalar_one()
    emitidos = (
        await db_session.execute(select(func.count()).select_from(DocumentSequenceIssue))
    ).scalar_one()

    assert contador == 0
    assert emitidos == 0


async def test_no_existe_ningun_endpoint_publico_que_consuma_numeros(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """Nadie debe poder gastar correlativos a voluntad desde la API.

    Se aceptan 404 (la ruta no existe) y 405 (existe la ruta pero no ese
    metodo); lo que no puede ocurrir bajo ningun concepto es que el contador
    avance.
    """
    for ruta in (
        f"{SEQUENCES}/QUOTE/next",
        f"{SEQUENCES}/QUOTE/issue",
        f"{SEQUENCES}/next",
        f"{SEQUENCES}/QUOTE/consume",
    ):
        response = await api.post(ruta, headers={"X-CSRF-Token": admin_csrf})
        assert response.status_code in (404, 405), f"{ruta} responde {response.status_code}"

    contador = (
        await db_session.execute(
            select(DocumentSequence.current_value).where(
                DocumentSequence.sequence_type == SequenceType.QUOTE
            )
        )
    ).scalar_one()
    emitidos = (
        await db_session.execute(select(func.count()).select_from(DocumentSequenceIssue))
    ).scalar_one()

    assert contador == 0
    assert emitidos == 0


# ---------------------------------------------------------------------------
# Cambio de configuracion
# ---------------------------------------------------------------------------
async def test_admin_cambia_el_prefijo(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        f"{SEQUENCES}/QUOTE", json=_config(1, prefix="GRE"), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 200, response.text
    assert response.json()["prefix"] == "GRE"
    assert response.json()["preview"].startswith("GRE-")


async def test_operator_no_puede_cambiar_la_configuracion(
    api: httpx.AsyncClient, operator_csrf: str
) -> None:
    response = await api.put(
        f"{SEQUENCES}/QUOTE", json=_config(1, prefix="GRE"), headers={"X-CSRF-Token": operator_csrf}
    )

    assert response.status_code == 403


async def test_un_patron_invalido_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        f"{SEQUENCES}/QUOTE",
        json=_config(1, pattern="{PREFIX}-{YYYY}"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422


async def test_una_version_desfasada_se_rechaza(api: httpx.AsyncClient, admin_csrf: str) -> None:
    await api.put(
        f"{SEQUENCES}/QUOTE", json=_config(1, prefix="GRE"), headers={"X-CSRF-Token": admin_csrf}
    )

    response = await api.put(
        f"{SEQUENCES}/QUOTE", json=_config(1, prefix="OTR"), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.status_code == 409


async def test_el_padding_cambia_la_vista_previa(api: httpx.AsyncClient, admin_csrf: str) -> None:
    response = await api.put(
        f"{SEQUENCES}/QUOTE", json=_config(1, padding=3), headers={"X-CSRF-Token": admin_csrf}
    )

    assert response.json()["preview"].endswith("-001")


# ---------------------------------------------------------------------------
# Los documentos ya emitidos no se reescriben
# ---------------------------------------------------------------------------
async def test_cambiar_el_prefijo_no_altera_los_numeros_ya_emitidos(
    api: httpx.AsyncClient,
    admin_csrf: str,
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """Regla del proyecto: el pasado no se reescribe."""
    async with sessionmaker_for_tests() as session:
        emitido = await SequenceService(session).issue(SequenceType.QUOTE)
        await session.commit()
    assert emitido.startswith("CTZ-")

    version = next(
        s["version"]
        for s in (await api.get(SEQUENCES)).json()["sequences"]
        if s["sequence_type"] == "QUOTE"
    )
    await api.put(
        f"{SEQUENCES}/QUOTE",
        json=_config(version, prefix="GRE"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    guardado = (
        await db_session.execute(select(DocumentSequenceIssue.formatted_value))
    ).scalar_one()

    assert guardado == emitido
    assert guardado.startswith("CTZ-")


async def test_el_contador_continua_tras_cambiar_el_prefijo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker_for_tests() as session:
        await SequenceService(session).issue(SequenceType.QUOTE)
        await session.commit()

    version = next(
        s["version"]
        for s in (await api.get(SEQUENCES)).json()["sequences"]
        if s["sequence_type"] == "QUOTE"
    )
    await api.put(
        f"{SEQUENCES}/QUOTE",
        json=_config(version, prefix="GRE"),
        headers={"X-CSRF-Token": admin_csrf},
    )

    async with sessionmaker_for_tests() as session:
        siguiente = await SequenceService(session).issue(SequenceType.QUOTE)
        await session.commit()

    # Nuevo prefijo, pero el numero sigue creciendo: no se reinicia ni se repite.
    assert siguiente == "GRE-2026-000002" or siguiente.endswith("000002")
    assert siguiente.startswith("GRE-")


# ---------------------------------------------------------------------------
# Reinicio por periodo
# ---------------------------------------------------------------------------
async def test_el_contador_reinicia_al_cambiar_de_ano(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import date

    async with sessionmaker_for_tests() as session:
        service = SequenceService(session)
        primero = await service.issue(SequenceType.QUOTE, moment=date(2026, 12, 31))
        await session.commit()

    async with sessionmaker_for_tests() as session:
        siguiente = await SequenceService(session).issue(
            SequenceType.QUOTE, moment=date(2027, 1, 1)
        )
        await session.commit()

    assert primero == "CTZ-2026-000001"
    assert siguiente == "CTZ-2027-000001"


async def test_los_dos_anos_conviven_sin_colisionar(
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    from datetime import date

    for momento in (date(2026, 5, 1), date(2027, 5, 1)):
        async with sessionmaker_for_tests() as session:
            await SequenceService(session).issue(SequenceType.QUOTE, moment=momento)
            await session.commit()

    valores = list(
        (await db_session.execute(select(DocumentSequenceIssue.formatted_value))).scalars().all()
    )

    assert sorted(valores) == ["CTZ-2026-000001", "CTZ-2027-000001"]


async def test_una_secuencia_desactivada_no_emite(
    api: httpx.AsyncClient,
    admin_csrf: str,
    sessionmaker_for_tests: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.sequences import SequenceInactiveError

    await api.put(
        f"{SEQUENCES}/QUOTE", json=_config(1, active=False), headers={"X-CSRF-Token": admin_csrf}
    )

    import pytest

    async with sessionmaker_for_tests() as session:
        with pytest.raises(SequenceInactiveError):
            await SequenceService(session).issue(SequenceType.QUOTE)
