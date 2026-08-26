"""Fase 5.5: cache y metricas de la consulta de identidad (DNI/RUC).

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-24

Crea las tablas de la consulta de identidad:

- ``identity_lookup_cache``: ultima respuesta normalizada por documento,
  indexada por su hash (nunca el documento en claro) y con TTL propio.
- ``identity_lookup_provider_metrics``: contadores diarios por proveedor,
  observabilidad interna, nunca la cuota oficial (esa la tiene el proveedor).
- ``identity_lookup_daily_stats``: aciertos de cache y usos de fallback del
  dia, sin desglosar por proveedor.
- ``identity_lookup_audit_events``: rastro minimo de consultas con el
  documento enmascarado, no el registro de auditoria general (ese sigue
  siendo para cambios en el tercero).

Ninguna tabla siembra datos: no hay catalogo oficial que transcribir aqui, a
diferencia de 0007/0008.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = (
    "identity_lookup_cache",
    "identity_lookup_provider_metrics",
    "identity_lookup_daily_stats",
    "identity_lookup_audit_events",
)

_REVOKE_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        REVOKE ALL ON TABLE public.{table} FROM {role};
    END IF;
END
$$
"""


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. identity_lookup_cache
    # ------------------------------------------------------------------
    op.create_table(
        "identity_lookup_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_type", sa.String(length=8), nullable=False),
        sa.Column("document_hash", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint(
            "document_type IN ('DNI', 'RUC')", name="ck_identity_lookup_cache_document_type"
        ),
        sa.CheckConstraint(
            "provider IN ('PERU_API', 'DECOLECTA')", name="ck_identity_lookup_cache_provider"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_identity_lookup_cache"),
        sa.UniqueConstraint("document_type", "document_hash", name="uq_identity_lookup_cache_hash"),
    )
    op.create_index("ix_identity_lookup_cache_expires_at", "identity_lookup_cache", ["expires_at"])

    # ------------------------------------------------------------------
    # 2. identity_lookup_provider_metrics
    # ------------------------------------------------------------------
    op.create_table(
        "identity_lookup_provider_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("requests", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("success", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("not_found", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rate_limited", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("timeouts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("provider_errors", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.CheckConstraint(
            "provider IN ('PERU_API', 'DECOLECTA')",
            name="ck_identity_lookup_provider_metrics_provider",
        ),
        sa.CheckConstraint(
            "requests >= 0", name="ck_identity_lookup_provider_metrics_requests_not_negative"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_identity_lookup_provider_metrics"),
        sa.UniqueConstraint("provider", "event_date", name="uq_identity_provider_metrics_day"),
    )

    # ------------------------------------------------------------------
    # 3. identity_lookup_daily_stats
    # ------------------------------------------------------------------
    op.create_table(
        "identity_lookup_daily_stats",
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("cache_hits", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("fallback_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.PrimaryKeyConstraint("event_date", name="pk_identity_lookup_daily_stats"),
    )

    # ------------------------------------------------------------------
    # 4. identity_lookup_audit_events
    # ------------------------------------------------------------------
    op.create_table(
        "identity_lookup_audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_type", sa.String(length=8), nullable=False),
        sa.Column("masked_document", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "document_type IN ('DNI', 'RUC')",
            name="ck_identity_lookup_audit_events_document_type",
        ),
        sa.CheckConstraint(
            "provider IS NULL OR provider IN ('PERU_API', 'DECOLECTA')",
            name="ck_identity_lookup_audit_events_provider",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_identity_lookup_audit_events"),
    )
    op.create_index(
        "ix_identity_lookup_audit_events_created_at",
        "identity_lookup_audit_events",
        ["created_at"],
    )

    # ------------------------------------------------------------------
    # 5. Seguridad: RLS habilitado sin FORCE, sin policies publicas
    # ------------------------------------------------------------------
    for table in NEW_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;")
        for role in ("anon", "authenticated"):
            op.execute(_REVOKE_SQL.format(table=table, role=role))


def downgrade() -> None:
    op.drop_table("identity_lookup_audit_events")
    op.drop_table("identity_lookup_daily_stats")
    op.drop_table("identity_lookup_provider_metrics")
    op.drop_table("identity_lookup_cache")
