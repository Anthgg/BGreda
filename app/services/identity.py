"""Orquestacion de la consulta de identidad: cache, fallback, cuotas, log.

``IdentityLookupService`` es el unico punto que decide a que proveedor
preguntar y cuando. Ni el router ni el frontend conocen Peru API ni Decolecta
por su nombre: solo conocen ``GET /identity/dni/{dni}`` y ``GET
/identity/ruc/{ruc}``.

## Flujo (§12 del encargo)

1. validar el documento;
2. mirar la cache, salvo que se pida refresco explicito;
3. preguntar al primario (Peru API);
4. si RATE_LIMITED, TIMEOUT o error de proveedor: preguntar al secundario
   (Decolecta);
5. si NOT_FOUND: preguntar al secundario solo si
   ``IDENTITY_FALLBACK_ON_NOT_FOUND`` esta activo, porque un documento
   inexistente gastaria dos cuotas sin obtener nada;
6. si ambos fallan: error normalizado, nunca el texto crudo del proveedor.

Nunca se preguntan los dos proveedores en paralelo: una consulta util no debe
costar dos cuotas.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity import (
    IdentityDocumentType,
    IdentityLookupUnavailableError,
    LookupStatus,
    ProviderName,
    document_hash,
    mask_document,
    normalize_dni,
    normalize_ruc,
)
from app.models.catalog import UbigeoDistrict
from app.models.identity import (
    IdentityLookupAuditEvent,
    IdentityLookupCache,
    IdentityLookupDailyStat,
    IdentityLookupProviderMetric,
)
from app.schemas.auth import AuthenticatedUser
from app.schemas.identity import DniLookupOut, RucLookupOut
from app.services.identity_providers import IdentityProvider, ProviderLookupResult

logger = logging.getLogger(__name__)

#: Estados que justifican intentar el proveedor secundario. NOT_FOUND se
#: decide aparte, segun la politica configurable.
_FAILOVER_STATUSES = frozenset(
    {LookupStatus.RATE_LIMITED, LookupStatus.TIMEOUT, LookupStatus.PROVIDER_ERROR}
)

#: Tras estos fallos consecutivos de un proveedor, se le da un respiro en vez
#: de seguir llamandolo: es un cortocircuito ligero en memoria, no un sistema
#: de telecomunicaciones. No sobrevive a un reinicio del proceso ni se
#: coordina entre instancias, y eso es una eleccion deliberada: con el
#: trafico esperado (decenas de consultas), un cortocircuito perfecto entre
#: instancias no vale la complejidad de coordinarlo.
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_COOLDOWN_SECONDS = 60


@dataclass
class _CircuitState:
    failures: int = 0
    open_until: float = 0.0


class _ProviderCircuitBreaker:
    """Cortocircuito en memoria de proceso, por proveedor."""

    def __init__(self) -> None:
        self._state: dict[ProviderName, _CircuitState] = {}

    def is_open(self, provider: ProviderName) -> bool:
        state = self._state.get(provider)
        return bool(state and time.monotonic() < state.open_until)

    def record_failure(self, provider: ProviderName) -> None:
        state = self._state.setdefault(provider, _CircuitState())
        state.failures += 1
        if state.failures >= _CIRCUIT_FAILURE_THRESHOLD:
            state.open_until = time.monotonic() + _CIRCUIT_COOLDOWN_SECONDS

    def record_success(self, provider: ProviderName) -> None:
        self._state.pop(provider, None)


class _InMemoryRateLimiter:
    """Ventana deslizante simple por (usuario, documento).

    Protege contra un bug o un doble clic que agote la cuota externa, no
    contra abuso coordinado: vive en memoria de un solo proceso, igual que el
    cortocircuito, y es una eleccion deliberada por la misma razon.
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> bool:
        """``True`` si la peticion esta permitida; registra el intento."""
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._max:
            return False
        hits.append(now)
        return True


