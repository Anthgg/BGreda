"""Administracion de usuarios (Fase 009K.2).

Un usuario vive en dos sistemas: la cuenta en Supabase Auth y el perfil en
`profiles`. Este servicio los mantiene alineados y, sobre todo, se ocupa del
caso que da miedo: **Supabase y PostgreSQL no comparten transaccion**. Si la
cuenta nace y el perfil no, queda una cuenta que puede autenticarse contra un
sistema que no la conoce. Por eso el alta compensa.

Lo que este servicio NO hace, a proposito:

* no duplica el correo en `profiles` —su autoridad es Supabase—;
* no borra usuarios: desactivar es la baja, y el historial sigue leyendose;
* no repara inconsistencias por su cuenta. Si un perfil no tiene cuenta, se
  ensena asi. Fabricar la cuenta que falta seria inventar una identidad.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.models.audit import AuditAction
from app.models.profile import Profile, UserRole
from app.schemas.auth import AuthenticatedUser
from app.schemas.users import UserCreateIn, UserOut, UserPage, UserUpdateIn
from app.services.audit import AuditRecorder
from app.services.supabase_auth import SupabaseAuthClient

logger = logging.getLogger(__name__)

ENTITY_USER = "user"


def _rol(perfil: Profile) -> UserRole:
    """El rol como enumeracion, venga como venga.

    `Profile.role` esta anotado `Mapped[UserRole]` pero su columna es
    `String(20)`, asi que SQLAlchemy lo devuelve como texto plano. Comparar
    con `is` contra el enum fallaria en ejecucion aunque el tipado diga que no.
    Se normaliza aqui, una vez, en vez de recordarlo en cada comparacion.
    """
    return UserRole(perfil.role)


class UserNotFoundError(APIError):
    status_code = 404
    code = "USER_NOT_FOUND"
    message = "El usuario no existe"


class LastActiveAdminError(APIError):
    """Quedarse sin administradores cierra la puerta por dentro."""

    status_code = 409
    code = "LAST_ACTIVE_ADMIN"
    message = "No se puede dejar el sistema sin ningun administrador activo"


class UserProvisioningError(APIError):
    """El alta se quedo a medias y la compensacion tampoco pudo cerrarla."""

    status_code = 500
    code = "USER_PROVISIONING_FAILED"
    message = "No se pudo completar el alta del usuario"


class UserService:
    def __init__(
        self,
        session: AsyncSession,
        supabase: SupabaseAuthClient,
        audit: AuditRecorder,
    ) -> None:
        self._session = session
        self._supabase = supabase
        self._audit = audit

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    async def list_users(self) -> UserPage:
        """Los perfiles de la casa, con el correo que les pone Supabase.

        Se pide el listado de cuentas UNA vez y se cruza en memoria. Preguntar
        cuenta por cuenta daria el mismo resultado y una llamada de red por
        usuario: con veinte perfiles son veinte viajes para dibujar una tabla.

        Manda `profiles`, no Supabase: una cuenta sin perfil no es un usuario
        de Greda —no puede entrar— y no se lista como si lo fuera.
        """
        perfiles = list(
            (await self._session.execute(select(Profile).order_by(Profile.display_name)))
            .scalars()
            .all()
        )
        correos = {cuenta.id: cuenta.email for cuenta in await self._supabase.admin_list_users()}

        items: list[UserOut] = []
        for perfil in perfiles:
            correo = correos.get(perfil.id)
            if correo is None:
                # Un perfil sin cuenta es un estado inconsistente real. No se
                # oculta ni se arregla solo: se ensena con el correo vacio para
                # que alguien lo mire.
                logger.warning("El perfil %s no tiene cuenta en Supabase", perfil.id)
            items.append(
                UserOut(
                    id=perfil.id,
                    display_name=perfil.display_name,
                    email=correo,
                    role=_rol(perfil),
                    active=perfil.active,
                )
            )
        return UserPage(items=items, total=len(items))

    async def _get_profile(self, user_id: uuid.UUID) -> Profile:
        perfil = await self._session.get(Profile, user_id)
        if perfil is None:
            raise UserNotFoundError()
        return perfil

    async def _present(self, perfil: Profile) -> UserOut:
        cuenta = await self._supabase.admin_get_user(perfil.id)
        return UserOut(
            id=perfil.id,
            display_name=perfil.display_name,
            email=cuenta.email if cuenta else None,
            role=_rol(perfil),
            active=perfil.active,
        )

    # ------------------------------------------------------------------
    # Alta
    # ------------------------------------------------------------------
    async def create(self, payload: UserCreateIn, *, user: AuthenticatedUser) -> UserOut:
        """Crea la cuenta y el perfil, o no deja ninguna de las dos.

        El orden importa: primero Supabase, porque es el unico paso que este
        backend no puede deshacer con un ROLLBACK. Si despues falla la parte
        local, se borra la cuenta recien creada. Ese `admin_delete_user` es una
        COMPENSACION de un alta que nunca llego a existir, no una baja: los
        usuarios de verdad se desactivan y siguen apareciendo en el historial.
        """
        cuenta = await self._supabase.admin_create_user(payload.email, payload.password)

        try:
            perfil = Profile(
                id=cuenta.id,
                display_name=payload.display_name,
                role=payload.role,
                active=True,
            )
            self._session.add(perfil)
            self._audit.record_action(
                entity_type=ENTITY_USER,
                entity_id=str(cuenta.id),
                action=AuditAction.CREATE,
                user_id=user.id,
                user_display_name=user.display_name,
                metadata={"display_name": payload.display_name, "role": payload.role.value},
            )
            await self._session.flush()
        except Exception:
            await self._session.rollback()
            await self._compensar(cuenta.id)
            raise

        return UserOut(
            id=perfil.id,
            display_name=perfil.display_name,
            email=cuenta.email,
            role=_rol(perfil),
            active=perfil.active,
        )

    async def _compensar(self, auth_user_id: uuid.UUID) -> None:
        """Retira la cuenta que quedo huerfana.

        Si esto tambien falla no se puede disimular: quedaria una cuenta capaz
        de autenticarse contra un sistema que no la reconoce. Se registra con
        el identificador —no es un secreto, y sin el nadie podria encontrarla—
        y se devuelve un error que dice que el alta no se completo.
        """
        try:
            await self._supabase.admin_delete_user(auth_user_id)
        except Exception:
            logger.critical(
                "Alta incompleta: la cuenta de Supabase %s quedo sin perfil y no se pudo "
                "retirar. Requiere intervencion manual.",
                auth_user_id,
            )
            raise UserProvisioningError() from None

    # ------------------------------------------------------------------
    # Edicion
    # ------------------------------------------------------------------
    async def update(
        self, user_id: uuid.UUID, payload: UserUpdateIn, *, user: AuthenticatedUser
    ) -> UserOut:
        """Cambia nombre visible y/o rol. No toca Supabase.

        Cambiar como se llama alguien en la casa no es cambiar su cuenta: el
        correo y la contrasena siguen siendo de Supabase y aqui no se rozan.
        """
        perfil = await self._get_profile(user_id)

        cambios: dict[str, tuple[object, object]] = {}
        if payload.display_name is not None and payload.display_name != perfil.display_name:
            cambios["display_name"] = (perfil.display_name, payload.display_name)
            perfil.display_name = payload.display_name
        if payload.role is not None and payload.role is not _rol(perfil):
            if _rol(perfil) is UserRole.ADMIN and payload.role is not UserRole.ADMIN:
                await self._exigir_otro_admin(perfil)
            cambios["role"] = (_rol(perfil).value, payload.role.value)
            perfil.role = payload.role

        if cambios:
            self._audit.record_changes(
                entity_type=ENTITY_USER,
                entity_id=str(perfil.id),
                changes=cambios,
                user_id=user.id,
                user_display_name=user.display_name,
            )
        await self._session.flush()
        return await self._present(perfil)

    async def set_active(
        self, user_id: uuid.UUID, *, active: bool, user: AuthenticatedUser
    ) -> UserOut:
        """Da de baja o vuelve a dar de alta.

        No se borra nada. `app/api/deps.py` ya rechaza cada peticion de un
        perfil inactivo, asi que desactivar basta para cerrar la puerta, y los
        documentos que esa persona firmo siguen diciendo su nombre porque lo
        llevan copiado, no prestado.
        """
        perfil = await self._get_profile(user_id)
        if perfil.active and not active and _rol(perfil) is UserRole.ADMIN:
            await self._exigir_otro_admin(perfil)
        if perfil.active != active:
            self._audit.record_changes(
                entity_type=ENTITY_USER,
                entity_id=str(perfil.id),
                changes={"active": (perfil.active, active)},
                user_id=user.id,
                user_display_name=user.display_name,
            )
            perfil.active = active
        await self._session.flush()
        return await self._present(perfil)

    async def _exigir_otro_admin(self, perfil: Profile) -> None:
        """Impide el movimiento que deja la casa sin administracion.

        No es una regla inventada por gusto: administrar usuarios exige ser
        ADMIN, asi que si el ultimo se degrada o se desactiva —incluido a si
        mismo, que es como suele pasar— nadie dentro del sistema puede
        deshacerlo. Se arreglaria por SQL, y eso ya no es la aplicacion.
        """
        if await self.contar_admins_activos(excepto=perfil.id) == 0:
            raise LastActiveAdminError()

    async def contar_admins_activos(self, *, excepto: uuid.UUID | None = None) -> int:
        """Cuantos administradores activos quedarian sin contar a `excepto`."""
        consulta = (
            select(func.count())
            .select_from(Profile)
            .where(Profile.role == UserRole.ADMIN, Profile.active.is_(True))
        )
        if excepto is not None:
            consulta = consulta.where(Profile.id != excepto)
        return int(await self._session.scalar(consulta) or 0)
