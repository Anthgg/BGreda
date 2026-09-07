"""Fase 009K.2 — quien preparo y quien emitio cada documento.

La regla que este archivo defiende cabe en una frase: **un documento emitido no
cambia de autor**. Si Jesus Garamendi firma una cotizacion y el mes que viene
su perfil pasa a decir «Jesus A. Garamendi Gonzales», el papel que ya se envio
al cliente sigue diciendo lo que decia. Por eso el nombre se COPIA al documento
y no se resuelve leyendo el perfil actual.

Lo comprueba de la unica forma que vale: creando, emitiendo, renombrando
despues, y volviendo a leer —incluido el PDF, que es lo que de verdad llega al
cliente—.

La otra mitad mira el hueco. Los documentos anteriores a esta fase no
registraron a nadie, y no se les puede atribuir un autor sin inventarlo: dicen
«No registrado» y ni la API ni el PDF se caen por ello.
"""

from __future__ import annotations

import io
import re
from typing import Any

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ACTOR_NO_REGISTRADO
from app.models.profile import UserRole
from app.models.prototype_quotations import PrototypeQuotation
from app.models.quotations import Quotation
from tests.db.conftest import (
    OPERATOR_EMAIL,
    OPERATOR_ID,
    OPERATOR_PASSWORD,
    TEST_USER_ID,
    authenticate,
)
from tests.db.test_production_orders_api import BUILDER, confirmar, escenario
from tests.db.test_prototype_quotations import (
    COTIZADOR,
    _caso_referencia,
    _payload,
)
from tests.db.test_quotation_builder_api import head
from tests.fakes import FakeProfileRepository

QUOTATIONS = "/api/v1/quotations"


def _texto_pdf(contenido: bytes) -> str:
    crudo = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(contenido)).pages)
    return re.sub(r"\s+", " ", crudo)


def _contiene(texto: str, rotulo: str) -> bool:
    """Sin espacios y en minusculas: `pypdf` reparte los blancos como quiere."""
    aplanar = lambda t: re.sub(r"\s+", "", t).lower()  # noqa: E731
    return aplanar(rotulo) in aplanar(texto)


def _renombrar(profiles: FakeProfileRepository, user_id: Any, nombre: str) -> None:
    """Cambia el nombre visible del perfil con el que se autentica."""
    profiles.profiles[user_id].display_name = nombre


# ---------------------------------------------------------------------------
# Cotizacion normal (CTZ)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_crear_una_cotizacion_copia_el_nombre_de_quien_la_escribe(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    profiles_fake: FakeProfileRepository,
) -> None:
    """CTZ_CREATED_BY_NAME_SNAPSHOTTED_AT_CREATE: PASS.

    El nombre se copia AL CREAR, no al emitir: un borrador ya se ensena con
    autor, y esperar a la emision dejaria ese hueco sin motivo.
    """
    _renombrar(profiles_fake, TEST_USER_ID, "Ana Perez")
    datos = await escenario(api, admin_csrf, db_session, suffix="_actor_crear")

    db_session.expire_all()
    fila = await db_session.get(Quotation, datos["quotation"]["id"])
    assert fila is not None
    assert fila.created_by_id == TEST_USER_ID
    assert fila.created_by_name == "Ana Perez"
    # Todavia no la ha emitido nadie.
    assert fila.confirmed_by_id is None
    assert fila.confirmed_by_name is None


@pytest.mark.asyncio
async def test_emitir_una_cotizacion_guarda_a_quien_la_emite_y_no_pisa_al_autor(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    profiles_fake: FakeProfileRepository,
    supabase_fake: Any,
) -> None:
    """CTZ_CONFIRMED_BY_NAME_SNAPSHOTTED_AT_CONFIRM: PASS.

    Escribe una persona y firma otra, que es como funciona una oficina. Las dos
    quedan, y cada una en su sitio.
    """
    _renombrar(profiles_fake, TEST_USER_ID, "Ana Perez")
    datos = await escenario(api, admin_csrf, db_session, suffix="_actor_emitir")

    # El segundo usuario es ADMIN de verdad: emitir no es un gesto de taller.
    profiles_fake.profiles[OPERATOR_ID].display_name = "Beto Ruiz"
    profiles_fake.profiles[OPERATOR_ID].role = UserRole.ADMIN
    csrf_beto = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
    await confirmar(api, csrf_beto, datos["quotation"])

    db_session.expire_all()
    fila = await db_session.get(Quotation, datos["quotation"]["id"])
    assert fila is not None
    assert fila.created_by_id == TEST_USER_ID
    assert fila.created_by_name == "Ana Perez"
    assert fila.confirmed_by_id == OPERATOR_ID
    assert fila.confirmed_by_name == "Beto Ruiz"