# Los dos guardias son de modulo, no de instancia: deben sobrevivir entre
# peticiones dentro del mismo proceso, y la dependencia de FastAPI crea un
# IdentityLookupService por peticion.
_circuit_breaker = _ProviderCircuitBreaker()


@dataclass(frozen=True, slots=True)
class IdentityLookupMetrics:
    """Contadores del proceso actual, expuestos para pruebas/observabilidad."""

    fallback_used: bool = False
    cache_hit: bool = False


class IdentityLookupService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        primary: IdentityProvider | None,
        secondary: IdentityProvider | None,
        dni_ttl_days: int,
        ruc_ttl_days: int,
        fallback_on_not_found: bool,
        rate_limiter: _InMemoryRateLimiter,
    ) -> None:
        self._session = session
        self._primary = primary
        self._secondary = secondary
        self._dni_ttl = timedelta(days=dni_ttl_days)
        self._ruc_ttl = timedelta(days=ruc_ttl_days)
        self._fallback_on_not_found = fallback_on_not_found
        self._rate_limiter = rate_limiter

    async def lookup_dni(
        self, raw_document: str, *, user: AuthenticatedUser, refresh: bool
    ) -> DniLookupOut:
        document = normalize_dni(raw_document)
        record = await self._resolve(
            IdentityDocumentType.DNI, document, ttl=self._dni_ttl, refresh=refresh, user=user
        )
        # `model_validate` en vez de `**record`: `record` es `dict[str, Any]`
        # construido dinamicamente (viene de la cache o del proveedor), y
        # desempaquetarlo como argumentos con nombre no es algo que mypy pueda
        # verificar contra los tipos literales del esquema.
        return DniLookupOut.model_validate(record)

    async def lookup_ruc(
        self, raw_document: str, *, user: AuthenticatedUser, refresh: bool
    ) -> RucLookupOut:
        document = normalize_ruc(raw_document)
        record = await self._resolve(
            IdentityDocumentType.RUC, document, ttl=self._ruc_ttl, refresh=refresh, user=user
        )
        ubigeo_names = await self._resolve_ubigeo(record.get("ubigeo"))
        return RucLookupOut.model_validate({**record, **ubigeo_names})

    # -- Orquestacion ---------------------------------------------------
    async def _resolve(
        self,
        document_type: IdentityDocumentType,
        document: str,
        *,
        ttl: timedelta,
        refresh: bool,
        user: AuthenticatedUser,
    ) -> dict[str, Any]:
        started = time.monotonic()
        masked = mask_document(document_type, document)

        rate_key = f"{user.id}:{document_type.value}:{document}"
        if not self._rate_limiter.check(rate_key):
            logger.warning(
                "identity.lookup rejected reason=rate_limited document_type=%s masked=%s",
                document_type.value,
                masked,
            )
            raise IdentityLookupUnavailableError(
                "Ha alcanzado el limite de consultas para este documento. Intente en unos minutos."
            )

        the_hash = document_hash(document_type, document)

        if not refresh:
            cached = await self._read_cache(document_type, the_hash)
            if cached is not None:
                await self._record_daily(cache_hit=True)
                self._log(
                    document_type,
                    masked,
                    cached["provider"],
                    LookupStatus.SUCCESS,
                    started,
                    cache_hit=True,
                    fallback_used=False,
                )
                await self._record_audit(
                    document_type,
                    masked,
                    cached["provider"],
                    LookupStatus.SUCCESS,
                    user,
                    cache_hit=True,
                )
                return cached

        result, fallback_used = await self._query_providers(document_type, document)

        if result.status is not LookupStatus.SUCCESS:
            self._log(
                document_type,
                masked,
                result.provider.value,
                result.status,
                started,
                cache_hit=False,
                fallback_used=fallback_used,
            )
            await self._record_audit(
                document_type, masked, result.provider.value, result.status, user, cache_hit=False
            )
            if result.status is LookupStatus.NOT_FOUND:
                raise IdentityLookupUnavailableError(
                    "No se encontro informacion para este documento",
                    code="IDENTITY_NOT_FOUND",
                    status_code=404,
                )
            raise IdentityLookupUnavailableError()

        assert result.data is not None  # SUCCESS siempre trae datos
        now = datetime.now(UTC)
        record = {
            "document_type": document_type.value,
            "document_number": document,
            "provider": result.provider.value,
            "cache_hit": False,
            "freshness": now,
            **result.data,
        }
        await self._write_cache(document_type, the_hash, result.provider, record, now, ttl)
        await self._record_daily(fallback_used=fallback_used)
        self._log(
            document_type,
            masked,
            result.provider.value,
            LookupStatus.SUCCESS,
            started,
            cache_hit=False,
            fallback_used=fallback_used,
        )
        await self._record_audit(
            document_type,
            masked,
            result.provider.value,
            LookupStatus.SUCCESS,
            user,
            cache_hit=False,
        )
        return record

    async def _query_providers(
        self, document_type: IdentityDocumentType, document: str
    ) -> tuple[ProviderLookupResult, bool]:
        primary_result = await self._ask(self._primary, document_type, document)

        if primary_result is not None and primary_result.status is LookupStatus.SUCCESS:
            return primary_result, False

        should_failover = primary_result is None or (
            primary_result.status in _FAILOVER_STATUSES
            or (primary_result.status is LookupStatus.NOT_FOUND and self._fallback_on_not_found)
        )
        if not should_failover:
            assert primary_result is not None
            return primary_result, False

        secondary_result = await self._ask(self._secondary, document_type, document)
        if secondary_result is not None:
            return secondary_result, True

        # Ninguno de los dos esta configurado o ambos fallaron sin resultado.
        fallback = primary_result or ProviderLookupResult(
            LookupStatus.PROVIDER_ERROR, ProviderName.PERU_API
        )
        return fallback, primary_result is not None

    async def _ask(
        self,
        provider: IdentityProvider | None,
        document_type: IdentityDocumentType,
        document: str,
    ) -> ProviderLookupResult | None:
        if provider is None:
            return None
        name = provider.name
        if _circuit_breaker.is_open(name):
            logger.info("identity.lookup circuit_open provider=%s", name.value)
            return None

        if document_type is IdentityDocumentType.DNI:
            result = await provider.lookup_dni(document)
        else:
            result = await provider.lookup_ruc(document)

        if result.status in _FAILOVER_STATUSES:
            _circuit_breaker.record_failure(name)
        else:
            _circuit_breaker.record_success(name)

        await self._record_provider_metric(name, result.status)
        return result

    # -- Cache ------------------------------------------------------------
    async def _read_cache(
        self, document_type: IdentityDocumentType, the_hash: str
    ) -> dict[str, Any] | None:
        row = (
            await self._session.execute(
                select(IdentityLookupCache).where(
                    IdentityLookupCache.document_type == document_type,
                    IdentityLookupCache.document_hash == the_hash,
                )
            )
        ).scalar_one_or_none()
        if row is None or row.expires_at <= datetime.now(UTC):
            return None
        return {
            **row.payload,
            "provider": row.provider.value,
            "cache_hit": True,
            "freshness": row.fetched_at,
        }

    async def _write_cache(
        self,
        document_type: IdentityDocumentType,
        the_hash: str,
        provider: ProviderName,
        record: dict[str, Any],
        fetched_at: datetime,
        ttl: timedelta,
    ) -> None:
        # Solo los campos normalizados del contrato publico: nunca la
        # respuesta cruda del proveedor, y nunca mas de lo que hace falta
        # para volver a servir la misma respuesta.
        payload = {
            k: v
            for k, v in record.items()
            if k not in {"document_type", "provider", "cache_hit", "freshness"}
        }
        stmt = (
            pg_insert(IdentityLookupCache)
            .values(
                document_type=document_type,
                document_hash=the_hash,
                provider=provider,
                payload=payload,
                fetched_at=fetched_at,
                expires_at=fetched_at + ttl,
            )
            .on_conflict_do_update(
                index_elements=[
                    IdentityLookupCache.document_type,
                    IdentityLookupCache.document_hash,
                ],
                set_={
                    "provider": provider,
                    "payload": payload,
                    "fetched_at": fetched_at,
                    "expires_at": fetched_at + ttl,
                },
            )
        )
        await self._session.execute(stmt)
        await self._session.commit()

    # -- Ubigeo -------------------------------------------------------------
    async def _resolve_ubigeo(self, code: str | None) -> dict[str, str | None]:
        """Resuelve contra el catalogo territorial existente, no uno nuevo.

        Si el codigo que trae el proveedor no coincide con ningun distrito
        conocido, se devuelven los tres campos en ``None``: el valor crudo ya
        quedo en ``ubigeo`` y no se inserta un distrito nuevo por una consulta
        externa (§29 del encargo).
        """
        if not code:
            return {"department": None, "province": None, "district": None}
        district = (
            await self._session.execute(select(UbigeoDistrict).where(UbigeoDistrict.code == code))
        ).scalar_one_or_none()
        if district is None:
            return {"department": None, "province": None, "district": None}
        return {
            "department": district.department_name,
            "province": district.province_name,
            "district": district.district_name,
        }

    # -- Metricas y auditoria ------------------------------------------------
    async def _record_provider_metric(self, provider: ProviderName, status: LookupStatus) -> None:
        column = {
            LookupStatus.SUCCESS: "success",
            LookupStatus.NOT_FOUND: "not_found",
            LookupStatus.RATE_LIMITED: "rate_limited",
            LookupStatus.TIMEOUT: "timeouts",
            LookupStatus.PROVIDER_ERROR: "provider_errors",
        }.get(status)
        today = date.today()
        increments: dict[str, int] = {"requests": 1}
        if column:
            increments[column] = 1
        stmt = (
            pg_insert(IdentityLookupProviderMetric)
            .values(provider=provider, event_date=today, **increments)
            .on_conflict_do_update(
                index_elements=[
                    IdentityLookupProviderMetric.provider,
                    IdentityLookupProviderMetric.event_date,
                ],
                set_={
                    key: getattr(IdentityLookupProviderMetric, key) + value
                    for key, value in increments.items()
                },
            )
        )
        await self._session.execute(stmt)
        await self._session.commit()

    async def _record_daily(self, *, cache_hit: bool = False, fallback_used: bool = False) -> None:
        if not cache_hit and not fallback_used:
            return
        today = date.today()
        increments = {
            "cache_hits": 1 if cache_hit else 0,
            "fallback_used": 1 if fallback_used else 0,
        }
        stmt = (
            pg_insert(IdentityLookupDailyStat)
            .values(event_date=today, **increments)
            .on_conflict_do_update(
                index_elements=[IdentityLookupDailyStat.event_date],
                set_={
                    key: getattr(IdentityLookupDailyStat, key) + value
                    for key, value in increments.items()
                },
            )
        )
        await self._session.execute(stmt)
        await self._session.commit()

    async def _record_audit(
        self,
        document_type: IdentityDocumentType,
        masked: str,
        provider: str | None,
        status: LookupStatus,
        user: AuthenticatedUser,
        *,
        cache_hit: bool,
    ) -> None:
        self._session.add(
            IdentityLookupAuditEvent(
                document_type=document_type,
                masked_document=masked,
                provider=provider,
                status=status.value,
                cache_hit=cache_hit,
                user_id=user.id,
            )
        )
        await self._session.commit()

    def _log(
        self,
        document_type: IdentityDocumentType,
        masked: str,
        provider: str | None,
        status: LookupStatus,
        started: float,
        *,
        cache_hit: bool,
        fallback_used: bool,
    ) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "identity.lookup provider=%s document_type=%s masked=%s status=%s "
            "duration_ms=%d cache_hit=%s fallback_used=%s",
            provider,
            document_type.value,
            masked,
            status.value,
            duration_ms,
            cache_hit,
            fallback_used,
        )


__all__ = ["IdentityLookupMetrics", "IdentityLookupService", "_InMemoryRateLimiter"]
