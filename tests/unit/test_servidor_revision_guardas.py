"""Las guardias del backend de revision de las E2E (Fase 010G).

`tests/e2e/servidor_revision.py` arranca la aplicacion real con la autenticacion
SIMULADA. Si alguna vez apuntara a una base remota, un «usuario» inventado
tendria permisos de administrador sobre datos de verdad. Estas pruebas fijan que
eso no puede pasar por accidente.

La primera version miraba el host de la cadena con `urlparse`, y la revision de
Codex encontro que `?host=` lo burlaba: el `hostname` visible era `localhost`
mientras asyncpg se conectaba a otro sitio. Por eso las guardias preguntan al
propio dialecto de SQLAlchemy a donde se conectaria, y por eso los vectores de
aqui son los que se probaron contra esa correccion.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from tests.e2e import servidor_revision

LOCAL = "postgresql://greda:greda@localhost:5432/greda_e2e"


@pytest.fixture(autouse=True)
def _entorno_limpio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada prueba parte de un entorno conocido y de una configuracion sin cache."""
    monkeypatch.setenv("GREDA_E2E_REVISION", "1")
    monkeypatch.setenv("DATABASE_URL", LOCAL)
    monkeypatch.setenv("E2E_EMAIL", "e2e@example.com")
    monkeypatch.setenv("E2E_PASSWORD", "aleatoria")
    get_settings.cache_clear()


def _rechaza() -> None:
    get_settings.cache_clear()
    with pytest.raises(SystemExit):
        servidor_revision._comprobar_entorno()


class TestLaBanderaEsObligatoria:
    def test_sin_bandera_no_arranca(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GREDA_E2E_REVISION")
        _rechaza()

    def test_una_bandera_distinta_de_1_no_vale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GREDA_E2E_REVISION", "true")
        _rechaza()


class TestLaBaseTieneQueSerLocal:
    @pytest.mark.parametrize(
        "url",
        [
            # El que encontro Codex: el hostname visible es localhost.
            "postgresql://u:p@localhost:5432/db?host=db.remoto.example",
            "postgresql://u:p@localhost:5432/db?host=localhost:5432&host=db.remoto:5432",
            "postgresql://u:p@localhost.evil.com:5432/db",
            "postgres://u:p@db.produccion.example:5432/db",
            "postgresql+asyncpg://u:p@10.0.0.5:5432/db",
            # Sin host explicito asyncpg cae en PGHOST o en un socket.
            "postgresql://u:p@/db",
        ],
    )
    def test_rechaza_lo_que_no_conecta_solo_a_localhost(
        self, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", url)
        _rechaza()

    @pytest.mark.parametrize(
        "parametro",
        ["hostaddr=10.0.0.5", "service=produccion", "passfile=/tmp/pgpass", "ssl=require"],
    )
    def test_rechaza_parametros_de_conexion_fuera_de_la_lista_blanca(
        self, monkeypatch: pytest.MonkeyPatch, parametro: str
    ) -> None:
        """Todo parametro de la query llega a asyncpg: una base de pruebas no necesita ninguno."""
        monkeypatch.setenv("DATABASE_URL", f"{LOCAL}?{parametro}")
        _rechaza()

    def test_sin_url_no_arranca(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "")
        _rechaza()

    @pytest.mark.parametrize(
        "url",
        [
            LOCAL,
            "postgresql+asyncpg://greda:greda@127.0.0.1:5432/greda_e2e",
            "postgresql://greda:greda@[::1]:5432/greda_e2e",
        ],
    )
    def test_acepta_localhost(self, monkeypatch: pytest.MonkeyPatch, url: str) -> None:
        monkeypatch.setenv("DATABASE_URL", url)
        get_settings.cache_clear()
        assert servidor_revision._comprobar_entorno() == ("e2e@example.com", "aleatoria")


class TestLasCredencialesLlegan:
    @pytest.mark.parametrize("variable", ["E2E_EMAIL", "E2E_PASSWORD"])
    def test_sin_credenciales_no_arranca(
        self, monkeypatch: pytest.MonkeyPatch, variable: str
    ) -> None:
        monkeypatch.delenv(variable)
        _rechaza()


class TestLosHostsSonLosDelDialecto:
    def test_ve_el_host_de_la_query_y_no_el_de_la_cadena(self) -> None:
        """La razon de preguntar al dialecto y no a `urlparse`."""
        hosts = servidor_revision.hosts_de_conexion(
            "postgresql://u:p@localhost:5432/db?host=db.remoto.example"
        )
        assert hosts == ["db.remoto.example"]