@pytest.mark.asyncio
async def test_renombrar_a_las_personas_no_reescribe_la_cotizacion_emitida(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    profiles_fake: FakeProfileRepository,
) -> None:
    """DOCUMENT_ACTOR_SNAPSHOT_IMMUTABILITY: PASS, en la API y en el papel.

    Es la prueba central de la fase. Si esto fallara, un cliente y el sistema
    tendrian delante dos versiones distintas del mismo documento.
    """
    _renombrar(profiles_fake, TEST_USER_ID, "Jesus Garamendi")
    datos = await escenario(api, admin_csrf, db_session, suffix="_inmutable")
    await confirmar(api, admin_csrf, datos["quotation"])
    quotation_id = datos["quotation"]["id"]

    papel_antes = await api.get(f"{QUOTATIONS}/{quotation_id}/pdf", headers=head(admin_csrf))
    assert papel_antes.status_code == 200, papel_antes.text
    assert _contiene(_texto_pdf(papel_antes.content), "Jesus Garamendi")

    # Al mes siguiente esa persona cambia como se llama en el sistema.
    _renombrar(profiles_fake, TEST_USER_ID, "Jesus A. Garamendi Gonzales")

    db_session.expire_all()
    fila = await db_session.get(Quotation, quotation_id)
    assert fila is not None
    assert fila.created_by_name == "Jesus Garamendi"
    assert fila.confirmed_by_name == "Jesus Garamendi"

    papel_despues = await api.get(f"{QUOTATIONS}/{quotation_id}/pdf", headers=head(admin_csrf))
    texto = _texto_pdf(papel_despues.content)
    assert _contiene(texto, "Jesus Garamendi")
    assert not _contiene(texto, "Garamendi Gonzales"), texto


@pytest.mark.asyncio
async def test_el_pdf_ensena_quien_preparo_el_documento(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    profiles_fake: FakeProfileRepository,
) -> None:
    """CTZ_PDF_ACTOR_VISIBLE: PASS. Y sin identificadores internos."""
    _renombrar(profiles_fake, TEST_USER_ID, "Ana Perez")
    datos = await escenario(api, admin_csrf, db_session, suffix="_pdfactor")
    await confirmar(api, admin_csrf, datos["quotation"])

    papel = await api.get(f"{QUOTATIONS}/{datos['quotation']['id']}/pdf", headers=head(admin_csrf))
    texto = _texto_pdf(papel.content)
    assert _contiene(texto, "Preparado por")
    assert _contiene(texto, "Ana Perez")
    # PDF_ACTOR_INTERNAL_ID_LEAK: 0
    assert str(TEST_USER_ID) not in texto
    assert "ADMIN" not in texto


