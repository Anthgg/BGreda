"""Catalogos controlados de moneda, ubigeo y formatos documentales.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-22
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
NEW_TABLES = ("currency_catalog", "ubigeo_districts", "sequence_pattern_presets")

_REVOKE_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
        REVOKE ALL ON TABLE public.{table} FROM {role};
    END IF;
END
$$
"""


def _lock_down(table: str) -> None:
    op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    for role in ("anon", "authenticated"):
        op.execute(_REVOKE_SQL.format(role=role, table=table))


def _read_csv(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _chunks(rows: list[dict[str, object]], size: int = 500) -> Iterable[list[dict[str, object]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _seed_catalogs() -> None:
    currency_table = sa.table(
        "currency_catalog",
        sa.column("code", sa.String),
        sa.column("numeric_code", sa.String),
        sa.column("name", sa.String),
        sa.column("symbol", sa.String),
        sa.column("minor_units", sa.SmallInteger),
    )
    currencies: list[dict[str, object]] = []
    for row in _read_csv("iso_4217_2026.csv"):
        currencies.append(
            {
                **row,
                "minor_units": int(row["minor_units"]) if row["minor_units"] else None,
            }
        )
    op.bulk_insert(currency_table, currencies)

    ubigeo_table = sa.table(
        "ubigeo_districts",
        sa.column("code", sa.String),
        sa.column("department_code", sa.String),
        sa.column("department_name", sa.String),
        sa.column("province_code", sa.String),
        sa.column("province_name", sa.String),
        sa.column("district_name", sa.String),
    )
    ubigeos: list[dict[str, object]] = list(_read_csv("ubigeo_inei_2022.csv"))
    for batch in _chunks(ubigeos):
        op.bulk_insert(ubigeo_table, batch)

    pattern_table = sa.table(
        "sequence_pattern_presets",
        sa.column("name", sa.String),
        sa.column("pattern", sa.String),
        sa.column("is_system", sa.Boolean),
    )
    op.bulk_insert(
        pattern_table,
        [
            {
                "name": "Prefijo - ano - numero",
                "pattern": "{PREFIX}-{YYYY}-{NUMBER}",
                "is_system": True,
            },
            {
                "name": "Prefijo - ano corto - numero",
                "pattern": "{PREFIX}-{YY}-{NUMBER}",
                "is_system": True,
            },
            {
                "name": "Prefijo - ano y mes - numero",
                "pattern": "{PREFIX}-{YYYY}{MM}-{NUMBER}",
                "is_system": True,
            },
            {
                "name": "Prefijo - numero",
                "pattern": "{PREFIX}-{NUMBER}",
                "is_system": True,
            },
        ],
    )


def upgrade() -> None:
    op.create_table(
        "currency_catalog",
        sa.Column("code", sa.String(length=3), nullable=False),
        sa.Column("numeric_code", sa.String(length=3), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("symbol", sa.String(length=8), nullable=False),
        sa.Column("minor_units", sa.SmallInteger(), nullable=True),
        sa.CheckConstraint("code ~ '^[A-Z]{3}$'", name="code_iso4217"),
        sa.CheckConstraint("numeric_code ~ '^[0-9]{3}$'", name="numeric_code_digits"),
        sa.CheckConstraint("length(btrim(symbol)) > 0", name="symbol_not_blank"),
        sa.CheckConstraint(
            "minor_units IS NULL OR minor_units BETWEEN 0 AND 9",
            name="minor_units_range",
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_currency_catalog")),
        sa.UniqueConstraint("numeric_code", name=op.f("uq_currency_catalog_numeric_code")),
    )
    op.create_table(
        "ubigeo_districts",
        sa.Column("code", sa.String(length=6), nullable=False),
        sa.Column("department_code", sa.String(length=2), nullable=False),
        sa.Column("department_name", sa.String(length=80), nullable=False),
        sa.Column("province_code", sa.String(length=4), nullable=False),
        sa.Column("province_name", sa.String(length=100), nullable=False),
        sa.Column("district_name", sa.String(length=120), nullable=False),
        sa.CheckConstraint("code ~ '^[0-9]{6}$'", name="code_digits"),
        sa.CheckConstraint("department_code = left(code, 2)", name="department_matches_code"),
        sa.CheckConstraint("province_code = left(code, 4)", name="province_matches_code"),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_ubigeo_districts")),
    )
    op.create_index(
        "ix_ubigeo_districts_department",
        "ubigeo_districts",
        ["department_code", "department_name"],
        unique=False,
    )
    op.create_index(
        "ix_ubigeo_districts_province",
        "ubigeo_districts",
        ["province_code", "province_name"],
        unique=False,
    )
    op.create_table(
        "sequence_pattern_presets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("pattern", sa.String(length=120), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint("position('{NUMBER}' in pattern) > 0", name="pattern_has_number"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sequence_pattern_presets")),
        sa.UniqueConstraint("name", name=op.f("uq_sequence_pattern_presets_name")),
        sa.UniqueConstraint("pattern", name=op.f("uq_sequence_pattern_presets_pattern")),
    )

    for table in NEW_TABLES:
        _lock_down(table)
    _seed_catalogs()

    # El simbolo deja de ser texto libre: se canoniza antes de crear la FK.
    op.execute(
        """
        UPDATE public.commercial_settings AS settings
        SET currency_symbol = catalog.symbol
        FROM public.currency_catalog AS catalog
        WHERE settings.currency_code = catalog.code
        """
    )
    op.create_foreign_key(
        "fk_commercial_settings_currency_code_currency_catalog",
        "commercial_settings",
        "currency_catalog",
        ["currency_code"],
        ["code"],
        ondelete="RESTRICT",
    )

    op.add_column("company_settings", sa.Column("ubigeo_code", sa.String(length=6)))
    # Si ya habia una direccion completa y coincide de forma exacta, se enlaza
    # sin pedir que el usuario vuelva a capturarla. Valores parciales se
    # conservan y la interfaz solicitara una seleccion valida al editarlos.
    op.execute(
        """
        UPDATE public.company_settings AS settings
        SET ubigeo_code = catalog.code,
            district = catalog.district_name,
            province = catalog.province_name,
            department = catalog.department_name,
            country = 'Peru'
        FROM public.ubigeo_districts AS catalog
        WHERE upper(btrim(settings.district)) = catalog.district_name
          AND upper(btrim(settings.province)) = catalog.province_name
          AND upper(btrim(settings.department)) = catalog.department_name
        """
    )
    op.create_foreign_key(
        "fk_company_settings_ubigeo_code_ubigeo_districts",
        "company_settings",
        "ubigeo_districts",
        ["ubigeo_code"],
        ["code"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_company_settings_ubigeo_code_ubigeo_districts",
        "company_settings",
        type_="foreignkey",
    )
    op.drop_column("company_settings", "ubigeo_code")
    op.drop_constraint(
        "fk_commercial_settings_currency_code_currency_catalog",
        "commercial_settings",
        type_="foreignkey",
    )
    op.drop_table("sequence_pattern_presets")
    op.drop_index("ix_ubigeo_districts_province", table_name="ubigeo_districts")
    op.drop_index("ix_ubigeo_districts_department", table_name="ubigeo_districts")
    op.drop_table("ubigeo_districts")
    op.drop_table("currency_catalog")
