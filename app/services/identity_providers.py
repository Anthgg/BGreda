"""Adaptadores de proveedor para la consulta de DNI/RUC.

## Aviso de verificacion pendiente

Los nombres de campo y las rutas de Peru API y Decolecta que usa este modulo
se han escrito con la mejor informacion disponible sin acceso a la
documentacion en vivo en el momento de escribirlo. **Antes de habilitar esto
en produccion hace falta contrastar ``_parse_dni`` y ``_parse_ruc`` de cada
proveedor contra una respuesta real.** Por eso el mapeo crudo-a-normalizado
esta aislado en un unico metodo por proveedor: corregir un nombre de campo
tras la verificacion es un cambio de una linea, no una reescritura.

## Diseno

Cada proveedor implementa :class:`IdentityProvider`. Ninguno lanza una
excepcion por un documento inexistente o un limite de cuota: ambos son
resultados normales del dominio (:class:`~app.core.identity.LookupStatus`),
no fallos de programa. Solo un fallo de red o una respuesta que no se puede
interpretar se traduce a ``PROVIDER_ERROR``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.identity import IdentityDocumentType, LookupStatus, ProviderName


@dataclass(frozen=True, slots=True)
class ProviderLookupResult:
    """Resultado crudo de un proveedor, ya en el vocabulario del dominio.

    ``data`` solo se rellena en ``SUCCESS`` y contiene exactamente los campos
    normalizados del contrato publico (ver ``app.schemas.identity``), listos
    para que el servicio los combine con los metadatos de cache/proveedor.
    """

    status: LookupStatus
    provider: ProviderName
    data: dict[str, Any] | None = None


class IdentityProvider(ABC):
    """Contrato que cualquier proveedor de identidad debe cumplir."""

    name: ProviderName

    @abstractmethod
    async def lookup_dni(self, document: str) -> ProviderLookupResult: ...

    @abstractmethod
    async def lookup_ruc(self, document: str) -> ProviderLookupResult: ...


def _classify_http_error(
    exc: httpx.HTTPStatusError, provider: ProviderName
) -> ProviderLookupResult:
    status = exc.response.status_code
    if status == 404:
        return ProviderLookupResult(LookupStatus.NOT_FOUND, provider)
    if status == 429:
        return ProviderLookupResult(LookupStatus.RATE_LIMITED, provider)
    return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, provider)


class PeruApiProvider(IdentityProvider):
    """Adaptador de Peru API (https://peruapi.com/documentacion).

    Primario: ambos documentos soportados en el plan observado, con cuota
    diaria y mensual (ver ``app.core.config`` para los valores por omision,
    puramente informativos y no una regla de negocio rigida).
    """

    name = ProviderName.PERU_API

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token

    async def lookup_dni(self, document: str) -> ProviderLookupResult:
        try:
            response = await self._client.get(
                "/v1/dni",
                params={"numero": document},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            return ProviderLookupResult(LookupStatus.TIMEOUT, self.name)
        except httpx.HTTPStatusError as exc:
            return _classify_http_error(exc, self.name)
        except (httpx.HTTPError, ValueError):
            return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
        return self._parse_dni(response)

    async def lookup_ruc(self, document: str) -> ProviderLookupResult:
        try:
            response = await self._client.get(
                "/v1/ruc",
                params={"numero": document},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            return ProviderLookupResult(LookupStatus.TIMEOUT, self.name)
        except httpx.HTTPStatusError as exc:
            return _classify_http_error(exc, self.name)
        except (httpx.HTTPError, ValueError):
            return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
        return self._parse_ruc(response)

    def _parse_dni(self, response: httpx.Response) -> ProviderLookupResult:
        try:
            body = response.json()
            if not isinstance(body, dict):
                return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
            if not body or not body.get("nombres"):
                return ProviderLookupResult(LookupStatus.NOT_FOUND, self.name)
            nombres = str(body["nombres"]).strip()
            paterno = str(body.get("apellido_paterno") or "").strip() or None
            materno = str(body.get("apellido_materno") or "").strip() or None
            completo = " ".join(p for p in (nombres, paterno, materno) if p)
            data = {
                "full_name": completo,
                "first_names": nombres or None,
                "paternal_surname": paterno,
                "maternal_surname": materno,
            }
        except (KeyError, TypeError, ValueError):
            return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
        return ProviderLookupResult(LookupStatus.SUCCESS, self.name, data)

    def _parse_ruc(self, response: httpx.Response) -> ProviderLookupResult:
        try:
            body = response.json()
            if not isinstance(body, dict):
                return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
            razon = body.get("razon_social")
            if not body or not razon:
                return ProviderLookupResult(LookupStatus.NOT_FOUND, self.name)
            data = {
                "business_name": str(razon).strip(),
                "status": _clean(body.get("estado")),
                "condition": _clean(body.get("condicion")),
                "address": _clean(body.get("direccion")),
                "ubigeo": _clean(body.get("ubigeo")),
            }
        except (KeyError, TypeError, ValueError):
            return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
        return ProviderLookupResult(LookupStatus.SUCCESS, self.name, data)


class DecolectaProvider(IdentityProvider):
    """Adaptador de Decolecta (https://decolecta.gitbook.io/docs).

    Secundario: se llama solo cuando el primario falla o excede su cuota, no
    en paralelo. Su cuota mensual observada es la mas ajustada de las dos.
    """

    name = ProviderName.DECOLECTA

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token

    async def lookup_dni(self, document: str) -> ProviderLookupResult:
        try:
            response = await self._client.get(
                "/v1/reniec/dni",
                params={"numero": document},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            return ProviderLookupResult(LookupStatus.TIMEOUT, self.name)
        except httpx.HTTPStatusError as exc:
            return _classify_http_error(exc, self.name)
        except (httpx.HTTPError, ValueError):
            return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
        return self._parse_dni(response)

    async def lookup_ruc(self, document: str) -> ProviderLookupResult:
        try:
            response = await self._client.get(
                "/v1/sunat/ruc",
                params={"numero": document},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            return ProviderLookupResult(LookupStatus.TIMEOUT, self.name)
        except httpx.HTTPStatusError as exc:
            return _classify_http_error(exc, self.name)
        except (httpx.HTTPError, ValueError):
            return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
        return self._parse_ruc(response)

    def _parse_dni(self, response: httpx.Response) -> ProviderLookupResult:
        try:
            body = response.json()
            if not isinstance(body, dict):
                return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
            completo = body.get("full_name")
            if not body or not completo:
                return ProviderLookupResult(LookupStatus.NOT_FOUND, self.name)
            data = {
                "full_name": str(completo).strip(),
                "first_names": _clean(body.get("first_name")),
                "paternal_surname": _clean(body.get("first_last_name")),
                "maternal_surname": _clean(body.get("second_last_name")),
            }
        except (KeyError, TypeError, ValueError):
            return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
        return ProviderLookupResult(LookupStatus.SUCCESS, self.name, data)

    def _parse_ruc(self, response: httpx.Response) -> ProviderLookupResult:
        try:
            body = response.json()
            if not isinstance(body, dict):
                return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
            razon = body.get("razon_social")
            if not body or not razon:
                return ProviderLookupResult(LookupStatus.NOT_FOUND, self.name)
            data = {
                "business_name": str(razon).strip(),
                "status": _clean(body.get("estado")),
                "condition": _clean(body.get("condicion")),
                "address": _clean(body.get("direccion")),
                "ubigeo": _clean(body.get("ubigeo")),
            }
        except (KeyError, TypeError, ValueError):
            return ProviderLookupResult(LookupStatus.PROVIDER_ERROR, self.name)
        return ProviderLookupResult(LookupStatus.SUCCESS, self.name, data)


def _clean(value: Any) -> str | None:
    """Cadena recortada o ``None``. Nunca inventa un valor que falta."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "DecolectaProvider",
    "IdentityDocumentType",
    "IdentityProvider",
    "PeruApiProvider",
    "ProviderLookupResult",
]
