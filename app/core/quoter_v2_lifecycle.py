"""Fase 010H — el calendario y la huella de una cotizacion V2 emitida.

Funciones puras, sin base de datos. Aqui se decide cuando vence una oferta y si
lo que alguien vio en pantalla es lo mismo que se va a congelar. Las dos cosas
son de las que no revientan: fallan cobrando un precio que ya no valia o
emitiendo un documento distinto del que se reviso.

## La vigencia se cuenta en dias de calendario de Lima

El Excel aprobado lo dice con una formula: `Vencida si HOY() > emision +
vigencia`. Emitida el 10/09 con 20 dias, se respeta HASTA el 30/09 incluido y
vence el 01/10. Contar en horas desde la emision —«20 x 24 h»— haria que una
cotizacion emitida a las 18:00 venciera a las 18:00 del ultimo dia, cuando el
cliente la lee como valida el dia entero.

«Hoy» es el de Lima, no el del servidor: Cloud Run corre en UTC, y a las 20:00
de Lima ya es manana en UTC. Sin zona explicita, una oferta venceria cinco
horas antes de lo que dice el papel.

**UTC-5 fijo y no `zoneinfo`.** Peru no tiene horario de verano desde 1994, y
la imagen del contenedor no trae la base `tzdata`: `ZoneInfo("America/Lima")`
reventaria en produccion con un error que ninguna prueba local veria. Un
desplazamiento fijo da exactamente las mismas fechas sin esa dependencia.

## Dos marcas y no una

- `valid_until` (fecha) es lo que se imprime: «Valida hasta 30/09/2026».
- `expires_at` (instante) es el LIMITE EXCLUSIVO: las 00:00 de Lima del dia
  siguiente. Comparar instantes contra `now()` de la base evita que cada capa
  vuelva a convertir zonas por su cuenta.

## La huella comercial

Confirmar congela lo que hay en la fila EN ESE INSTANTE. Si entre que una
persona reviso el resumen y pulso «Confirmar» otra cambio una cantidad, se
emitiria un documento que nadie vio. La huella es un hash de todo lo que el
resumen ensena y de todo lo que mueve el precio: si no coincide con la que el
navegador recibio, la emision se rechaza con un conflicto en vez de congelar
datos distintos a los mostrados.

Los decimales se normalizan antes de hashear: `Decimal("10.500000")` y
`Decimal("10.5")` son el mismo importe, y leerlo de la base con otra escala no
puede producir un falso conflicto.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

#: Hora oficial de Peru. Sin horario de verano: ver la cabecera del modulo.
BUSINESS_TZ = timezone(timedelta(hours=-5), "America/Lima")

#: Tope de vigencia. El MISMO que admite la configuracion de V2 (010B): con
#: uno mas bajo aqui, una casa configurada a 400 dias crearia borradores que
#: nunca podrian emitirse, y el motivo no estaria en ninguna pantalla.
MAX_VALIDITY_DAYS = 3650


class V2EffectiveStatus(StrEnum):
    """Lo que la cotizacion ES hoy, derivado de su estado y del reloj.

    `EXPIRED` y `READY_FOR_PRODUCTION` no se guardan: se calculan. Guardar
    «vencida» exigiria un proceso que la marque a medianoche, y el servicio
    escala a cero; el dia que ese proceso no corriera, la base diria «vigente»
    de una oferta que el papel da por vencida. Derivarlo es la unica forma de
    que haya una sola verdad.
    """

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    EXPIRED = "EXPIRED"
    READY_FOR_PRODUCTION = "READY_FOR_PRODUCTION"
    CANCELLED = "CANCELLED"


class ValidityError(ValueError):
    """Una vigencia imposible: cero, negativa o absurda."""


def business_date(instant: datetime) -> date:
    """La fecha de calendario de Lima de un instante con zona."""
    if instant.tzinfo is None:
        raise ValidityError("El instante no tiene zona horaria")
    return instant.astimezone(BUSINESS_TZ).date()


def compute_validity(issued_at: datetime, validity_days: int) -> tuple[date, datetime]:
    """`(valid_until, expires_at)` de una emision.

    `valid_until = fecha_Lima(emision) + dias`. `expires_at` son las 00:00 de
    Lima del dia siguiente, en UTC. Una vigencia de 1 dia emitida el lunes a
    las 23:59 se respeta el martes entero.
    """
    if isinstance(validity_days, bool) or not isinstance(validity_days, int):
        raise ValidityError("La vigencia tiene que ser un numero entero de dias")
    if validity_days <= 0:
        raise ValidityError("La vigencia tiene que ser de al menos un dia")
    if validity_days > MAX_VALIDITY_DAYS:
        raise ValidityError(f"La vigencia no puede superar {MAX_VALIDITY_DAYS} dias")
    valid_until = business_date(issued_at) + timedelta(days=validity_days)
    siguiente = valid_until + timedelta(days=1)
    expires_at = datetime.combine(siguiente, time.min, tzinfo=BUSINESS_TZ).astimezone(UTC)
    return valid_until, expires_at


def is_expired(expires_at: datetime | None, now: datetime) -> bool:
    """Si la oferta ya no vale. Sin fecha de vencimiento no hay nada que vencer."""
    if expires_at is None:
        return False
    return now >= expires_at


def effective_status(
    *,
    status: str,
    expires_at: datetime | None,
    has_production_handoff: bool,
    now: datetime,
) -> V2EffectiveStatus:
    """El estado que se ensena y que decide que se puede hacer.

    El orden es el del Excel: lo cancelado es cancelado aunque haya vencido;
    lo que ya paso a produccion no vence —el cliente acepto dentro de plazo—;
    y solo una emitida sin aceptar puede vencer.
    """
    if status == "DRAFT":
        return V2EffectiveStatus.DRAFT
    if status == "CANCELLED":
        return V2EffectiveStatus.CANCELLED
    if has_production_handoff:
        return V2EffectiveStatus.READY_FOR_PRODUCTION
    if is_expired(expires_at, now):
        return V2EffectiveStatus.EXPIRED
    return V2EffectiveStatus.CONFIRMED


def _canonical(value: Any) -> Any:
    """Un valor listo para JSON estable: decimales sin ceros de escala."""
    if isinstance(value, Decimal):
        if value.is_nan() or value.is_infinite():
            raise ValueError("Un importe no puede ser NaN ni infinito")
        normalizado = value.normalize()
        # `normalize` deja `0E-18` para un cero con escala: se unifica.
        return "0" if normalizado == 0 else format(normalizado, "f")
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_canonical(v) for v in value]
    return str(value)


def commercial_fingerprint(payload: Mapping[str, Any]) -> str:
    """SHA-256 hexadecimal de un contenido comercial, independiente de la escala."""
    texto = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


__all__ = [
    "BUSINESS_TZ",
    "MAX_VALIDITY_DAYS",
    "V2EffectiveStatus",
    "ValidityError",
    "business_date",
    "commercial_fingerprint",
    "compute_validity",
    "effective_status",
    "is_expired",
]
