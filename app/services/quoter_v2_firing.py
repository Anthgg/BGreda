"""Quema del Cotizador V2: que horno, cuantas hornadas y cuanto cuesta.

Fase 010E. Cuatro responsabilidades, y conviene separarlas al leer:

1. **medir** — volumen de cada linea y ocupacion del horno elegido;
2. **contar** — cuantas hornadas pide esa carga, en baja y en alta;
3. **valorar** — el gas REAL que se consume y la TARIFA que se cobra, que son
   dos numeros distintos y se guardan separados a proposito;
4. **repartir** — la quema es global y despues se distribuye entre productos
   segun el volumen que ocupa cada uno.

## Lo que este modulo NO hace

**No multiplica por ocupacion.** El motor historico encarece la pieza que ocupa
poco horno, hasta x3. En V2 eso no existe bajo ningun nombre: la ocupacion dice
cuanto cabe y cuantas veces hay que encender, nada mas.

**No prorratea una hornada incompleta.** Una segunda hornada al 60 % cuesta una
hornada entera, de tarifa y de gas: el horno se enciende completo.

**No cambia el horno ni el tipo de produccion por su cuenta.** Calcula
recomendaciones y las devuelve como avisos. Que una produccion por menor no
quepa en el horno chico es un aviso, no una conversion automatica a por mayor.

**No hace depender el gas del cliente.** Un alumno y un cliente externo pagan
tarifas distintas por la misma quema; el gas que se consume es el mismo.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError
from app.core.quoter_v2_firing import (
    FiringMathError,
    allocate_by_volume,
    batch_loads,
    firing_cost,
    firing_count,
    occupancy_percent,
    piece_volume,
    total_volume,
    volume_share_percent,
)
from app.models.audit import AuditAction
from app.models.firings import FiringType, Kiln
from app.models.masters import Product
from app.models.quoter_v2 import (
    V2CustomerKind,
    V2ProductionType,
    V2Quotation,
    V2QuotationProduct,
    V2QuotationStatus,
)
from app.models.quoter_v2_settings import V2KilnRate
from app.schemas.auth import AuthenticatedUser
from app.services.audit import AuditRecorder

ZERO = Decimal(0)
HUNDRED = Decimal(100)

#: Entidad de auditoria propia. Cambiar el horno de una cotizacion no es lo
#: mismo que cambiar sus materiales ni su mano de obra.
V2_FIRING_ENTITY = "v2_quotation_firing"

#: Avisos. Ninguno bloquea: un borrador a medias tiene que poder guardarse, y
#: una recomendacion que impidiera guardar dejaria de ser una recomendacion.
WARN_KILN_NOT_SELECTED = "V2_FIRING_KILN_NOT_SELECTED"
WARN_KILN_UNAVAILABLE = "V2_FIRING_KILN_UNAVAILABLE"
WARN_RATES_MISSING = "V2_FIRING_RATES_MISSING"
WARN_NO_PROCESS = "V2_FIRING_NO_PROCESS_SELECTED"
WARN_NO_VOLUME = "V2_FIRING_NO_VOLUME"
WARN_LINE_WITHOUT_DIMENSIONS = "V2_FIRING_LINE_WITHOUT_DIMENSIONS"
WARN_OVER_CAPACITY = "V2_FIRING_OVER_CAPACITY"
WARN_RETAIL_OVER_CAPACITY = "V2_FIRING_RETAIL_OVER_CAPACITY"
WARN_SMALLER_KILN_FITS = "V2_FIRING_SMALLER_KILN_FITS"
WARN_LARGER_KILN_SUGGESTED = "V2_FIRING_LARGER_KILN_SUGGESTED"


class V2FiringQuotationNotFoundError(APIError):
    status_code = 404
    code = "V2_FIRING_QUOTATION_NOT_FOUND"
    message = "La cotizacion V2 no existe"


class V2KilnNotFoundError(APIError):
    status_code = 404
    code = "V2_FIRING_KILN_NOT_FOUND"
    message = "El horno no existe"


class V2KilnInactiveError(APIError):
    """Elegir HOY un horno dado de baja.

    Se rechaza al elegirlo y solo se avisa si ya estaba elegido: bloquear un
    borrador porque alguien retiro el horno en otra pantalla lo dejaria
    encallado, y la cotizacion conserva igualmente lo que congelo.
    """

    status_code = 422
    code = "V2_FIRING_KILN_INACTIVE"
    message = "El horno esta dado de baja y no puede elegirse"


class V2FiringInputInvalid(APIError):
    status_code = 422
    code = "V2_FIRING_INPUT_INVALID"
    message = "Los datos no permiten calcular la quema"


class V2FiringNotEditableError(APIError):
    status_code = 409
    code = "V2_FIRING_QUOTATION_NOT_EDITABLE"
    message = "La cotizacion ya no es un borrador y su quema no puede cambiarse"


@dataclass(frozen=True)
class KilnOption:
    """Un horno del taller y lo que pasaria si se eligiera este.

    La ocupacion y las hornadas de CADA horno viajan con la lista porque son
    justo lo que hace falta para decidir: sin ellas la pantalla ofreceria una
    lista de nombres y quien elige tendria que calcular de cabeza.
    """

    kiln_id: int
    code: str
    name: str
    capacity_cm3: Decimal
    active: bool
    occupancy_percent: Decimal
    firing_count: int
    has_rates: bool


@dataclass(frozen=True)
class FiringState:
    """Todo lo que hay que saber de la quema de una cotizacion."""

    quotation: V2Quotation
    lines: list[V2QuotationProduct]
    batch_loads: tuple[Decimal, ...]
    kilns: list[KilnOption]
    #: El horno que el sistema recomendaria. RECOMIENDA: no se aplica solo.
    recommended_kiln_id: int | None
    warnings: list[str]


def apply_line_geometry(line: V2QuotationProduct, data: dict[str, Any]) -> None:
    """Deja en la linea sus medidas y el volumen que ocupa.

    Se recalcula SIEMPRE, aunque el cambio parezca no afectar al volumen: subir
    la cantidad cambia el volumen total, y un volumen que no se recalcula es un
    volumen que deja de corresponder a su linea —y con el, las hornadas—.

    Las medidas ausentes dejan el volumen en cero en vez de reventar. Un
    borrador a medio llenar es legitimo: se anade la linea, se elige la pasta y
    las medidas llegan despues. Quien llama avisa.
    """
    for campo in ("length_cm", "width_cm", "height_cm"):
        if campo in data:
            setattr(line, campo, data[campo])

    line.unit_volume_cm3 = piece_volume(line.length_cm, line.width_cm, line.height_cm)
    line.total_volume_cm3 = total_volume(line.unit_volume_cm3, line.quantity)


def copy_master_dimensions(line: V2QuotationProduct, product: Product) -> None:
    """Ofrece las medidas del catalogo cuando la linea no tiene las suyas.

    No las pisa: lo que el usuario ya escribio manda. Mismo criterio que el
    gramaje en 010C, y por el mismo motivo: remedir una pieza en el maestro no
    puede recalcular por detras el volumen de algo ya cotizado.

    Solo escribe las MEDIDAS. Quien la llame tiene que pasar despues por
    `apply_line_geometry`, que es la unica que recalcula el volumen: si no, la
    linea se queda con las medidas del catalogo y un volumen de cero.
    """
    if line.length_cm is None and product.length is not None:
        line.length_cm = product.length
    if line.width_cm is None and product.width is not None:
        line.width_cm = product.width
    if line.height_cm is None and product.height is not None:
        line.height_cm = product.height


async def _lines_of(session: AsyncSession, quotation_id: int) -> list[V2QuotationProduct]:
    consulta = (
        select(V2QuotationProduct)
        .where(V2QuotationProduct.v2_quotation_id == quotation_id)
        .order_by(V2QuotationProduct.sort_order, V2QuotationProduct.id)
    )
    return list((await session.scalars(consulta)).all())


async def _rates_of(session: AsyncSession, kiln_id: int) -> dict[FiringType, V2KilnRate]:
    filas = (await session.scalars(select(V2KilnRate).where(V2KilnRate.kiln_id == kiln_id))).all()
    return {fila.firing_type: fila for fila in filas}


def _commercial_rate(rate: V2KilnRate, customer_kind: V2CustomerKind) -> Decimal:
    """Lo que se COBRA por una hornada, segun a quien se le cobra.

    El tipo de cliente es un valor persistido y elegido, nunca deducido del
    nombre ni del correo del tercero.
    """
    if customer_kind is V2CustomerKind.STUDENT:
        return rate.student_rate
    return rate.external_rate


def _reset_firing(quotation: V2Quotation, lines: list[V2QuotationProduct]) -> None:
    """Deja la quema en cero. Sin horno no hay hornada ni importe que valga."""
    quotation.firing_occupancy_percent = ZERO
    quotation.firing_count = 0
    quotation.low_fire_count = 0
    quotation.high_fire_count = 0
    quotation.firing_gas_total = ZERO
    quotation.firing_commercial_total = ZERO
    for linea in lines:
        linea.firing_occupancy_percent = ZERO
        linea.firing_commercial_cost = ZERO
        linea.firing_gas_cost = ZERO


async def refresh_firing(session: AsyncSession, quotation: V2Quotation) -> list[str]:
    """Recalcula la quema entera de una cotizacion y la deja escrita.

    Se llama tanto al tocar la quema como al tocar una linea: cambiar la
    cantidad de un producto cambia el volumen, y con el las hornadas y el
    reparto. Una quema que no se recalculara al anadir una pieza diria un
    numero de hornadas que ya no corresponde a lo que se va a quemar.

    **Solo escribe sobre borradores.** Una cotizacion emitida conserva lo que
    congelo: la funcion se llama igualmente para poder leerla, y sale sin tocar
    nada. Devuelve avisos, nunca excepciones, para lo que no impide calcular.
    """
    # `no_autoflush` protege los estados INTERMEDIOS, y cubre ya la primera
    # consulta: quien llama pudo apagar la quema alta justo antes, y leer las
    # lineas escribiria esa fila con «alta apagada y dos hornadas», que es
    # exactamente lo que la base tiene prohibido —y con razon—.
    #
    # Lo mismo ocurre dentro del recalculo: entre poner las hornadas y poner
    # los importes hay una consulta de tarifas, y un flush ahi escribiria una
    # fila a medio hacer. Los `flush` explicitos de dentro escriben solo cuando
    # la fila ya es coherente consigo misma.
    with session.no_autoflush:
        lineas = await _lines_of(session, quotation.id)
        if quotation.status is not V2QuotationStatus.DRAFT:
            return []
        return await _recalculate(session, quotation, lineas)


async def _recalculate(
    session: AsyncSession, quotation: V2Quotation, lineas: list[V2QuotationProduct]
) -> list[str]:
    avisos: list[str] = []
    volumen = sum((linea.total_volume_cm3 for linea in lineas), ZERO)
    quotation.firing_total_volume_cm3 = volumen

    for linea in lineas:
        linea.firing_volume_share_percent = volume_share_percent(linea.total_volume_cm3, volumen)

    if lineas and any(linea.total_volume_cm3 <= ZERO for linea in lineas):
        avisos.append(WARN_LINE_WITHOUT_DIMENSIONS)

    if quotation.kiln_id is None:
        _reset_firing(quotation, lineas)
        await session.flush()
        return [*avisos, WARN_KILN_NOT_SELECTED]

    horno = await session.get(Kiln, quotation.kiln_id)
    if horno is None:
        # La FK es RESTRICT, asi que esto no deberia poder pasar. Si pasara,
        # dejar la quema en cero es mas honesto que inventar una capacidad.
        _reset_firing(quotation, lineas)
        await session.flush()
        return [*avisos, WARN_KILN_NOT_SELECTED]
    if not horno.active:
        avisos.append(WARN_KILN_UNAVAILABLE)

    # La capacidad CONGELADA manda sobre la del maestro: si manana se remide el
    # horno, esta cotizacion sigue explicando sus hornadas con el numero que
    # uso. Se congela ahora si todavia no lo estaba.
    if quotation.kiln_capacity_snapshot is None:
        quotation.kiln_capacity_snapshot = horno.capacity_volume_cm3
        quotation.kiln_name_snapshot = horno.name
    capacidad = quotation.kiln_capacity_snapshot

    try:
        quotation.firing_occupancy_percent = occupancy_percent(volumen, capacidad)
        hornadas = firing_count(volumen, capacidad)
    except FiringMathError as error:
        raise V2FiringInputInvalid(str(error)) from error
    quotation.firing_count = hornadas

    baja = bool(quotation.low_fire_enabled)
    alta = bool(quotation.high_fire_enabled)
    if not baja and not alta:
        avisos.append(WARN_NO_PROCESS)
    # Apagada es apagada: cero hornadas y, por tanto, cero costo. No quedan
    # importes escondidos de una quema que alguien desactivo.
    quotation.low_fire_count = hornadas if baja else 0
    quotation.high_fire_count = hornadas if alta else 0

    tarifas = await _rates_of(session, quotation.kiln_id)
    cliente = quotation.customer_kind or V2CustomerKind.EXTERNAL
    falta_tarifa = False

    for tipo, gas_campo, comercial_campo, gas_override, comercial_override in (
        (
            FiringType.LOW,
            "gas_cost_low_snapshot",
            "commercial_rate_low_snapshot",
            "gas_low_is_override",
            "commercial_low_is_override",
        ),
        (
            FiringType.HIGH,
            "gas_cost_high_snapshot",
            "commercial_rate_high_snapshot",
            "gas_high_is_override",
            "commercial_high_is_override",
        ),
    ):
        fila = tarifas.get(tipo)
        if fila is None:
            # Sin tarifa configurada no se inventa una: la aritmetica de abajo
            # cuenta el hueco como cero y se avisa.
            #
            # El snapshot se deja en NULL a proposito. Guardar aqui un cero lo
            # volveria indistinguible de una tarifa elegida, y el recalculo
            # siguiente ya no lo rellenaria: configurar la tarifa despues
            # dejaria el borrador costeando en cero para siempre.
            falta_tarifa = True
            continue
        # Los snapshots se toman UNA vez. Si ya estan —congelados al elegir el
        # horno o pactados dentro de la cotizacion— se respetan: subir manana
        # la tarifa del taller no puede reescribir un precio ya entregado.
        if not getattr(quotation, gas_override) and getattr(quotation, gas_campo) is None:
            setattr(quotation, gas_campo, fila.gas_cost)
        if (
            not getattr(quotation, comercial_override)
            and getattr(quotation, comercial_campo) is None
        ):
            setattr(quotation, comercial_campo, _commercial_rate(fila, cliente))

    if falta_tarifa:
        avisos.append(WARN_RATES_MISSING)

    gas_baja = quotation.gas_cost_low_snapshot or ZERO
    gas_alta = quotation.gas_cost_high_snapshot or ZERO
    com_baja = quotation.commercial_rate_low_snapshot or ZERO
    com_alta = quotation.commercial_rate_high_snapshot or ZERO

    try:
        quotation.firing_gas_total = firing_cost(quotation.low_fire_count, gas_baja) + firing_cost(
            quotation.high_fire_count, gas_alta
        )
        quotation.firing_commercial_total = firing_cost(
            quotation.low_fire_count, com_baja
        ) + firing_cost(quotation.high_fire_count, com_alta)
    except FiringMathError as error:
        raise V2FiringInputInvalid(str(error)) from error

    # El reparto: la quema es global y cada producto absorbe su participacion
    # en el VOLUMEN, no una hornada suya.
    volumenes = [linea.total_volume_cm3 for linea in lineas]
    comerciales = allocate_by_volume(quotation.firing_commercial_total, volumenes)
    gases = allocate_by_volume(quotation.firing_gas_total, volumenes)
    for linea, comercial, gas in zip(lineas, comerciales, gases, strict=True):
        linea.firing_occupancy_percent = occupancy_percent(linea.total_volume_cm3, capacidad)
        linea.firing_commercial_cost = comercial
        linea.firing_gas_cost = gas

    # El estado ya es coherente: hornadas, conteos e importes se corresponden.
    # Se escribe aqui, a mano, porque el bloque de arriba impidio que ninguna
    # consulta lo escribiera a medias.
    await session.flush()

    if volumen <= ZERO:
        avisos.append(WARN_NO_VOLUME)
    if quotation.firing_occupancy_percent > HUNDRED:
        avisos.append(WARN_OVER_CAPACITY)
        if quotation.production_type is V2ProductionType.RETAIL:
            # Por menor deberia caber en el horno chico. Que no quepa se avisa
            # y NO convierte la cotizacion en por mayor: eso lo decide quien
            # cotiza, mirando el pedido y no el volumen.
            avisos.append(WARN_RETAIL_OVER_CAPACITY)
    return avisos


class V2FiringService:
    """Horno, hornadas y costo de quema de una cotizacion V2."""

    def __init__(self, session: AsyncSession, audit: AuditRecorder) -> None:
        self._session = session
        self._audit = audit

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------
    async def firing_state(self, quotation_id: int) -> FiringState:
        """La quema de una cotizacion, lista para presentarla.

        Recalcula si es borrador —las lineas pueden haber cambiado por otra
        via— y se limita a leer si ya esta emitida.

        Que recalcule no la convierte en una escritura encubierta: la ruta de
        lectura no confirma la transaccion, asi que lo recalculado sirve para
        responder y se descarta. Lo que persiste lo escribe quien de verdad
        cambio algo —la quema o una linea—, y por eso los dos coinciden.
        """
        quotation = await self.quotation(quotation_id)
        avisos = await refresh_firing(self._session, quotation)
        lineas = await _lines_of(self._session, quotation_id)
        hornos = await self._kiln_options(quotation)
        recomendado = self._recommended_kiln(quotation, hornos)

        if (
            recomendado is not None
            and quotation.kiln_id is not None
            and recomendado != quotation.kiln_id
        ):
            actual = next((h for h in hornos if h.kiln_id == quotation.kiln_id), None)
            sugerido = next((h for h in hornos if h.kiln_id == recomendado), None)
            if actual is not None and sugerido is not None:
                if sugerido.capacity_cm3 < actual.capacity_cm3:
                    avisos.append(WARN_SMALLER_KILN_FITS)
                elif sugerido.capacity_cm3 > actual.capacity_cm3:
                    avisos.append(WARN_LARGER_KILN_SUGGESTED)

        return FiringState(
            quotation=quotation,
            lines=lineas,
            batch_loads=batch_loads(quotation.firing_occupancy_percent, quotation.firing_count),
            kilns=hornos,
            recommended_kiln_id=recomendado,
            warnings=avisos,
        )

    async def _kiln_options(self, quotation: V2Quotation) -> list[KilnOption]:
        """Los hornos que se pueden elegir, con lo que costaria elegir cada uno.

        Se listan los ACTIVOS y, si el de la cotizacion ya no lo esta, tambien
        el suyo: quitarlo de la lista dejaria la pantalla enseñando un horno
        que no aparece entre las opciones.
        """
        condicion: ColumnElement[bool] = Kiln.active.is_(True)
        if quotation.kiln_id is not None:
            condicion = or_(condicion, Kiln.id == quotation.kiln_id)
        hornos = list(
            (await self._session.scalars(select(Kiln).where(condicion).order_by(Kiln.code))).all()
        )
        if not hornos:
            return []

        con_tarifa = {
            fila.kiln_id
            for fila in (
                await self._session.scalars(
                    select(V2KilnRate).where(V2KilnRate.kiln_id.in_([horno.id for horno in hornos]))
                )
            ).all()
        }
        # `or ZERO`: el valor por defecto de la columna lo pone la base, asi
        # que una cotizacion recien creada y todavia no releida lo tiene en
        # NULL en memoria. Compararlo o dividirlo ahi seria un 500.
        volumen = quotation.firing_total_volume_cm3 or ZERO
        opciones: list[KilnOption] = []
        for horno in hornos:
            # Para el horno ya elegido se usa la capacidad CONGELADA: es la que
            # produjo las hornadas que la cotizacion dice tener, y mezclar las
            # dos daria una lista que se contradice con su propia cabecera.
            capacidad = (
                quotation.kiln_capacity_snapshot
                if horno.id == quotation.kiln_id and quotation.kiln_capacity_snapshot is not None
                else horno.capacity_volume_cm3
            )
            opciones.append(
                KilnOption(
                    kiln_id=horno.id,
                    code=horno.code,
                    name=horno.name,
                    capacity_cm3=capacidad,
                    active=horno.active,
                    occupancy_percent=occupancy_percent(volumen, capacidad),
                    firing_count=firing_count(volumen, capacidad),
                    has_rates=horno.id in con_tarifa,
                )
            )
        return opciones

    @staticmethod
    def _recommended_kiln(quotation: V2Quotation, kilns: list[KilnOption]) -> int | None:
        """El horno mas pequeno donde la carga cabe de una sola vez.

        Es una RECOMENDACION y nunca se aplica sola. Se busca el mas pequeno
        que baste porque encender un horno grande a medias cuesta mas gas por
        lo mismo; si ninguno basta, se recomienda el mayor, que es el que
        exigira menos hornadas.

        No hay enum chico/grande en ningun sitio: se compara la capacidad, que
        es el unico dato de tamano que el maestro tiene de verdad. Deducir el
        tamano del nombre pondria la tarifa de S/200 en el horno equivocado.
        """
        elegibles = [horno for horno in kilns if horno.active and horno.has_rates]
        if not elegibles:
            return None
        if (quotation.firing_total_volume_cm3 or ZERO) <= ZERO:
            return None
        caben = [horno for horno in elegibles if horno.firing_count <= 1]
        if caben:
            return min(caben, key=lambda horno: (horno.capacity_cm3, horno.kiln_id)).kiln_id
        return max(elegibles, key=lambda horno: (horno.capacity_cm3, -horno.kiln_id)).kiln_id

    async def quotation(self, quotation_id: int) -> V2Quotation:
        """La cotizacion, para LEER. Sin bloqueo y sin exigir que sea borrador."""
        quotation = await self._session.get(V2Quotation, quotation_id)
        if quotation is None:
            raise V2FiringQuotationNotFoundError()
        return quotation

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------
    async def set_firing(
        self, quotation_id: int, data: dict[str, Any], *, user: AuthenticatedUser
    ) -> FiringState:
        """Cambia el horno, los procesos, el tipo de cliente o las tarifas.

        Semantica de PATCH, y la distincion es el aprendizaje caro de 010C y
        010D: **la presencia de una clave no es un cambio**. Reenviar el mismo
        `kiln_id` no es elegir horno hoy, y por tanto no retira las tarifas
        pactadas dentro de esta cotizacion.
        """
        quotation = await self._draft(quotation_id)

        cambia_horno = "kiln_id" in data and data["kiln_id"] != quotation.kiln_id
        cambia_cliente = (
            "customer_kind" in data and data["customer_kind"] != quotation.customer_kind
        )

        if cambia_horno:
            await self._apply_kiln(quotation, data["kiln_id"])
        if "customer_kind" in data and data["customer_kind"] is not None:
            quotation.customer_kind = data["customer_kind"]
        if "low_fire_enabled" in data and data["low_fire_enabled"] is not None:
            quotation.low_fire_enabled = bool(data["low_fire_enabled"])
        if "high_fire_enabled" in data and data["high_fire_enabled"] is not None:
            quotation.high_fire_enabled = bool(data["high_fire_enabled"])

        if cambia_cliente and not cambia_horno:
            # Cambiar a quien se cotiza reevalua lo que se COBRA y solo eso. El
            # gas no depende del cliente: un alumno y un externo queman el mismo
            # gas. Los acuerdos de tarifa no se heredan porque se tomaron para
            # otro tipo de cliente.
            quotation.commercial_rate_low_snapshot = None
            quotation.commercial_rate_high_snapshot = None
            quotation.commercial_low_is_override = False
            quotation.commercial_high_is_override = False

        self._apply_overrides(quotation, data)

        # UN solo recalculo: `firing_state` ya lo hace y ademas arma la
        # respuesta. Llamarlo aqui tambien duplicaba consultas y obligaba a
        # fusionar dos listas de avisos que siempre decian lo mismo.
        estado = await self.firing_state(quotation_id)
        await self._session.flush()

        self._audit.record_action(
            entity_type=V2_FIRING_ENTITY,
            entity_id=str(quotation.id),
            action=AuditAction.UPDATE,
            user_id=user.id,
            user_display_name=user.display_name,
            metadata={
                "kiln_id": str(quotation.kiln_id),
                "firing_count": str(quotation.firing_count),
                "commercial_total": str(quotation.firing_commercial_total),
                "gas_total": str(quotation.firing_gas_total),
            },
        )
        return estado

    async def _apply_kiln(self, quotation: V2Quotation, kiln_id: int | None) -> None:
        """Cambia el horno de verdad: recongela capacidad y retira las tarifas.

        Solo se llama cuando el horno CAMBIA. Las tarifas pactadas se retiran
        porque se pactaron sobre otro horno: mantener los S/40 acordados para
        el chico al pasar al grande cobraria una quema de horno grande a precio
        de horno chico.
        """
        if kiln_id is None:
            quotation.kiln_id = None
            quotation.kiln_name_snapshot = None
            quotation.kiln_capacity_snapshot = None
        else:
            horno = await self._session.get(Kiln, kiln_id)
            if horno is None:
                raise V2KilnNotFoundError()
            if not horno.active:
                raise V2KilnInactiveError(f"«{horno.name}» esta dado de baja")
            quotation.kiln_id = horno.id
            quotation.kiln_name_snapshot = horno.name
            quotation.kiln_capacity_snapshot = horno.capacity_volume_cm3

        quotation.gas_cost_low_snapshot = None
        quotation.gas_cost_high_snapshot = None
        quotation.commercial_rate_low_snapshot = None
        quotation.commercial_rate_high_snapshot = None
        quotation.gas_low_is_override = False
        quotation.gas_high_is_override = False
        quotation.commercial_low_is_override = False
        quotation.commercial_high_is_override = False

    @staticmethod
    def _apply_overrides(quotation: V2Quotation, data: dict[str, Any]) -> None:
        """Los cuatro importes pactados dentro de ESTA cotizacion.

        Presente con valor: manda, y queda marcado como decision. Presente en
        nulo: retira el acuerdo y deja que el recalculo vuelva a leer el
        maestro. AUSENTE: se conserva lo que hubiera, que es la mitad de la
        regla que 010C aprendio a base de borrar precios pactados.

        Un cero explicito es un valor, no una ausencia: un horno prestado con
        gas incluido se cotiza con gas cero, y eso tiene que poder decirse.
        """
        for campo, columna, marca in (
            ("gas_cost_low_override", "gas_cost_low_snapshot", "gas_low_is_override"),
            ("gas_cost_high_override", "gas_cost_high_snapshot", "gas_high_is_override"),
            (
                "commercial_rate_low_override",
                "commercial_rate_low_snapshot",
                "commercial_low_is_override",
            ),
            (
                "commercial_rate_high_override",
                "commercial_rate_high_snapshot",
                "commercial_high_is_override",
            ),
        ):
            if campo not in data:
                continue
            valor = data[campo]
            if valor is None:
                setattr(quotation, columna, None)
                setattr(quotation, marca, False)
            else:
                setattr(quotation, columna, valor)
                setattr(quotation, marca, True)

    async def _draft(self, quotation_id: int) -> V2Quotation:
        """La cotizacion, bloqueada, si todavia admite cambios de quema.

        Mismo criterio y mismo motivo que en 010C y 010D: leer el estado sin
        bloquear deja una ventana entre la comprobacion y el guardado por la
        que una emision simultanea colaria un horno distinto en una cotizacion
        ya comprometida. El bloqueo ademas serializa dos ediciones de quema de
        la misma cotizacion, que si no recalcularian las dos sobre el mismo
        estado viejo y una de las dos se perderia sin avisar.
        """
        quotation = (
            await self._session.scalars(
                select(V2Quotation).where(V2Quotation.id == quotation_id).with_for_update()
            )
        ).one_or_none()
        if quotation is None:
            raise V2FiringQuotationNotFoundError()
        if quotation.status is not V2QuotationStatus.DRAFT:
            raise V2FiringNotEditableError()
        return quotation


__all__ = [
    "FiringState",
    "KilnOption",
    "V2FiringInputInvalid",
    "V2FiringNotEditableError",
    "V2FiringQuotationNotFoundError",
    "V2FiringService",
    "V2KilnInactiveError",
    "V2KilnNotFoundError",
    "apply_line_geometry",
    "copy_master_dimensions",
    "refresh_firing",
]