@pytest.mark.asyncio
async def test_una_cotizacion_historica_sin_actor_dice_que_no_consta(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """El hueco honesto. Lo anterior a esta fase no registro a nadie.

    Se vacian las columnas a mano para reproducir exactamente una fila vieja:
    ni la API ni el PDF pueden caerse, y ninguno de los dos puede rellenar el
    nombre con quien esta mirando.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_historica")
    await confirmar(api, admin_csrf, datos["quotation"])
    quotation_id = datos["quotation"]["id"]

    fila = await db_session.get(Quotation, quotation_id)
    assert fila is not None
    fila.created_by_name = None
    fila.confirmed_by_name = None
    await db_session.commit()

    lectura = await api.get(f"{BUILDER}/{quotation_id}", headers=head(admin_csrf))
    assert lectura.status_code == 200, lectura.text
    assert lectura.json()["created_by_name"] is None
    assert lectura.json()["confirmed_by_name"] is None

    papel = await api.get(f"{QUOTATIONS}/{quotation_id}/pdf", headers=head(admin_csrf))
    assert papel.status_code == 200, papel.text
    assert _contiene(_texto_pdf(papel.content), ACTOR_NO_REGISTRADO)


@pytest.mark.asyncio
async def test_el_navegador_no_puede_decir_quien_firmo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    profiles_fake: FakeProfileRepository,
) -> None:
    """DOCUMENT_ACTOR_BACKEND_AUTHORITY: PASS.

    El actor lo pone la sesion, no el cuerpo de la peticion. Mandarlo tiene que
    ser inocuo —o rechazado—, nunca obedecido: si no, cualquiera podria firmar
    con el nombre de otro.
    """
    _renombrar(profiles_fake, TEST_USER_ID, "Ana Perez")
    datos = await escenario(api, admin_csrf, db_session, suffix="_suplantar")
    quotation_id = datos["quotation"]["id"]

    respuesta = await api.post(
        f"{BUILDER}/{quotation_id}/confirm",
        json={
            "expected_updated_at": datos["quotation"]["updated_at"],
            "confirmed_by_name": "El Duque de Osuna",
            "created_by_name": "El Duque de Osuna",
        },
        headers=head(admin_csrf),
    )
    # Da igual si lo rechaza o lo ignora: lo que no puede es hacerle caso.
    db_session.expire_all()
    fila = await db_session.get(Quotation, quotation_id)
    assert fila is not None
    assert fila.created_by_name == "Ana Perez"
    assert fila.confirmed_by_name in (None, "Ana Perez"), respuesta.text


@pytest.mark.asyncio
async def test_la_respuesta_de_cotizacion_no_publica_el_identificador_del_actor(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
) -> None:
    """QUOTATION_INTERNAL_ACTOR_UUID_EXPOSED: NO.

    La pantalla ensena a una persona; para eso no hace falta publicar la clave
    con la que el sistema la busca.
    """
    datos = await escenario(api, admin_csrf, db_session, suffix="_sinuuid")
    await confirmar(api, admin_csrf, datos["quotation"])

    crudo = (await api.get(f"{BUILDER}/{datos['quotation']['id']}", headers=head(admin_csrf))).text
    assert str(TEST_USER_ID) not in crudo
    assert "created_by_id" not in crudo
    assert "confirmed_by_id" not in crudo


# ---------------------------------------------------------------------------
# Cotizacion de prototipo (CPR)
# ---------------------------------------------------------------------------
async def _cpr_confirmada(
    api: httpx.AsyncClient, csrf: str, db_session: AsyncSession, sufijo: str
) -> dict[str, Any]:
    caso = await _caso_referencia(api, csrf, db_session, sufijo)
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(csrf))
    assert creada.status_code == 201, creada.text
    confirmada = await api.post(f"{COTIZADOR}/{creada.json()['id']}/confirm", headers=head(csrf))
    assert confirmada.status_code == 200, confirmada.text
    return dict(confirmada.json())


@pytest.mark.asyncio
async def test_el_cpr_guarda_a_quien_lo_escribe_y_a_quien_lo_emite(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    profiles_fake: FakeProfileRepository,
) -> None:
    """CPR_CREATED_BY_NAME_SNAPSHOT y CPR_CONFIRMED_BY_NAME_...: PASS."""
    _renombrar(profiles_fake, TEST_USER_ID, "Ana Perez")
    caso = await _caso_referencia(api, admin_csrf, db_session, "_cpractor")
    creada = await api.post(COTIZADOR, json=_payload(caso), headers=head(admin_csrf))
    assert creada.status_code == 201, creada.text

    db_session.expire_all()
    borrador = await db_session.get(PrototypeQuotation, creada.json()["id"])
    assert borrador is not None
    assert borrador.created_by_name == "Ana Perez"
    assert borrador.confirmed_by is None

    profiles_fake.profiles[OPERATOR_ID].display_name = "Beto Ruiz"
    profiles_fake.profiles[OPERATOR_ID].role = UserRole.ADMIN
    csrf_beto = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
    emitida = await api.post(f"{COTIZADOR}/{creada.json()['id']}/confirm", headers=head(csrf_beto))
    assert emitida.status_code == 200, emitida.text

    db_session.expire_all()
    fila = await db_session.get(PrototypeQuotation, creada.json()["id"])
    assert fila is not None
    assert fila.created_by_name == "Ana Perez"
    assert fila.confirmed_by == OPERATOR_ID
    assert fila.confirmed_by_name == "Beto Ruiz"
    assert emitida.json()["created_by_name"] == "Ana Perez"
    assert emitida.json()["confirmed_by_name"] == "Beto Ruiz"


@pytest.mark.asyncio
async def test_el_pdf_del_cpr_ensena_al_actor_y_no_cambia_al_renombrarlo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    profiles_fake: FakeProfileRepository,
) -> None:
    """CPR_PDF_ACTOR_VISIBLE y PDF_ACTOR_SNAPSHOT_IMMUTABILITY: PASS."""
    _renombrar(profiles_fake, TEST_USER_ID, "Jesus Garamendi")
    documento = await _cpr_confirmada(api, admin_csrf, db_session, "_cprpdf")

    papel = await api.get(f"{COTIZADOR}/{documento['id']}/pdf", headers=head(admin_csrf))
    assert papel.status_code == 200, papel.text
    texto = _texto_pdf(papel.content)
    assert _contiene(texto, "Preparado por")
    assert _contiene(texto, "Jesus Garamendi")
    assert str(TEST_USER_ID) not in texto

    _renombrar(profiles_fake, TEST_USER_ID, "Jesus A. Garamendi Gonzales")
    otra_vez = await api.get(f"{COTIZADOR}/{documento['id']}/pdf", headers=head(admin_csrf))
    texto2 = _texto_pdf(otra_vez.content)
    assert _contiene(texto2, "Jesus Garamendi")
    assert not _contiene(texto2, "Garamendi Gonzales"), texto2


@pytest.mark.asyncio
async def test_el_cpr_no_publica_el_identificador_del_actor(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Mismo criterio que la CTZ: nombres si, claves internas no."""
    documento = await _cpr_confirmada(api, admin_csrf, db_session, "_cprsinuuid")
    crudo = (await api.get(f"{COTIZADOR}/{documento['id']}", headers=head(admin_csrf))).text
    assert str(TEST_USER_ID) not in crudo
    assert "created_by_id" not in crudo
    assert "confirmed_by" not in crudo.replace("confirmed_by_name", "")


@pytest.mark.asyncio
async def test_un_cpr_historico_sin_actor_no_rompe_el_pdf(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Los CPR emitidos antes de esta fase no registraron quien los emitio."""
    documento = await _cpr_confirmada(api, admin_csrf, db_session, "_cprhist")
    fila = await db_session.get(PrototypeQuotation, documento["id"])
    assert fila is not None
    fila.created_by_name = None
    fila.confirmed_by_name = None
    await db_session.commit()

    papel = await api.get(f"{COTIZADOR}/{documento['id']}/pdf", headers=head(admin_csrf))
    assert papel.status_code == 200, papel.text
    assert _contiene(_texto_pdf(papel.content), ACTOR_NO_REGISTRADO)


@pytest.mark.asyncio
async def test_emitir_no_cambia_ni_el_precio_ni_el_pago_ni_la_muestra(
    api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
) -> None:
    """Regresion de 009K.1.1: esta fase solo anade identidad.

    Se comprueba el caso de referencia entero —450 / 81 / 531 y 6 dias— porque
    tocar el confirm del CPR es tocar el sitio donde se congela el dinero.
    """
    documento = await _cpr_confirmada(api, admin_csrf, db_session, "_regresion")
    costeo = documento["costing"]
    assert costeo["commercial_net_total"] == "450.00"
    assert costeo["commercial_tax_total"] == "81.00"
    assert costeo["commercial_gross_total"] == "531.00"
    assert documento["payment_status"] == "UNPAID"
    assert documento["prototype_id"] is None

    db_session.expire_all()
    muestras = (await db_session.execute(select(PrototypeQuotation))).scalars().all()
    assert len(muestras) == 1
