"""Configuracion comercial del Cotizador V2: leerla, editarla y congelarla.

Fase 010B. Este servicio hace tres cosas y conviene distinguirlas:

1. **lee** la configuracion efectiva, que es la mezcla de lo propio de V2 y lo
   que sigue siendo politica canonica de la casa (IGV, moneda, simbolo);
2. **edita** lo propio de V2, con control de concurrencia optimista;
3. **congela** —`capture_snapshot`— los valores con los que nace una
   cotizacion.

El punto 3 es el que justifica la fase. Una cotizacion no guarda una FK a la
configuracion: guarda una COPIA. Si guardara la referencia, subir el costo del
taller reescribiria el precio de todo lo ya emitido.

## Sobre el IGV y la moneda

No hay un IGV de V2. Se lee de `commercial_settings`, que es la unica fuente
canonica del proyecto: un segundo IGV daria dos sitios donde mirar y el que
quedara desactualizado emitiria documentos incorrectos.

Leer ese valor NO acopla V2 al motor Legacy: no se invoca ninguna formula suya,
solo se consulta un numero que el negocio declara una vez. Este modulo no
importa `app.services.quotations` ni `app.services.quotation_builder`, y la
prueba de aislamiento lo comprueba.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.quoter_v2_config import BASE_CURRENCY
from app.models.audit import AuditAction
from app.models.firings import FiringType, Kiln
from app.models.quoter_v2 import V2CustomerKind, V2ProductionType
from app.models.quoter_v2_settings import V2CommercialSettings, V2KilnRate
from app.models.settings import SINGLETON_ID, CommercialSettings
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder

#: Entidad con la que se audita la configuracion V2. Propia, no la de
#: `commercial_settings`: son dos configuraciones distintas y mezclar su
#: historial haria imposible saber cual se toco.
V2_SETTINGS_ENTITY = "v2_commercial_settings"
V2_KILN_RATE_ENTITY = "v2_kiln_rate"

#: Simbolos de las monedas que el taller puede emitir. Se resuelve el simbolo
#: desde el codigo y no al reves: el codigo ISO es la autoridad semantica, el
#: simbolo es presentacion.
CURRENCY_SYMBOLS: dict[str, str] = {"PEN": "S/", "USD": "US$"}

#: Lo que vale una tarifa que nadie ha configurado todavia. Va acompanada
#: de `configured=False` para que no se confunda con un cero elegido.
ZERO = Decimal(0)


@dataclass(frozen=True)
class V2KilnRateRow:
    """Una celda de la rejilla de tarifas: horno x tipo de quema."""

    kiln_id: int
    kiln_code: str
    kiln_name: str
    firing_type: FiringType
    gas_cost: Decimal
    external_rate: Decimal
    student_rate: Decimal
    configured: bool


class V2SettingsNotFoundError(APIError):
    status_code = 404
    code = "V2_SETTINGS_NOT_FOUND"
    message = "La configuracion del Cotizador V2 no existe"


class V2SettingsConflictError(APIError):
    """La configuracion cambio entre la lectura y la escritura."""

    status_code = 409
    code = "V2_SETTINGS_VERSION_CONFLICT"
    message = "La configuracion cambio desde que se leyo. Vuelva a cargarla."


class V2KilnNotFoundError(APIError):
    status_code = 404
    code = "V2_KILN_NOT_FOUND"
    message = "El horno indicado no existe"


class V2KilnInactiveError(APIError):
    """Sugerir un horno que el taller ya no enciende."""

    status_code = 422
    code = "V2_KILN_INACTIVE"
    message = "El horno esta dado de baja y no puede sugerirse"


class V2FactorOutOfRangeError(APIError):
    """El factor pedido no cabe en el rango vigente de la configuracion."""

    status_code = 422
    code = "V2_FACTOR_OUT_OF_RANGE"
    message = "El factor comercial esta fuera del rango permitido"


class V2SettingsService:
    """Lectura y edicion de la configuracion comercial del Cotizador V2."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    async def get(self) -> V2CommercialSettings:
        fila = await self._session.get(V2CommercialSettings, SINGLETON_ID)
        if fila is None:
            raise V2SettingsNotFoundError()
        return fila

    async def commercial_policy(self) -> CommercialSettings:
        """La politica canonica de la casa: IGV, moneda y simbolo.

        Se consulta, no se copia. V2 no tiene un IGV propio.
        """
        fila = await self._session.get(CommercialSettings, SINGLETON_ID)
        if fila is None:
            raise V2SettingsNotFoundError(
                "La configuracion comercial de la empresa no esta inicializada",
                code="COMMERCIAL_SETTINGS_NOT_FOUND",
            )
        return fila

    async def kiln_rate_grid(self) -> list[V2KilnRateRow]:
        """Todos los hornos activos por baja y por alta, tengan tarifa o no.

        Devuelve la REJILLA COMPLETA y no solo las filas existentes. Las
        tarifas V2 nacen vacias a proposito —el sistema no sabe cual horno es
        el chico— y una pantalla que solo enseñara lo ya guardado no tendria
        por donde crear la primera: el taller quedaria sin forma de configurar
        sus hornos.

        Los huecos salen en cero y marcados como no configurados, para que se
        distinga «todavia nadie lo puso» de «vale cero».
        """
        hornos = list(
            (await self._session.scalars(select(Kiln).where(Kiln.active).order_by(Kiln.code))).all()
        )
        existentes = {
            (fila.kiln_id, fila.firing_type): fila
            for fila in (await self._session.scalars(select(V2KilnRate))).all()
        }

        rejilla: list[V2KilnRateRow] = []
        for horno in hornos:
            for tipo in (FiringType.LOW, FiringType.HIGH):
                fila = existentes.get((horno.id, tipo))
                rejilla.append(
                    V2KilnRateRow(
                        kiln_id=horno.id,
                        kiln_code=horno.code,
                        kiln_name=horno.name,
                        firing_type=tipo,
                        gas_cost=fila.gas_cost if fila else ZERO,
                        external_rate=fila.external_rate if fila else ZERO,
                        student_rate=fila.student_rate if fila else ZERO,
                        configured=fila is not None,
                    )
                )
        return rejilla

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------
    async def update(
        self, data: dict[str, Any], *, expected_version: int, user: AuthenticatedUser
    ) -> V2CommercialSettings:
        """Actualiza los defaults de V2.

        Concurrencia optimista, como el resto de la configuracion del
        proyecto: quien escribe declara la version que leyo. Sin esto, dos
        personas editando a la vez dejarian la ultima escritura como ganadora
        sin que la primera se enterara de que su cambio desaparecio.
        """
        # `with_for_update`: comprobar la version leyendo sin bloquear no basta.
        # Dos administradores que abran la pantalla a la vez leerian version 1
        # los dos, los dos pasarian la comprobacion, y el ultimo en confirmar
        # pisaria al primero sin que nadie viera un conflicto. El bloqueo hace
        # que la segunda transaccion espere y lea la version ya incrementada.
        fila = (
            await self._session.scalars(
                select(V2CommercialSettings)
                .where(V2CommercialSettings.id == SINGLETON_ID)
                .with_for_update()
            )
        ).one_or_none()
        if fila is None:
            raise V2SettingsNotFoundError()
        if fila.version != expected_version:
            raise V2SettingsConflictError()

        for campo in ("retail_kiln_id", "wholesale_kiln_id"):
            valor = data.get(campo)
            if valor is None:
                continue
            horno = await self._session.get(Kiln, valor)
            if horno is None:
                raise V2KilnNotFoundError()
            # Fase 010E. Tambien tiene que estar activo. Configurar aqui un
            # horno dado de baja parecia guardarse bien y despues las
            # cotizaciones nuevas nacian sin horno, sin que nada explicara por
            # que. La cotizacion ya rechaza elegir un horno inactivo; esta es
            # la misma regla en la otra superficie.
            if not horno.active:
                raise V2KilnInactiveError(f"«{horno.name}» esta dado de baja")

        # `None` significa «no lo mandes» en casi todo el contrato, pero en los
        # dos hornos sugeridos significa «quitalo»: son anulables justamente
        # para poder no tener ninguno, y sin esta excepcion, una vez elegido,
        # no habria forma de deshacerlo desde la API.
        anulables = {"retail_kiln_id", "wholesale_kiln_id"}
        cambios: dict[str, tuple[Any, Any]] = {}
        for campo, nuevo in data.items():
            if not hasattr(fila, campo):
                continue
            if nuevo is None and campo not in anulables:
                continue
            anterior = getattr(fila, campo)
            if anterior != nuevo:
                cambios[campo] = (anterior, nuevo)
                setattr(fila, campo, nuevo)

        self._validate_factor_range(fila)

        if cambios:
            fila.version += 1
            self._audit.record_changes(
                entity_type=V2_SETTINGS_ENTITY,
                entity_id=str(fila.id),
                changes=cambios,
                user_id=user.id,
                user_display_name=user.display_name,
            )
        await self._session.flush()
        return fila

    async def set_kiln_rate(
        self,
        kiln_id: int,
        firing_type: FiringType,
        data: dict[str, Any],
        *,
        user: AuthenticatedUser,
    ) -> V2KilnRate:
        """Fija los tres numeros de un horno para un tipo de quema.

        Alta o edicion en la misma operacion: un horno sin tarifa V2 todavia no
        tiene fila, y obligar a crearla primero solo anadiria un paso que nadie
        entenderia.
        """
        if await self._session.get(Kiln, kiln_id) is None:
            raise V2KilnNotFoundError()

        campos = {
            campo: data[campo]
            for campo in ("gas_cost", "external_rate", "student_rate")
            if data.get(campo) is not None
        }

        existia = (
            await self._session.scalar(
                select(V2KilnRate.id).where(
                    V2KilnRate.kiln_id == kiln_id,
                    V2KilnRate.firing_type == firing_type,
                )
            )
        ) is not None

        # INSERT ... ON CONFLICT DO UPDATE, y no «mira si existe y decide»:
        # entre la lectura y la escritura cabe otra peticion, y dos personas
        # configurando el mismo horno a la vez acabarian con un 500 por
        # violacion del UNIQUE. Aqui las serializa la base.
        insercion = pg_insert(V2KilnRate).values(kiln_id=kiln_id, firing_type=firing_type, **campos)
        await self._session.execute(
            insercion.on_conflict_do_update(
                index_elements=["kiln_id", "firing_type"],
                # Solo lo que vino: omitir un campo lo deja como estaba, no lo
                # pone en cero.
                #
                # `updated_at` va explicito porque esta es una sentencia Core:
                # el `onupdate` del modelo es un gancho del ORM y no se dispara
                # aqui. Sin el, la fila se modificaria dejando una fecha de
                # actualizacion que ya no corresponde a nada.
                set_={**campos, "updated_at": func.now()},
            )
        )
        # La sesion no conoce la fila que escribio la sentencia: se relee.
        self._session.expire_all()
        fila = (
            await self._session.scalars(
                select(V2KilnRate).where(
                    V2KilnRate.kiln_id == kiln_id,
                    V2KilnRate.firing_type == firing_type,
                )
            )
        ).one()
        accion = AuditAction.UPDATE if existia else AuditAction.CREATE
        self._audit.record_action(
            entity_type=V2_KILN_RATE_ENTITY,
            entity_id=str(fila.id),
            action=accion,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "kiln_id": str(kiln_id),
                "firing_type": firing_type.value,
                "gas_cost": str(fila.gas_cost),
                "external_rate": str(fila.external_rate),
                "student_rate": str(fila.student_rate),
            },
        )
        return fila

    # ------------------------------------------------------------------
    # Congelado
    # ------------------------------------------------------------------
    async def capture_snapshot(
        self,
        *,
        currency_code: str | None = None,
        exchange_rate: Decimal | None = None,
        commercial_factor: Decimal | None = None,
        customer_kind: V2CustomerKind | None = None,
        production_type: V2ProductionType | None = None,
    ) -> dict[str, Any]:
        """Los valores con los que nace una cotizacion, ya congelados.

        Lo que el usuario pide manda; lo que no pide lo pone la configuracion.
        A partir de aqui la cotizacion vive de esta copia y deja de mirar la
        configuracion: mover un default manana no le cambia el precio.
        """
        v2 = await self.get()
        politica = await self.commercial_policy()

        moneda_snapshot = await self.currency_snapshot(
            currency_code or politica.currency_code, exchange_rate
        )
        factor = (
            commercial_factor if commercial_factor is not None else v2.commercial_factor_default
        )
        # Contra el rango que ESTA cotizacion va a congelar. Sin esta
        # comprobacion el valor llega al CHECK de la base y el cliente recibe
        # un 500 en vez de un 422 que le diga entre que limites puede moverse.
        if not (v2.commercial_factor_min <= factor <= v2.commercial_factor_max):
            raise V2FactorOutOfRangeError(
                "El factor comercial tiene que estar entre "
                f"{v2.commercial_factor_min} y {v2.commercial_factor_max}"
            )

        # Fase 010E. El horno con el que nace la cotizacion: el sugerido para su
        # tipo de produccion. SUGERIDO —dentro de la cotizacion se puede cambiar
        # sin tocar esta configuracion— y no obligatorio: una instalacion recien
        # creada no tiene hornos todavia, y no poder abrir un borrador por eso
        # dejaria el sistema sin forma de empezar.
        tipo = production_type or v2.default_production_type
        horno = await self._suggested_kiln(v2, tipo)

        return {
            "tax_percent_snapshot": politica.tax_percent,
            **moneda_snapshot,
            "validity_days_snapshot": v2.quotation_validity_days,
            "workday_hours_snapshot": v2.workday_hours,
            "space_service_cost_per_day_snapshot": v2.space_service_cost_per_day,
            "administrative_cost_snapshot": v2.administrative_cost_per_quote,
            "rounding_step_snapshot": politica.rounding_step,
            "commercial_factor": factor,
            "commercial_factor_min_snapshot": v2.commercial_factor_min,
            "commercial_factor_max_snapshot": v2.commercial_factor_max,
            "customer_kind": customer_kind or v2.default_customer_kind,
            "production_type": tipo,
            "kiln_id": horno.id if horno is not None else None,
            "kiln_name_snapshot": horno.name if horno is not None else None,
            "kiln_capacity_snapshot": (horno.capacity_volume_cm3 if horno is not None else None),
            "low_fire_enabled": v2.low_fire_enabled_default,
            "high_fire_enabled": v2.high_fire_enabled_default,
            "settings_version_snapshot": v2.version,
            "settings_captured_at": datetime.now(UTC),
        }

    async def currency_snapshot(
        self, currency_code: str | None, exchange_rate: Decimal | None
    ) -> dict[str, Any]:
        """Los tres campos de moneda, coherentes entre si.

        Se extrae a su propio metodo porque 010G permite CAMBIAR la moneda de un
        borrador, y la regla no puede vivir en dos sitios: en moneda base no hay
        tipo de cambio —un 1 ahi seria una tasa inventada que alguien acabaria
        multiplicando— y en moneda extranjera es obligatorio. El CHECK de la
        tabla exige exactamente esas tres combinaciones.

        Una moneda que no conocemos cae a la base en vez de guardarse: es mejor
        cotizar en soles que en una divisa sin simbolo ni tasa.
        """
        moneda = (currency_code or BASE_CURRENCY).upper()
        if moneda not in CURRENCY_SYMBOLS:
            moneda = BASE_CURRENCY
        if moneda == BASE_CURRENCY:
            tasa = None
        else:
            v2 = await self.get()
            tasa = exchange_rate if exchange_rate is not None else v2.default_exchange_rate
        return {
            "currency_code_snapshot": moneda,
            "currency_symbol_snapshot": CURRENCY_SYMBOLS[moneda],
            "exchange_rate_snapshot": tasa,
        }

    async def suggested_kiln_for(self, production_type: V2ProductionType) -> Kiln | None:
        """El horno sugerido para un tipo de produccion. Publico desde 010G.

        Lo necesita el alta —desde 010E— y ahora tambien la edicion de la
        cabecera: cambiar de por menor a por mayor mueve el horno sugerido.
        """
        return await self._suggested_kiln(await self.get(), production_type)

    async def _suggested_kiln(
        self, v2: V2CommercialSettings, production_type: V2ProductionType
    ) -> Kiln | None:
        """El horno sugerido para un tipo de produccion, si sigue en pie.

        Por menor sugiere el chico y por mayor el grande, pero cual es cual lo
        dice la configuracion y no una heuristica sobre el nombre o sobre un
        umbral de capacidad que nadie definio. Un horno configurado y luego
        dado de baja devuelve `None`: es mejor nacer sin horno —y avisarlo— que
        nacer con uno que el taller ya no enciende.
        """
        kiln_id = (
            v2.retail_kiln_id
            if production_type is V2ProductionType.RETAIL
            else v2.wholesale_kiln_id
        )
        if kiln_id is None:
            return None
        horno = await self._session.get(Kiln, kiln_id)
        if horno is None or not horno.active:
            return None
        return horno

    # ------------------------------------------------------------------
    # Validacion
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_factor_range(fila: V2CommercialSettings) -> None:
        """El rango del factor tiene que seguir siendo coherente tras editar.

        La base tambien lo impone, pero un CHECK devuelve un 500 con un
        mensaje que no explica nada. Aqui se responde 422 diciendo cual de los
        tres numeros esta mal.
        """
        if fila.commercial_factor_min < Decimal(2):
            raise APIError(
                "El factor minimo no puede ser menor que x2",
                code="V2_FACTOR_MIN_BELOW_FLOOR",
                status_code=422,
            )
        if fila.commercial_factor_max < fila.commercial_factor_min:
            raise APIError(
                "El factor maximo no puede ser menor que el minimo",
                code="V2_FACTOR_RANGE_INVALID",
                status_code=422,
            )
        if not (
            fila.commercial_factor_min
            <= fila.commercial_factor_default
            <= fila.commercial_factor_max
        ):
            raise APIError(
                "El factor por defecto tiene que estar entre el minimo y el maximo",
                code="V2_FACTOR_DEFAULT_OUT_OF_RANGE",
                status_code=422,
            )
