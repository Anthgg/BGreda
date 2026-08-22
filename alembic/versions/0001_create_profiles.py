"""Crea la tabla profiles y la blinda frente a la API publica de Supabase.

Revision ID: 0001
Revises:
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Supabase expone automaticamente el esquema "public" a traves de PostgREST con
# la publishable key. Una tabla sin RLS quedaria legible por cualquiera que
# posea esa clave. Se habilita RLS y NO se crea ninguna policy: con RLS activo
# y sin policies, los roles anon/authenticated no pueden leer ni escribir nada.
# El backend accede con la credencial de DATABASE_URL, propietaria de la tabla,
# y la autorizacion real la aplica FastAPI.
#
# Deliberadamente NO se usa FORCE ROW LEVEL SECURITY: aplicaria RLS tambien al
# propietario, es decir al propio backend, que quedaria sin acceso salvo que su
# rol tuviese BYPASSRLS. No aporta seguridad frente a PostgREST y si introduce
# un fallo silencioso al cambiar de rol de conexion.
#
# Cada sentencia va en su propio execute: asyncpg usa sentencias preparadas y
# no admite varias sentencias en una sola llamada.
_REVOKE_ROLE_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        REVOKE ALL ON TABLE public.profiles FROM {role};
    END IF;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "profiles",
        # Coincide con auth.users.id de Supabase. La correspondencia la
        # garantiza el backend a partir de un token ya verificado; no se
        # declara clave foranea para no acoplar la migracion al esquema
        # interno de Supabase.
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=20), server_default="OPERATOR", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Los nombres van sin prefijo: la convencion de Base.metadata anade
        # "ck_<tabla>_" automaticamente.
        sa.CheckConstraint("role IN ('ADMIN', 'OPERATOR')", name="role_allowed"),
        sa.CheckConstraint("length(btrim(display_name)) > 0", name="display_name_not_blank"),
        sa.PrimaryKeyConstraint("id", name="pk_profiles"),
    )
    op.create_index("ix_profiles_active", "profiles", ["active"])

    op.execute("ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY")
    for role in ("anon", "authenticated"):
        op.execute(_REVOKE_ROLE_SQL.format(role=role))


def downgrade() -> None:
    op.execute("ALTER TABLE public.profiles DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_profiles_active", table_name="profiles")
    op.drop_table("profiles")
