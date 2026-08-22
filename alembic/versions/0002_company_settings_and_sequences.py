"""Fase 2: configuracion de empresa, parametros comerciales y secuencias.

Revision ID: 9becb580f293
Revises: 0001
Create Date: 2026-08-21 23:19:27.632300
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Mismo blindaje que la migracion 0001: Supabase publica el esquema "public"
# por PostgREST con la publishable key, de modo que una tabla sin RLS quedaria
# legible por cualquiera que tenga esa clave. Con RLS activo y sin policies,
# anon y authenticated no pueden leer ni escribir nada. El backend accede con
# el rol propietario y la autorizacion real la aplica FastAPI.
NEW_TABLES = (
    "audit_events",
    "bank_accounts",
    "commercial_settings",
    "company_settings",
    "document_sequence_issues",
    "document_sequences",
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

# Formato aprobado en el Plan v1.2 seccion 2.6: CTZ-AAAA-NNNNNN para
# cotizaciones y HR-AAAA-NNNNNN para quemas, con reinicio anual. Es
# configuracion documentada por la fuente, no un dato de negocio inventado.
DEFAULT_PATTERN = "{PREFIX}-{YYYY}-{NUMBER}"
SEED_SEQUENCES = (
    ("QUOTE", "CTZ"),
    ("FIRING", "HR"),
)


def _lock_down(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    for role in ("anon", "authenticated"):
        op.execute(_REVOKE_SQL.format(role=role, table=table))


def _seed() -> None:
    """Crea las filas singleton y las secuencias con su formato aprobado.

    No se precarga ningun dato de empresa: razon social, RUC, banco y correo
    quedan nulos hasta que el usuario los complete. Las fuentes del proyecto no
    los proporcionan y no se inventan.
    """
    op.execute("INSERT INTO public.company_settings (id) VALUES (1)")
    op.execute("INSERT INTO public.commercial_settings (id) VALUES (1)")
    for sequence_type, prefix in SEED_SEQUENCES:
        op.execute(
            sa.text(
                """
                INSERT INTO public.document_sequences
                    (sequence_type, prefix, pattern, padding, reset_policy,
                     current_value, period_key, active)
                VALUES
                    (:sequence_type, :prefix, :pattern, 6, 'YEARLY', 0, '', true)
                """
            ).bindparams(sequence_type=sequence_type, prefix=prefix, pattern=DEFAULT_PATTERN)
        )


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("field", sa.String(length=80), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("user_display_name", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "action IN ('CREATE', 'UPDATE', 'DELETE')", name=op.f("ck_audit_events_action_allowed")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"], unique=False)
    op.create_index(
        "ix_audit_events_entity",
        "audit_events",
        ["entity_type", "entity_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "commercial_settings",
        sa.Column("currency_code", sa.String(length=3), nullable=True),
        sa.Column("currency_symbol", sa.String(length=8), nullable=True),
        sa.Column("tax_percent", sa.Numeric(precision=9, scale=6), nullable=True),
        sa.Column("quote_validity_days", sa.Integer(), nullable=True),
        sa.Column("general_conditions", sa.Text(), nullable=True),
        sa.Column("payment_notes", sa.Text(), nullable=True),
        sa.Column("document_footer", sa.Text(), nullable=True),
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
            "currency_code IS NULL OR currency_code ~ '^[A-Z]{3}$'",
            name=op.f("ck_commercial_settings_currency_code_iso4217"),
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_commercial_settings_singleton")),
        sa.CheckConstraint(
            "quote_validity_days IS NULL OR "
            "(quote_validity_days > 0 AND quote_validity_days <= 3650)",
            name=op.f("ck_commercial_settings_quote_validity_range"),
        ),
        sa.CheckConstraint(
            "tax_percent IS NULL OR (tax_percent >= 0 AND tax_percent <= 100)",
            name=op.f("ck_commercial_settings_tax_percent_range"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_commercial_settings_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commercial_settings")),
    )
    op.create_table(
        "company_settings",
        sa.Column("legal_name", sa.String(length=200), nullable=True),
        sa.Column("trade_name", sa.String(length=200), nullable=True),
        sa.Column("tax_id", sa.String(length=20), nullable=True),
        sa.Column("address_line1", sa.String(length=200), nullable=True),
        sa.Column("address_line2", sa.String(length=200), nullable=True),
        sa.Column("district", sa.String(length=120), nullable=True),
        sa.Column("province", sa.String(length=120), nullable=True),
        sa.Column("department", sa.String(length=120), nullable=True),
        sa.Column("country", sa.String(length=120), nullable=True),
        sa.Column("postal_code", sa.String(length=20), nullable=True),
        sa.Column("phone", sa.String(length=40), nullable=True),
        sa.Column("mobile", sa.String(length=40), nullable=True),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("website", sa.String(length=200), nullable=True),
        sa.Column("contact_name", sa.String(length=160), nullable=True),
        sa.Column("contact_role", sa.String(length=120), nullable=True),
        sa.Column("logo_object_path", sa.String(length=400), nullable=True),
        sa.Column("logo_content_type", sa.String(length=80), nullable=True),
        sa.Column("logo_size_bytes", sa.Integer(), nullable=True),
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
        sa.CheckConstraint("id = 1", name=op.f("ck_company_settings_singleton")),
        sa.CheckConstraint("version > 0", name=op.f("ck_company_settings_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_settings")),
    )
    op.create_table(
        "document_sequence_issues",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sequence_type", sa.String(length=16), nullable=False),
        sa.Column("period_key", sa.String(length=16), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("formatted_value", sa.String(length=160), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("issued_by", sa.UUID(), nullable=True),
        sa.CheckConstraint("number > 0", name=op.f("ck_document_sequence_issues_number_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_sequence_issues")),
        sa.UniqueConstraint("formatted_value", name="unique_formatted"),
        sa.UniqueConstraint("sequence_type", "period_key", "number", name="unique_number"),
    )
    op.create_table(
        "document_sequences",
        sa.Column("id", sa.SmallInteger(), autoincrement=True, nullable=False),
        sa.Column("sequence_type", sa.String(length=16), nullable=False),
        sa.Column("prefix", sa.String(length=10), nullable=False),
        sa.Column("pattern", sa.String(length=120), nullable=False),
        sa.Column("padding", sa.SmallInteger(), nullable=False),
        sa.Column("reset_policy", sa.String(length=16), nullable=False),
        sa.Column("current_value", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("period_key", sa.String(length=16), server_default=sa.text("''"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
            "position('{NUMBER}' in pattern) > 0",
            name=op.f("ck_document_sequences_pattern_has_number"),
        ),
        sa.CheckConstraint(
            "reset_policy IN ('NEVER', 'YEARLY', 'MONTHLY', 'DAILY')",
            name=op.f("ck_document_sequences_reset_policy_allowed"),
        ),
        sa.CheckConstraint(
            "sequence_type IN ('QUOTE', 'FIRING')", name=op.f("ck_document_sequences_type_allowed")
        ),
        sa.CheckConstraint(
            "current_value >= 0", name=op.f("ck_document_sequences_current_value_not_negative")
        ),
        sa.CheckConstraint(
            "length(btrim(prefix)) > 0", name=op.f("ck_document_sequences_prefix_not_blank")
        ),
        sa.CheckConstraint(
            "padding BETWEEN 1 AND 12", name=op.f("ck_document_sequences_padding_range")
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_document_sequences_version_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_sequences")),
        sa.UniqueConstraint("sequence_type", name=op.f("uq_document_sequences_sequence_type")),
    )
    op.create_table(
        "bank_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("settings_id", sa.SmallInteger(), nullable=False),
        sa.Column("bank_name", sa.String(length=160), nullable=True),
        sa.Column("account_holder", sa.String(length=200), nullable=True),
        sa.Column("account_number", sa.String(length=64), nullable=True),
        sa.Column("cci", sa.String(length=32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["settings_id"],
            ["commercial_settings.id"],
            name=op.f("fk_bank_accounts_settings_id_commercial_settings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bank_accounts")),
    )
    op.create_index(
        "uq_bank_accounts_primary",
        "bank_accounts",
        ["is_primary"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )

    for table in NEW_TABLES:
        _lock_down(table)

    _seed()


def downgrade() -> None:
    op.drop_index(
        "uq_bank_accounts_primary",
        table_name="bank_accounts",
        postgresql_where=sa.text("is_primary"),
    )
    op.drop_table("bank_accounts")
    op.drop_table("document_sequences")
    op.drop_table("document_sequence_issues")
    op.drop_table("company_settings")
    op.drop_table("commercial_settings")
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")
