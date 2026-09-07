"""Fase 009K.2 — administracion de usuarios.

Un usuario de Greda vive partido en dos: la CUENTA en Supabase Auth y el
PERFIL en `profiles`. Casi todo lo que puede salir mal aqui sale de esa
juntura, y en particular de que **no comparten transaccion**. Un alta que crea
la cuenta y no el perfil deja algo peor que un error: una credencial valida
contra un sistema que no reconoce a su dueno.

Por eso la mitad de este archivo mira lo que NO debe quedar. La otra mitad
mira lo que no debe perderse: desactivar a alguien le cierra la puerta, no le
borra de los documentos que firmo.

Nota sobre el montaje: la autenticacion de las pruebas usa un repositorio de
perfiles en memoria (`FakeProfileRepository`), mientras que este servicio lee y
escribe la tabla `profiles` de verdad. Son dos cosas distintas a proposito, y
por eso cada prueba siembra la tabla con lo que necesita.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.errors import AuthAccountInactiveError
from app.models.audit import AuditEvent
from app.models.profile import Profile, UserRole
from tests.db.conftest import OPERATOR_ID, TEST_USER_ID
from tests.db.test_quotation_builder_api import head
from tests.fakes import FakeProfileRepository, FakeSupabaseAuthClient

USERS = "/api/v1/users"


async def _sembrar(
    db_session: AsyncSession,
    supabase: FakeSupabaseAuthClient,
    *,
    user_id: uuid.UUID,
    nombre: str,
    correo: str,
    rol: UserRole = UserRole.ADMIN,
    activo: bool = True,
) -> uuid.UUID:
    """Un usuario completo: cuenta en Supabase y perfil en la tabla real.

    Devuelve el IDENTIFICADOR y no la fila: las pruebas llaman a `expire_all()`
    para releer de la base, y tocar un atributo de una fila expirada desde
    codigo sincrono dispara una recarga que en sesion asincrona revienta.
    """
    supabase.register(email=correo, password="da-igual-1234", user_id=user_id)
    perfil = Profile(id=user_id, display_name=nombre, role=rol, active=activo)
    db_session.add(perfil)
    await db_session.commit()
    return user_id


async def _admin_sembrado(db_session: AsyncSession, supabase: FakeSupabaseAuthClient) -> uuid.UUID:
    """El administrador que autentica, tambien en la tabla real.

    Hace falta sembrarlo: sin ningun ADMIN activo en `profiles`, la guardia del
    ultimo administrador bloquearia operaciones que la prueba quiere observar.
    """
    return await _sembrar(
        db_session,
        supabase,
        user_id=TEST_USER_ID,
        nombre="Administrador",
        correo="admin-009k2@greda-test.com",
        rol=UserRole.ADMIN,
    )


async def _perfiles(db_session: AsyncSession) -> list[Profile]:
    db_session.expire_all()
    return list((await db_session.execute(select(Profile))).scalars().all())


# ---------------------------------------------------------------------------
# Lectura y permisos
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_un_administrador_ve_la_lista_con_el_correo_de_supabase(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """USER_LIST: PASS.

    El correo NO esta en `profiles` y no se duplica alli: se pide a Supabase,
    que es su unica autoridad, y se cruza por identificador.
    """
    await _admin_sembrado(db_session, supabase_fake)
    await _sembrar(
        db_session,
        supabase_fake,
        user_id=uuid.uuid4(),
        nombre="Jesus Garamendi",
        correo="jesus@greda-test.com",
        rol=UserRole.OPERATOR,
    )

    respuesta = await api.get(USERS, headers=head(admin_csrf))
    assert respuesta.status_code == 200, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["total"] == 2
    por_nombre = {u["display_name"]: u for u in cuerpo["items"]}
    assert por_nombre["Jesus Garamendi"]["email"] == "jesus@greda-test.com"
    assert por_nombre["Jesus Garamendi"]["role"] == "OPERATOR"
    assert por_nombre["Jesus Garamendi"]["active"] is True


@pytest.mark.asyncio
async def test_la_lista_no_devuelve_ni_contrasenas_ni_tokens(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """USER_API_SENSITIVE_FIELD_LEAK: 0.

    Se mira el JSON entero como texto, no campo a campo: una fuga aparece
    justamente en la clave que a nadie se le ocurrio enumerar.
    """
    await _admin_sembrado(db_session, supabase_fake)
    crudo = (await api.get(USERS, headers=head(admin_csrf))).text.lower()
    for prohibido in ("password", "token", "secret", "hash", "provider", "service_role"):
        assert prohibido not in crudo, prohibido


@pytest.mark.asyncio
async def test_un_perfil_sin_cuenta_se_ensena_sin_correo_en_vez_de_esconderse(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """Un estado inconsistente se ve; no se repara solo ni se oculta.

    Fabricar la cuenta que falta seria inventar una identidad, y ocultarlo
    dejaria a alguien preguntandose por que no aparece en la lista.
    """
    await _admin_sembrado(db_session, supabase_fake)
    huerfano = Profile(
        id=uuid.uuid4(), display_name="Perfil sin cuenta", role=UserRole.OPERATOR, active=True
    )
    db_session.add(huerfano)
    await db_session.commit()

    cuerpo = (await api.get(USERS, headers=head(admin_csrf))).json()
    fila = next(u for u in cuerpo["items"] if u["display_name"] == "Perfil sin cuenta")
    assert fila["email"] is None


@pytest.mark.asyncio
async def test_una_cuenta_de_supabase_sin_perfil_no_es_un_usuario_de_greda(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """Existir en Supabase no habilita: manda `profiles`.

    Listarla como usuario activo seria decir que puede entrar, y no puede.
    """
    await _admin_sembrado(db_session, supabase_fake)
    supabase_fake.register(email="ajeno@otra-app.com", password="x", user_id=uuid.uuid4())

    cuerpo = (await api.get(USERS, headers=head(admin_csrf))).json()
    assert all(u["email"] != "ajeno@otra-app.com" for u in cuerpo["items"])


@pytest.mark.asyncio
async def test_un_operario_no_administra_usuarios(
    api: httpx.AsyncClient,
    operator_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """USER_MANAGEMENT_RBAC: PASS. Y la base no se mueve tras el 403."""
    antes = len(await _perfiles(db_session))

    assert (await api.get(USERS, headers=head(operator_csrf))).status_code == 403
    creado = await api.post(
        USERS,
        json={
            "email": "colado@greda-test.com",
            "display_name": "Colado",
            "role": "ADMIN",
            "password": "contrasena-larga",
        },
        headers=head(operator_csrf),
    )
    assert creado.status_code == 403

    assert len(await _perfiles(db_session)) == antes
    assert supabase_fake.admin_created == []


# ---------------------------------------------------------------------------
# Alta
# ---------------------------------------------------------------------------
def _alta(**extra: Any) -> dict[str, Any]:
    return {
        "email": "nueva@greda-test.com",
        "display_name": "Ana Perez",
        "role": "OPERATOR",
        "password": "contrasena-larga",
    } | extra


@pytest.mark.asyncio
async def test_dar_de_alta_crea_la_cuenta_y_el_perfil(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """USER_CREATE: PASS. Las dos mitades, con el mismo identificador."""
    await _admin_sembrado(db_session, supabase_fake)

    respuesta = await api.post(USERS, json=_alta(), headers=head(admin_csrf))
    assert respuesta.status_code == 201, respuesta.text
    cuerpo = respuesta.json()
    assert cuerpo["display_name"] == "Ana Perez"
    assert cuerpo["email"] == "nueva@greda-test.com"
    assert cuerpo["role"] == "OPERATOR"
    assert cuerpo["active"] is True

    nuevo_id = uuid.UUID(cuerpo["id"])
    assert nuevo_id in supabase_fake.admin_created
    db_session.expire_all()
    perfil = await db_session.get(Profile, nuevo_id)
    assert perfil is not None
    assert perfil.display_name == "Ana Perez"
    assert UserRole(perfil.role) is UserRole.OPERATOR
    assert perfil.active is True


@pytest.mark.asyncio
async def test_el_alta_no_devuelve_la_contrasena(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """Viaja de ida y no vuelve. Tampoco se guarda en `profiles`."""
    await _admin_sembrado(db_session, supabase_fake)
    respuesta = await api.post(
        USERS, json=_alta(password="secreto-larguisimo"), headers=head(admin_csrf)
    )
    assert respuesta.status_code == 201
    assert "secreto-larguisimo" not in respuesta.text
    assert "password" not in respuesta.text.lower()


@pytest.mark.asyncio
async def test_un_correo_repetido_se_rechaza_sin_crear_perfil(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """FAILED_USER_CREATE_PROFILE_ORPHAN: 0, por el lado de Supabase."""
    await _admin_sembrado(db_session, supabase_fake)
    supabase_fake.register(email="nueva@greda-test.com", password="x", user_id=uuid.uuid4())
    antes = len(await _perfiles(db_session))

    respuesta = await api.post(USERS, json=_alta(), headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "USER_EMAIL_ALREADY_EXISTS"
    assert len(await _perfiles(db_session)) == antes


@pytest.mark.asyncio
async def test_si_falla_la_parte_local_se_retira_la_cuenta_recien_creada(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """USER_CREATE_AUTH_PROFILE_COMPENSATION: PASS.

    Es el caso caro: la cuenta ya existe en un sistema que este backend no
    puede deshacer con un ROLLBACK. Se provoca el fallo local reusando un
    identificador que ya esta en `profiles`, de modo que la insercion viole la
    clave primaria DESPUES de que la cuenta haya nacido.
    """
    await _admin_sembrado(db_session, supabase_fake)
    chocado = uuid.uuid4()
    db_session.add(
        Profile(id=chocado, display_name="Ya existia", role=UserRole.OPERATOR, active=True)
    )
    await db_session.commit()

    # El doble entrega justo ese identificador a la cuenta nueva.
    original = supabase_fake.admin_create_user

    async def crear_con_id_chocado(email: str, password: str) -> Any:
        await original(email, password)
        from app.services.supabase_auth import SupabaseUser

        supabase_fake.admin_created[-1] = chocado
        return SupabaseUser(id=chocado, email=email)

    supabase_fake.admin_create_user = crear_con_id_chocado  # type: ignore[method-assign]

    antes = len(await _perfiles(db_session))
    # El fallo local es un error de integridad, no un error de dominio: segun
    # donde lo atrape la aplicacion sale como 500 o sube hasta aqui. Lo que se
    # prueba no es su forma, sino que no quede nada suelto en ninguno de los
    # dos sistemas.
    try:
        respuesta = await api.post(USERS, json=_alta(), headers=head(admin_csrf))
        assert respuesta.status_code >= 400, respuesta.text
    except IntegrityError:
        pass

    # FAILED_USER_CREATE_PROFILE_ORPHAN: 0 — no quedo perfil de mas.
    assert len(await _perfiles(db_session)) == antes
    # FAILED_USER_CREATE_AUTH_ORPHAN: 0 — la cuenta se retiro.
    assert chocado in supabase_fake.admin_deleted


@pytest.mark.asyncio
async def test_si_la_compensacion_tambien_falla_se_dice_en_vez_de_callarlo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """Quedaria una credencial huerfana. Eso no se disimula con un 500 mudo."""
    await _admin_sembrado(db_session, supabase_fake)
    chocado = uuid.uuid4()
    db_session.add(
        Profile(id=chocado, display_name="Ya existia", role=UserRole.OPERATOR, active=True)
    )
    await db_session.commit()

    original = supabase_fake.admin_create_user

    async def crear_con_id_chocado(email: str, password: str) -> Any:
        await original(email, password)
        from app.services.supabase_auth import SupabaseUser

        return SupabaseUser(id=chocado, email=email)

    supabase_fake.admin_create_user = crear_con_id_chocado  # type: ignore[method-assign]
    supabase_fake.fallar_en("admin_delete_user")

    respuesta = await api.post(USERS, json=_alta(), headers=head(admin_csrf))
    assert respuesta.status_code == 500, respuesta.text
    assert respuesta.json()["error"]["code"] == "USER_PROVISIONING_FAILED"


@pytest.mark.asyncio
async def test_un_nombre_visible_en_blanco_se_rechaza(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """Y se rechaza ANTES de crear la cuenta: un 422 no deja rastro."""
    await _admin_sembrado(db_session, supabase_fake)
    respuesta = await api.post(USERS, json=_alta(display_name="   "), headers=head(admin_csrf))
    assert respuesta.status_code == 422, respuesta.text
    assert supabase_fake.admin_created == []


# ---------------------------------------------------------------------------
# Edicion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_editar_el_nombre_visible_y_el_rol(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """USER_UPDATE_DISPLAY_NAME y USER_ROLE_UPDATE: PASS."""
    await _admin_sembrado(db_session, supabase_fake)
    otro = uuid.uuid4()
    await _sembrar(
        db_session,
        supabase_fake,
        user_id=otro,
        nombre="Jesus Garamendi",
        correo="jesus@greda-test.com",
        rol=UserRole.OPERATOR,
    )

    respuesta = await api.put(
        f"{USERS}/{otro}",
        json={"display_name": "Jesus A. Garamendi Gonzales", "role": "ADMIN"},
        headers=head(admin_csrf),
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["display_name"] == "Jesus A. Garamendi Gonzales"
    assert respuesta.json()["role"] == "ADMIN"

    db_session.expire_all()
    perfil = await db_session.get(Profile, otro)
    assert perfil is not None
    assert perfil.display_name == "Jesus A. Garamendi Gonzales"
    assert UserRole(perfil.role) is UserRole.ADMIN


@pytest.mark.asyncio
async def test_cambiar_solo_el_nombre_no_toca_la_cuenta_de_supabase(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """Como se llama alguien en la casa no es su credencial."""
    await _admin_sembrado(db_session, supabase_fake)
    otro = uuid.uuid4()
    await _sembrar(
        db_session,
        supabase_fake,
        user_id=otro,
        nombre="Nombre viejo",
        correo="otro@greda-test.com",
        rol=UserRole.OPERATOR,
    )

    await api.put(
        f"{USERS}/{otro}", json={"display_name": "Nombre nuevo"}, headers=head(admin_csrf)
    )
    assert supabase_fake.admin_created == []
    assert supabase_fake.admin_deleted == []
    cuenta = await supabase_fake.admin_get_user(otro)
    assert cuenta is not None
    assert cuenta.email == "otro@greda-test.com"


@pytest.mark.asyncio
async def test_editar_un_usuario_que_no_existe_da_404(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    await _admin_sembrado(db_session, supabase_fake)
    respuesta = await api.put(
        f"{USERS}/{uuid.uuid4()}", json={"display_name": "Nadie"}, headers=head(admin_csrf)
    )
    assert respuesta.status_code == 404


# ---------------------------------------------------------------------------
# Baja y alta de nuevo
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_desactivar_y_reactivar(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """USER_DISABLE y USER_REENABLE: PASS. Y la cuenta NO se borra."""
    await _admin_sembrado(db_session, supabase_fake)
    otro = uuid.uuid4()
    await _sembrar(
        db_session,
        supabase_fake,
        user_id=otro,
        nombre="Se va",
        correo="seva@greda-test.com",
        rol=UserRole.OPERATOR,
    )

    baja = await api.post(f"{USERS}/{otro}/disable", headers=head(admin_csrf))
    assert baja.status_code == 200, baja.text
    assert baja.json()["active"] is False
    assert supabase_fake.admin_deleted == []

    alta = await api.post(f"{USERS}/{otro}/enable", headers=head(admin_csrf))
    assert alta.status_code == 200, alta.text
    assert alta.json()["active"] is True


@pytest.mark.asyncio
async def test_un_perfil_desactivado_deja_de_tener_acceso(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """La baja de la pantalla y la guardia de `deps.py` son la misma cosa.

    Se pasa la fila REAL por el guardia REAL: sin esto, la prueba diria que se
    escribio `active = false` y no que eso cierre ninguna puerta.
    """
    await _admin_sembrado(db_session, supabase_fake)
    otro = uuid.uuid4()
    await _sembrar(
        db_session,
        supabase_fake,
        user_id=otro,
        nombre="Se va",
        correo="seva@greda-test.com",
        rol=UserRole.OPERATOR,
    )
    await api.post(f"{USERS}/{otro}/disable", headers=head(admin_csrf))

    db_session.expire_all()
    perfil = await db_session.get(Profile, otro)
    assert perfil is not None
    with pytest.raises(AuthAccountInactiveError):
        await deps.resolve_profile(
            otro, "seva@greda-test.com", FakeProfileRepository({otro: perfil})
        )


@pytest.mark.asyncio
async def test_desactivar_al_ultimo_administrador_se_impide(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """LAST_ACTIVE_ADMIN.

    Administrar usuarios exige ser ADMIN, asi que si el ultimo se desactiva
    nadie dentro del sistema puede deshacerlo. Se arreglaria por SQL, y eso ya
    no es la aplicacion.
    """
    admin_id = await _admin_sembrado(db_session, supabase_fake)
    await _sembrar(
        db_session,
        supabase_fake,
        user_id=uuid.uuid4(),
        nombre="Operario",
        correo="op@greda-test.com",
        rol=UserRole.OPERATOR,
    )

    respuesta = await api.post(f"{USERS}/{admin_id}/disable", headers=head(admin_csrf))
    assert respuesta.status_code == 409, respuesta.text
    assert respuesta.json()["error"]["code"] == "LAST_ACTIVE_ADMIN"

    db_session.expire_all()
    vigente = await db_session.get(Profile, admin_id)
    assert vigente is not None
    assert vigente.active is True


@pytest.mark.asyncio
async def test_degradar_al_ultimo_administrador_tambien_se_impide(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """La otra forma de cerrarse la puerta: quitarse el rol en vez de la cuenta."""
    admin_id = await _admin_sembrado(db_session, supabase_fake)
    respuesta = await api.put(
        f"{USERS}/{admin_id}", json={"role": "OPERATOR"}, headers=head(admin_csrf)
    )
    assert respuesta.status_code == 409
    db_session.expire_all()
    vigente = await db_session.get(Profile, admin_id)
    assert vigente is not None
    assert UserRole(vigente.role) is UserRole.ADMIN


@pytest.mark.asyncio
async def test_con_otro_administrador_activo_si_se_puede_degradar(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """La guardia protege el ultimo, no cualquier cambio de rol."""
    admin_id = await _admin_sembrado(db_session, supabase_fake)
    await _sembrar(
        db_session,
        supabase_fake,
        user_id=uuid.uuid4(),
        nombre="Otra administradora",
        correo="admin2@greda-test.com",
        rol=UserRole.ADMIN,
    )

    respuesta = await api.put(
        f"{USERS}/{admin_id}", json={"role": "OPERATOR"}, headers=head(admin_csrf)
    )
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["role"] == "OPERATOR"


# ---------------------------------------------------------------------------
# Auditoria
# ---------------------------------------------------------------------------
async def _eventos_de_usuario(db_session: AsyncSession) -> list[AuditEvent]:
    db_session.expire_all()
    return list(
        (await db_session.execute(select(AuditEvent).where(AuditEvent.entity_type == "user")))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_el_alta_queda_auditada_con_quien_la_hizo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """AUDIT_ACTOR_MATCHES_AUTH_USER: PASS."""
    await _admin_sembrado(db_session, supabase_fake)
    await api.post(USERS, json=_alta(), headers=head(admin_csrf))

    eventos = await _eventos_de_usuario(db_session)
    assert len(eventos) == 1
    assert eventos[0].user_id == TEST_USER_ID
    assert eventos[0].user_display_name == "Administrador"


@pytest.mark.asyncio
async def test_cambiar_rol_y_estado_queda_auditado_campo_a_campo(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    await _admin_sembrado(db_session, supabase_fake)
    otro = uuid.uuid4()
    await _sembrar(
        db_session,
        supabase_fake,
        user_id=otro,
        nombre="Antes",
        correo="otro@greda-test.com",
        rol=UserRole.OPERATOR,
    )

    await api.put(
        f"{USERS}/{otro}",
        json={"display_name": "Despues", "role": "ADMIN"},
        headers=head(admin_csrf),
    )
    await api.post(f"{USERS}/{otro}/disable", headers=head(admin_csrf))

    campos = {e.field for e in await _eventos_de_usuario(db_session)}
    assert {"display_name", "role", "active"} <= campos


@pytest.mark.asyncio
async def test_una_operacion_rechazada_no_deja_auditoria_de_exito(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """Delta de auditoria 0 cuando el cambio no llego a ocurrir."""
    await _admin_sembrado(db_session, supabase_fake)
    antes = await db_session.scalar(select(func.count()).select_from(AuditEvent))

    rechazado = await api.put(
        f"{USERS}/{uuid.uuid4()}", json={"display_name": "Nadie"}, headers=head(admin_csrf)
    )
    assert rechazado.status_code == 404

    db_session.expire_all()
    assert await db_session.scalar(select(func.count()).select_from(AuditEvent)) == antes


@pytest.mark.asyncio
async def test_el_operario_sembrado_no_es_un_efecto_del_alta(
    api: httpx.AsyncClient,
    admin_csrf: str,
    db_session: AsyncSession,
    supabase_fake: FakeSupabaseAuthClient,
) -> None:
    """Nada de esto crea usuarios por su cuenta.

    El identificador del operario de las pruebas existe en el doble de auth
    desde el montaje; que no aparezca un perfil suyo confirma que el servicio
    no aprovisiona a nadie que no le hayan pedido.
    """
    await _admin_sembrado(db_session, supabase_fake)
    db_session.expire_all()
    assert await db_session.get(Profile, OPERATOR_ID) is None
