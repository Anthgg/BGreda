"""El modelo ORM y la migracion 0001 deben describir el mismo esquema."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models.profile import Profile, UserRole


def _ddl() -> str:
    return str(CreateTable(Profile.__table__).compile(dialect=postgresql.dialect()))


def test_la_tabla_se_llama_profiles() -> None:
    assert Profile.__tablename__ == "profiles"


def test_las_columnas_son_las_esperadas() -> None:
    assert set(Profile.__table__.columns.keys()) == {
        "id",
        "display_name",
        "role",
        "active",
        "created_at",
        "updated_at",
    }


def test_la_clave_primaria_es_el_uuid_del_usuario() -> None:
    ddl = _ddl()

    assert "id UUID NOT NULL" in ddl
    assert "CONSTRAINT pk_profiles PRIMARY KEY (id)" in ddl


def test_los_nombres_de_constraint_siguen_la_convencion() -> None:
    """Coinciden con los que emite la migracion 0001."""
    ddl = _ddl()

    assert "CONSTRAINT ck_profiles_role_allowed" in ddl
    assert "CONSTRAINT ck_profiles_display_name_not_blank" in ddl


def test_los_valores_por_defecto_coinciden_con_la_migracion() -> None:
    ddl = _ddl()

    assert "role VARCHAR(20) DEFAULT 'OPERATOR' NOT NULL" in ddl
    assert "active BOOLEAN DEFAULT true NOT NULL" in ddl


def test_las_marcas_de_tiempo_son_con_zona_horaria() -> None:
    ddl = _ddl()

    assert ddl.count("TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL") == 2


def test_los_roles_iniciales_son_admin_y_operator() -> None:
    assert {role.value for role in UserRole} == {"ADMIN", "OPERATOR"}
