"""Fase 010C — materiales del Cotizador V2 contra PostgreSQL real.

Lo que aqui se comprueba, por orden de gravedad:

1. **cotizar no consume inventario.** Es la regla mas facil de romper sin
   notarlo y la mas cara: pedir un presupuesto no puede vaciar el almacen;
2. el snapshot sobrevive a que cambie el maestro, y el override de una
   cotizacion no toca el maestro;
3. el esmalte se elige por COSTO POR GRAMO, no por precio de envase, y sin
   stock sigue sirviendo de referencia;
4. permisos: cotizar no autoriza a valorizar materiales.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db.conftest import OPERATOR_EMAIL, OPERATOR_PASSWORD, authenticate

MATERIALS = "/api/v1/quoter-v2/materials"
V2 = "/api/v1/quotations-v2"
PRODUCTS = "/api/v1/products"
CATEGORIES = "/api/v1/categories"
INVENTORY = "/api/v1/inventory"

#: Cien kilos expresados en gramos. El maestro lleva los materiales en la
#: unidad base, y la base de estos es el gramo.
CIEN_KILOS_EN_GRAMOS = "100000"


async def _categoria(api: httpx.AsyncClient, csrf: str, nombre: str) -> int:
    response = await api.post(
        CATEGORIES, json={"name": nombre, "parent_id": None}, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def crear_producto(
    api: httpx.AsyncClient, csrf: str, nombre: str, **overrides: Any
) -> dict[str, Any]:
    categoria = overrides.pop("categoria", None) or await _categoria(api, csrf, f"Cat {nombre}")
    payload: dict[str, Any] = {
        "name": nombre,
        "product_type": "RAW_MATERIAL",
        "product_category_id": categoria,
        "base_uom_code": "g",
        "purchasable": True,
    }
    payload.update(overrides)
    response = await api.post(PRODUCTS, json=payload, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    return dict(response.json())


async def valorizar(
    api: httpx.AsyncClient, csrf: str, product_id: int, **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "material_kind": "BODY",
        "origin": "PURCHASE",
        "purchase_quantity": CIEN_KILOS_EN_GRAMOS,
        "purchase_cost": "100",
        "transport_cost": "30",
    }
    payload.update(overrides)
    response = await api.put(
        f"{MATERIALS}/{product_id}", json=payload, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def crear_cotizacion(api: httpx.AsyncClient, csrf: str) -> int:
    response = await api.post(V2, json={"name": "Materiales 010C"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def anadir_linea(
    api: httpx.AsyncClient, csrf: str, quotation_id: int, **campos: Any
) -> dict[str, Any]:
    response = await api.post(
        f"{V2}/{quotation_id}/products", json=campos, headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# Autorizacion
# ---------------------------------------------------------------------------
class TestAutorizacion:
    async def test_sin_sesion_no_se_leen_los_materiales(self, api: httpx.AsyncClient) -> None:
        assert (await api.get(MATERIALS)).status_code == 401

    async def test_cotizar_no_autoriza_a_valorizar(self, api: httpx.AsyncClient) -> None:
        """Poner el costo de la arcilla es politica de la casa, no una linea."""
        csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)
        response = await api.put(
            f"{MATERIALS}/1",
            json={
                "material_kind": "BODY",
                "origin": "PURCHASE",
                "purchase_quantity": "1000",
                "purchase_cost": "10",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Valorizacion
# ---------------------------------------------------------------------------
class TestValorizacion:
    async def test_el_transporte_entra_en_el_costo_por_gramo(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El ejemplo aprobado: 100 kg a S/100 + S/30 = S/0,0013 el gramo.

        Calculado sobre los 100 sueltos daria 0,001, un 23 % menos, y ese error
        se multiplicaria por cada gramo de cada pieza.
        """
        producto = await crear_producto(api, admin_csrf, "Arcilla Terranova")

        material = await valorizar(api, admin_csrf, producto["id"])

        assert Decimal(material["acquisition_total_cost"]) == Decimal(130)
        assert Decimal(material["effective_cost_per_unit"]) == Decimal("0.0013")

    async def test_el_costo_por_gramo_lo_deriva_la_base(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Es una columna generada: no existe separada de sus operandos.

        Cambiar la compra por SQL recalcula el costo sin que nadie tenga que
        acordarse de hacerlo.
        """
        producto = await crear_producto(api, admin_csrf, "Arcilla generada")
        await valorizar(api, admin_csrf, producto["id"])

        await db_session.execute(
            text("UPDATE v2_material_costs SET purchase_cost = 200 WHERE product_id = :id"),
            {"id": producto["id"]},
        )
        await db_session.commit()

        recalculado = await db_session.scalar(
            text("SELECT effective_cost_per_unit FROM v2_material_costs WHERE product_id = :id"),
            {"id": producto["id"]},
        )
        assert recalculado == Decimal("0.0023")

    async def test_un_material_donado_puede_valorizarse(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Costo cero y valor de costeo propio son dos hechos distintos.

        Valorizarlo a cero regalaria tambien el margen de la pieza.
        """
        producto = await crear_producto(api, admin_csrf, "Arcilla reciclada del taller")

        material = await valorizar(
            api,
            admin_csrf,
            producto["id"],
            origin="DONATION",
            purchase_quantity="12000",
            purchase_cost="0",
            transport_cost="0",
            costing_override_per_unit="0.0012",
        )

        assert Decimal(material["acquisition_total_cost"]) == Decimal(0)
        assert Decimal(material["effective_cost_per_unit"]) == Decimal("0.0012")

    async def test_no_se_escribe_en_el_costo_del_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """`products.cost` lo lee el costeo del Cotizador historico.

        Recalcularlo cambiaria precios de Legacy sin que nadie lo pidiera, y
        sin error visible.
        """
        producto = await crear_producto(api, admin_csrf, "Arcilla sin tocar", cost="0.5")
        await valorizar(api, admin_csrf, producto["id"])

        maestro = await db_session.scalar(
            text("SELECT cost FROM products WHERE id = :id"), {"id": producto["id"]}
        )
        assert maestro == Decimal("0.5"), "010C reescribio el costo del maestro Legacy"

    async def test_una_cantidad_de_cero_se_rechaza_con_un_mensaje(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        producto = await crear_producto(api, admin_csrf, "Arcilla sin cantidad")
        response = await api.put(
            f"{MATERIALS}/{producto['id']}",
            json={
                "material_kind": "BODY",
                "origin": "PURCHASE",
                "purchase_quantity": "0",
                "purchase_cost": "100",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 422

    async def test_un_producto_terminado_no_puede_ser_material(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Un producto terminado no forma otro producto terminado."""
        producto = await crear_producto(
            api, admin_csrf, "Plato terminado", product_type="FINISHED_PRODUCT"
        )
        response = await api.put(
            f"{MATERIALS}/{producto['id']}",
            json={
                "material_kind": "BODY",
                "origin": "PURCHASE",
                "purchase_quantity": "1000",
                "purchase_cost": "10",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "V2_MATERIAL_PRODUCT_INVALID"

    async def test_valorizar_queda_auditado(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        producto = await crear_producto(api, admin_csrf, "Arcilla auditada")
        await valorizar(api, admin_csrf, producto["id"])

        total = await db_session.scalar(
            text("SELECT count(*) FROM audit_events WHERE entity_type = 'v2_material_cost'")
        )
        assert total == 1


# ---------------------------------------------------------------------------
# Pasta en la linea
# ---------------------------------------------------------------------------
class TestPasta:
    async def test_peso_y_costo_del_ejemplo_canonico(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """500 g por pieza, 20 piezas: 10.000 g a S/0,0013 son S/13."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla del ejemplo")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
        )

        assert Decimal(linea["body_total_weight"]) == Decimal(10_000)
        assert Decimal(linea["body_cost_per_unit"]) == Decimal("0.0013")
        assert Decimal(linea["body_cost"]) == Decimal(13)

    async def test_la_unidad_sale_del_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Nunca del navegador: si pudiera mandarla, un material que se lleva
        en gramos podria cotizarse en mililitros sin que nada lo desmintiera."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla con unidad")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=1,
            body_material_id=pasta["id"],
            body_unit_weight="500",
        )
        assert linea["body_uom"] == "g"

    async def test_cada_linea_puede_tener_su_propia_pasta(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """No hay una pasta global forzosa para toda la cotizacion."""
        primera = await crear_producto(api, admin_csrf, "Arcilla A")
        segunda = await crear_producto(api, admin_csrf, "Arcilla B")
        await valorizar(api, admin_csrf, primera["id"])
        await valorizar(api, admin_csrf, segunda["id"], purchase_cost="200")
        cotizacion = await crear_cotizacion(api, admin_csrf)

        una = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=1,
            body_material_id=primera["id"],
            body_unit_weight="100",
        )
        otra = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=1,
            body_material_id=segunda["id"],
            body_unit_weight="100",
        )

        assert una["body_material_id"] != otra["body_material_id"]
        assert Decimal(una["body_cost"]) != Decimal(otra["body_cost"])

    async def test_el_override_no_cambia_el_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Costear ESTA cotizacion con otro valor es una decision de ESTA."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla con override")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=10,
            body_material_id=pasta["id"],
            body_unit_weight="100",
            body_cost_per_unit_override="0.0015",
        )

        assert Decimal(linea["body_cost_per_unit"]) == Decimal("0.0015")
        assert linea["body_cost_is_override"] is True

        materiales = (await api.get(MATERIALS)).json()["items"]
        maestro = next(m for m in materiales if m["product_id"] == pasta["id"])
        assert Decimal(maestro["effective_cost_per_unit"]) == Decimal("0.0013")

    async def test_cambiar_el_maestro_no_reescribe_lo_ya_cotizado(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La prueba que justifica el snapshot.

        Subir el precio de la arcilla no puede cambiar el costo de algo que ya
        se presupuesto —ni, mucho menos, de algo que ya se envio—.
        """
        pasta = await crear_producto(api, admin_csrf, "Arcilla que sube")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        antes = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=10,
            body_material_id=pasta["id"],
            body_unit_weight="100",
        )
        assert Decimal(antes["body_cost_per_unit"]) == Decimal("0.0013")

        await valorizar(api, admin_csrf, pasta["id"], purchase_cost="150", expected_version=1)

        lineas = (await api.get(f"{V2}/{cotizacion}/products")).json()["items"]
        assert Decimal(lineas[0]["body_cost_per_unit"]) == Decimal("0.0013")

        nueva = await crear_cotizacion(api, admin_csrf)
        despues = await anadir_linea(
            api,
            admin_csrf,
            nueva,
            quantity=10,
            body_material_id=pasta["id"],
            body_unit_weight="100",
        )
        assert Decimal(despues["body_cost_per_unit"]) == Decimal("0.0018")

    async def test_elegir_pasta_sin_peso_avisa_y_no_bloquea(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Un borrador a medio llenar tiene que poder guardarse."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla sin peso")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api, admin_csrf, cotizacion, quantity=10, body_material_id=pasta["id"]
        )

        assert "V2_BODY_WEIGHT_REQUIRED" in linea["warnings"]
        assert Decimal(linea["body_cost"]) == Decimal(0)


# ---------------------------------------------------------------------------
# Esmalte
# ---------------------------------------------------------------------------
class TestEsmalte:
    async def test_por_defecto_una_linea_no_lleva_esmalte(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla sin esmalte")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
        )

        assert linea["requires_glaze"] is False
        assert Decimal(linea["glaze_total_weight"]) == Decimal(0)
        assert Decimal(linea["glaze_cost"]) == Decimal(0)

    async def test_el_esmalte_es_el_quince_por_ciento_del_peso(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """500 g de pasta llevan 75 g; 20 piezas, 1.500 g."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla con esmalte")
        await valorizar(api, admin_csrf, pasta["id"])
        esmalte = await crear_producto(api, admin_csrf, "Esmalte unico")
        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="200",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )

        assert Decimal(linea["glaze_percent"]) == Decimal(15)
        assert Decimal(linea["glaze_total_weight"]) == Decimal(1500)
        assert Decimal(linea["glaze_cost_per_unit"]) == Decimal("0.2")
        assert Decimal(linea["glaze_cost"]) == Decimal(300)

    async def test_se_elige_el_mas_caro_por_gramo_no_el_envase_mas_caro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El caso canonico de la fase.

        A: S/50 por 1.000 g = S/0,05 el gramo.
        B: S/20 por 100 g   = S/0,20 el gramo.

        B cuesta menos por envase y mas por gramo. Comparar el envase elegiria
        el equivocado y la cotizacion saldria barata.
        """
        pasta = await crear_producto(api, admin_csrf, "Arcilla para comparar")
        await valorizar(api, admin_csrf, pasta["id"])
        barato_por_gramo = await crear_producto(api, admin_csrf, "Esmalte A envase grande")
        caro_por_gramo = await crear_producto(api, admin_csrf, "Esmalte B envase chico")
        await valorizar(
            api,
            admin_csrf,
            barato_por_gramo["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="50",
            transport_cost="0",
        )
        await valorizar(
            api,
            admin_csrf,
            caro_por_gramo["id"],
            material_kind="GLAZE",
            purchase_quantity="100",
            purchase_cost="20",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=1,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )

        assert linea["glaze_material_id"] == caro_por_gramo["id"]
        assert Decimal(linea["glaze_cost_per_unit"]) == Decimal("0.2")
        assert linea["glaze_is_reference"] is True

    async def test_un_esmalte_sin_stock_sirve_de_referencia(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Avisa, NUNCA bloquea: lo que se protege es el precio de la oferta."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla sin stock de esmalte")
        await valorizar(api, admin_csrf, pasta["id"])
        esmalte = await crear_producto(api, admin_csrf, "Esmalte agotado")
        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="200",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )

        assert linea["glaze_material_id"] == esmalte["id"]
        assert Decimal(linea["glaze_cost"]) == Decimal(300)
        assert "V2_GLAZE_REFERENCE_WITHOUT_STOCK" in linea["warnings"]

    async def test_un_esmalte_inactivo_no_se_elige(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla con esmalte inactivo")
        await valorizar(api, admin_csrf, pasta["id"])
        inactivo = await crear_producto(api, admin_csrf, "Esmalte retirado")
        activo = await crear_producto(api, admin_csrf, "Esmalte vigente")
        await valorizar(
            api,
            admin_csrf,
            inactivo["id"],
            material_kind="GLAZE",
            purchase_quantity="100",
            purchase_cost="90",
            transport_cost="0",
        )
        await valorizar(
            api,
            admin_csrf,
            activo["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="50",
            transport_cost="0",
        )
        baja = await api.put(
            f"{PRODUCTS}/{inactivo['id']}",
            json={
                "name": inactivo["name"],
                "product_type": "RAW_MATERIAL",
                "product_category_id": inactivo["product_category_id"],
                "base_uom_code": "g",
                "active": False,
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert baja.status_code == 200, baja.text

        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=1,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )

        assert linea["glaze_material_id"] == activo["id"]

    async def test_sin_ningun_esmalte_activo_avisa_y_no_revienta(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla huerfana")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=1,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )

        assert "V2_GLAZE_NO_ACTIVE_MATERIAL" in linea["warnings"]
        assert Decimal(linea["glaze_cost"]) == Decimal(0)

    async def test_sin_conversion_declarada_se_usa_la_de_reserva_y_se_dice(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla fallback")
        await valorizar(api, admin_csrf, pasta["id"])
        esmalte = await crear_producto(api, admin_csrf, "Esmalte sin conversion")
        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="200",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )

        assert Decimal(linea["glaze_volume_ml"]) == Decimal(1500)
        assert linea["glaze_conversion_is_fallback"] is True

    async def test_la_conversion_declarada_gana_sobre_la_de_reserva(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla con conversion")
        await valorizar(api, admin_csrf, pasta["id"])
        esmalte = await crear_producto(api, admin_csrf, "Esmalte con densidad")
        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="200",
            transport_cost="0",
            ml_per_gram="0.8",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )

        assert Decimal(linea["glaze_volume_ml"]) == Decimal(1200)
        assert linea["glaze_conversion_is_fallback"] is False
        assert Decimal(linea["glaze_ml_per_gram"]) == Decimal("0.8")

    async def test_apagar_el_esmalte_lo_deja_todo_en_cero(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla que apaga")
        await valorizar(api, admin_csrf, pasta["id"])
        esmalte = await crear_producto(api, admin_csrf, "Esmalte que se apaga")
        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="200",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )
        assert Decimal(linea["glaze_cost"]) == Decimal(300)

        apagada = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"requires_glaze": False},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert apagada.status_code == 200, apagada.text

        assert Decimal(apagada.json()["glaze_cost"]) == Decimal(0)
        assert Decimal(apagada.json()["glaze_total_weight"]) == Decimal(0)
        assert Decimal(apagada.json()["glaze_volume_ml"]) == Decimal(0)

    async def test_el_esmalte_congelado_sobrevive_al_cambio_del_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla esmalte historico")
        await valorizar(api, admin_csrf, pasta["id"])
        esmalte = await crear_producto(api, admin_csrf, "Esmalte que sube")
        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="200",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )

        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="400",
            transport_cost="0",
            expected_version=1,
        )

        lineas = (await api.get(f"{V2}/{cotizacion}/products")).json()["items"]
        assert Decimal(lineas[0]["glaze_cost_per_unit"]) == Decimal("0.2")
        assert Decimal(lineas[0]["glaze_cost"]) == Decimal(300)


# ---------------------------------------------------------------------------
# Inventario: cotizar no consume
# ---------------------------------------------------------------------------
class TestInventario:
    async def test_cotizar_no_descuenta_ni_pasta_ni_esmalte(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """La regla mas cara de romper: pedir un presupuesto no vacia el almacen.

        Se compara el saldo ANTES y DESPUES de cotizar, y tambien que no se
        haya escrito ni un movimiento: un saldo igual con movimientos detras
        seria una salida y una entrada que se anulan, no «no se toco nada».
        """
        pasta = await crear_producto(api, admin_csrf, "Arcilla con existencia")
        esmalte = await crear_producto(api, admin_csrf, "Esmalte con existencia")
        await valorizar(api, admin_csrf, pasta["id"])
        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="200",
            transport_cost="0",
        )
        ubicacion = await _ubicacion(api, admin_csrf)
        await _entrada(api, admin_csrf, pasta["id"], ubicacion, "50000")
        await _entrada(api, admin_csrf, esmalte["id"], ubicacion, "5000")

        antes = await _saldos(db_session)
        movimientos_antes = await db_session.scalar(text("SELECT count(*) FROM stock_movements"))

        cotizacion = await crear_cotizacion(api, admin_csrf)
        await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )

        assert await _saldos(db_session) == antes, "cotizar movio existencia"
        assert (
            await db_session.scalar(text("SELECT count(*) FROM stock_movements"))
            == movimientos_antes
        ), "cotizar dejo movimientos de inventario"

    async def test_una_entrada_manual_aumenta_el_stock(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Se reutiliza el inventario del proyecto, no se crea uno paralelo."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla que entra")
        await valorizar(api, admin_csrf, pasta["id"])
        ubicacion = await _ubicacion(api, admin_csrf)

        await _entrada(api, admin_csrf, pasta["id"], ubicacion, "12000")

        materiales = (await api.get(MATERIALS)).json()["items"]
        material = next(m for m in materiales if m["product_id"] == pasta["id"])
        assert Decimal(material["stock"]) == Decimal(12_000)

    async def test_el_taller_puede_registrar_una_entrada_pero_no_valorizar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Dos permisos distintos porque son dos decisiones distintas.

        Quien recibe el material en el taller registra la entrada: es su
        trabajo, y el inventario del proyecto ya lo permite desde Fase 3.
        Poner cuanto VALE ese material es politica comercial, y esa sigue
        siendo de administracion.

        010C no cambia ninguno de los dos: solo comprueba que la frontera
        existente sigue donde estaba.
        """
        pasta = await crear_producto(api, admin_csrf, "Arcilla del taller")
        await valorizar(api, admin_csrf, pasta["id"])
        ubicacion = await _ubicacion(api, admin_csrf)

        csrf = await authenticate(api, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD)

        entrada = await api.post(
            f"{INVENTORY}/adjustments",
            json={
                "product_id": pasta["id"],
                "location_id": ubicacion,
                "quantity": "500",
                "reason": "Entrada del taller",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert entrada.status_code == 201, entrada.text

        valorizacion = await api.put(
            f"{MATERIALS}/{pasta['id']}",
            json={
                "material_kind": "BODY",
                "origin": "PURCHASE",
                "purchase_quantity": "1000",
                "purchase_cost": "10",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert valorizacion.status_code == 403


async def _ubicacion(api: httpx.AsyncClient, csrf: str) -> int:
    response = await api.post(
        f"{INVENTORY}/locations",
        json={"name": "Almacen 010C"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


async def _entrada(
    api: httpx.AsyncClient, csrf: str, product_id: int, location_id: int, cantidad: str
) -> None:
    response = await api.post(
        f"{INVENTORY}/adjustments",
        json={
            "product_id": product_id,
            "location_id": location_id,
            "quantity": cantidad,
            "reason": "Entrada manual 010C",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 201, response.text


async def _saldos(session: AsyncSession) -> list[tuple[int, Decimal]]:
    filas = await session.execute(
        text("SELECT product_id, quantity FROM stock_balances ORDER BY product_id")
    )
    return [(int(fila.product_id), Decimal(fila.quantity)) for fila in filas]


# ---------------------------------------------------------------------------
# Legacy
# ---------------------------------------------------------------------------
class TestLegacy:
    async def test_las_lineas_v2_no_tocan_las_de_legacy(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla aislada")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=10,
            body_material_id=pasta["id"],
            body_unit_weight="100",
        )

        assert await db_session.scalar(text("SELECT count(*) FROM quotations")) == 0
        assert await db_session.scalar(text("SELECT count(*) FROM quotation_items")) == 0

    async def test_una_cotizacion_emitida_no_admite_mas_material(
        self, api: httpx.AsyncClient, admin_csrf: str, db_session: AsyncSession
    ) -> None:
        """Ya comprometio un precio: anadirle material lo cambiaria por detras."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla congelada")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        await db_session.execute(
            text("UPDATE v2_quotations SET status = 'CONFIRMED' WHERE id = :id"),
            {"id": cotizacion},
        )
        await db_session.commit()

        response = await api.post(
            f"{V2}/{cotizacion}/products",
            json={"quantity": 1, "body_material_id": pasta["id"], "body_unit_weight": "100"},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "V2_QUOTATION_NOT_EDITABLE"

    async def test_una_linea_de_otra_cotizacion_no_se_puede_editar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Conocer un id de linea no puede bastar para tocar otra cotizacion."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla ajena")
        await valorizar(api, admin_csrf, pasta["id"])
        propia = await crear_cotizacion(api, admin_csrf)
        ajena = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            propia,
            quantity=1,
            body_material_id=pasta["id"],
            body_unit_weight="100",
        )

        response = await api.put(
            f"{V2}/{ajena}/products/{linea['id']}",
            json={"quantity": 5},
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert response.status_code == 404


@pytest.mark.parametrize("cantidad", [0, 1])
async def test_una_cantidad_cero_no_revienta(
    api: httpx.AsyncClient, admin_csrf: str, cantidad: int
) -> None:
    """Cero es un estado legitimo de un borrador, no un error."""
    pasta = await crear_producto(api, admin_csrf, f"Arcilla cantidad {cantidad}")
    await valorizar(api, admin_csrf, pasta["id"])
    cotizacion = await crear_cotizacion(api, admin_csrf)

    linea = await anadir_linea(
        api,
        admin_csrf,
        cotizacion,
        quantity=cantidad,
        body_material_id=pasta["id"],
        body_unit_weight="500",
    )

    assert Decimal(linea["body_total_weight"]) == Decimal(500 * cantidad)


# ---------------------------------------------------------------------------
# Lo que encontraron las auditorias de 010C
# ---------------------------------------------------------------------------
class TestConcurrencia:
    """Valorizar es un formulario que se manda ENTERO.

    Por eso la version importa aqui y no en las lineas, que se mandan por
    partes: dos administradores con la pantalla abierta reescriben los siete
    campos con lo que cada uno tenia delante, y sin version el segundo borra el
    trabajo del primero sin conflicto, sin error y sin rastro.
    """

    async def test_cambiar_sin_declarar_la_version_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla concurrente")
        await valorizar(api, admin_csrf, pasta["id"])

        segunda = await api.put(
            f"{MATERIALS}/{pasta['id']}",
            json={
                "material_kind": "BODY",
                "origin": "PURCHASE",
                "purchase_quantity": CIEN_KILOS_EN_GRAMOS,
                "purchase_cost": "150",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert segunda.status_code == 409
        assert segunda.json()["error"]["code"] == "V2_MATERIAL_VERSION_CONFLICT"

    async def test_una_version_vieja_no_pisa_a_quien_llego_antes(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El caso exacto: dos pantallas abiertas, las dos leyeron la version 1."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla disputada")
        await valorizar(api, admin_csrf, pasta["id"])

        primero = await valorizar(
            api, admin_csrf, pasta["id"], purchase_cost="150", expected_version=1
        )
        assert primero["version"] == 2

        segundo = await api.put(
            f"{MATERIALS}/{pasta['id']}",
            json={
                "expected_version": 1,
                "material_kind": "BODY",
                "origin": "PURCHASE",
                "purchase_quantity": CIEN_KILOS_EN_GRAMOS,
                "purchase_cost": "100",
                "notes": "vengo de un formulario viejo",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert segundo.status_code == 409
        # Y la correccion del primero sigue ahi.
        actual = (await api.get(MATERIALS)).json()["items"]
        fila = next(m for m in actual if m["product_id"] == pasta["id"])
        assert Decimal(fila["purchase_cost"]) == Decimal(150)

    async def test_la_primera_valorizacion_no_tiene_version_que_declarar(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Exigirla al crear obligaria a inventarse un numero."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla estrenada")

        material = await valorizar(api, admin_csrf, pasta["id"])

        assert material["version"] == 1


class TestMaterialQueDejoDeSerlo:
    """El maestro es editable y la valorizacion se queda donde estaba.

    Un producto valorizado como materia prima puede acabar convertido en
    servicio, quedarse sin unidad base o darse de baja. La valorizacion
    sobrevive a todo eso, intacta y ya sin sentido.
    """

    async def test_una_pasta_no_puede_elegirse_como_esmalte(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El costo "funcionaria" y el documento quedaria economicamente falso."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla que no es esmalte")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)

        response = await api.post(
            f"{V2}/{cotizacion}/products",
            json={
                "quantity": 1,
                "requires_glaze": True,
                "glaze_material_id": pasta["id"],
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "V2_MATERIAL_KIND_MISMATCH"

    async def test_un_esmalte_no_puede_elegirse_como_pasta(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        esmalte = await crear_producto(api, admin_csrf, "Esmalte que no es pasta")
        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="200",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)

        response = await api.post(
            f"{V2}/{cotizacion}/products",
            json={
                "quantity": 20,
                "body_material_id": esmalte["id"],
                "body_unit_weight": "500",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "V2_MATERIAL_KIND_MISMATCH"

    async def test_elegir_hoy_un_material_dado_de_baja_se_rechaza(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla retirada")
        await valorizar(api, admin_csrf, pasta["id"])
        baja = await api.put(
            f"{PRODUCTS}/{pasta['id']}",
            json={
                "name": pasta["name"],
                "product_type": "RAW_MATERIAL",
                "product_category_id": pasta["product_category_id"],
                "base_uom_code": "g",
                "active": False,
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert baja.status_code == 200, baja.text
        cotizacion = await crear_cotizacion(api, admin_csrf)

        response = await api.post(
            f"{V2}/{cotizacion}/products",
            json={
                "quantity": 10,
                "body_material_id": pasta["id"],
                "body_unit_weight": "500",
            },
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "V2_MATERIAL_PRODUCT_INVALID"

    async def test_si_ya_estaba_puesto_avisa_pero_deja_seguir(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Bloquear aqui encallaria un borrador por una decision de otra pantalla.

        La distincion es toda la regla: elegirlo HOY se rechaza; seguir
        editando una linea que ya lo tenia, se avisa.
        """
        pasta = await crear_producto(api, admin_csrf, "Arcilla que se retira despues")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=10,
            body_material_id=pasta["id"],
            body_unit_weight="500",
        )
        baja = await api.put(
            f"{PRODUCTS}/{pasta['id']}",
            json={
                "name": pasta["name"],
                "product_type": "RAW_MATERIAL",
                "product_category_id": pasta["product_category_id"],
                "base_uom_code": "g",
                "active": False,
            },
            headers={"X-CSRF-Token": admin_csrf},
        )
        assert baja.status_code == 200, baja.text

        response = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"quantity": 20},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert "V2_BODY_MATERIAL_UNAVAILABLE" in cuerpo["warnings"]
        # El peso SI se recalcula: depende de la linea, no del maestro.
        assert Decimal(cuerpo["body_total_weight"]) == Decimal(10_000)
        # El costo por unidad NO: se queda en lo que la linea congelo.
        assert Decimal(cuerpo["body_cost_per_unit"]) == Decimal("0.0013")

    async def test_una_pasta_convertida_en_esmalte_no_recalcula_la_linea(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Avisar no arregla un importe: hay que dejar de recalcularlo.

        Alguien corrige en el maestro el tipo de un material —lo que valorizo
        como pasta era en realidad un esmalte— y una linea que ya lo usaba
        seguiria cobrando pasta al precio del esmalte. El numero seria falso y
        el aviso no lo cambiaria, asi que la linea conserva lo que congelo.
        """
        pasta = await crear_producto(api, admin_csrf, "Arcilla mal clasificada")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=10,
            body_material_id=pasta["id"],
            body_unit_weight="500",
        )
        assert Decimal(linea["body_cost_per_unit"]) == Decimal("0.0013")

        # Se corrige el maestro: era un esmalte, y ademas mucho mas caro.
        await valorizar(
            api,
            admin_csrf,
            pasta["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="500",
            transport_cost="0",
            expected_version=1,
        )

        response = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"quantity": 20},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert "V2_BODY_MATERIAL_UNAVAILABLE" in cuerpo["warnings"]
        # Ni el costo por unidad ni el nombre se refrescan desde un material
        # que ya no corresponde a este uso.
        assert Decimal(cuerpo["body_cost_per_unit"]) == Decimal("0.0013")
        assert Decimal(cuerpo["body_cost"]) == Decimal(20) * Decimal(500) * Decimal("0.0013")

    async def test_dos_primeras_valorizaciones_a_la_vez_dan_conflicto_no_un_500(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """No hay fila que bloquear todavia, asi que las dos pasan la lectura.

        Una choca contra el UNIQUE. Es el mismo conflicto de concurrencia que
        cubre la version y merece la misma respuesta: un 409 que se entiende,
        no un 500 que parece una averia del servidor.
        """
        pasta = await crear_producto(api, admin_csrf, "Arcilla a dos manos")
        cuerpo = {
            "material_kind": "BODY",
            "origin": "PURCHASE",
            "purchase_quantity": CIEN_KILOS_EN_GRAMOS,
            "purchase_cost": "100",
        }

        primera = await api.put(
            f"{MATERIALS}/{pasta['id']}", json=cuerpo, headers={"X-CSRF-Token": admin_csrf}
        )
        assert primera.status_code == 200, primera.text

        # La segunda llega creyendo que tampoco existe: sin `expected_version`.
        segunda = await api.put(
            f"{MATERIALS}/{pasta['id']}", json=cuerpo, headers={"X-CSRF-Token": admin_csrf}
        )

        assert segunda.status_code == 409
        assert segunda.json()["error"]["code"] == "V2_MATERIAL_VERSION_CONFLICT"


class TestNombreYTotalDeLinea:
    async def test_una_pieza_de_encargo_puede_llamarse_por_su_nombre(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La mitad del trabajo del taller no existe en el catalogo."""
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api, admin_csrf, cotizacion, product_name="Plato palta", quantity=20
        )

        assert linea["product_name"] == "Plato palta"

    async def test_si_la_linea_cuelga_de_un_producto_manda_el_maestro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Dos nombres para la misma linea serian dos verdades."""
        pieza = await crear_producto(
            api, admin_csrf, "Plato del catalogo", product_type="FINISHED_PRODUCT"
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            product_id=pieza["id"],
            product_name="Como lo llamo yo",
            quantity=5,
        )

        assert linea["product_name"] == "Plato del catalogo"

    async def test_el_total_de_la_linea_lo_suma_el_backend(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Sumarlo en el navegador daria colas de decimales en coma flotante."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla sumada")
        await valorizar(api, admin_csrf, pasta["id"])
        esmalte = await crear_producto(api, admin_csrf, "Esmalte sumado")
        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="200",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)

        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )

        # El ejemplo aprobado: S/13 de pasta y S/300 de esmalte.
        assert Decimal(linea["body_cost"]) == Decimal(13)
        assert Decimal(linea["glaze_cost"]) == Decimal(300)
        assert Decimal(linea["materials_cost"]) == Decimal(313)


class TestLaEdicionParcialNoBorraDecisiones:
    """Cambiar la cantidad no puede cambiar el precio pactado ni quien eligio.

    Los dos fallos de esta familia son invisibles: no lanzan nada, no salen en
    los registros y solo se notan al leer un importe que ya no es el que se
    acordo. Por eso estan fijados aqui con el caso completo.
    """

    async def test_el_costo_pactado_sobrevive_a_cambiar_la_cantidad(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La pantalla manda solo lo que cambio, y eso no es «quita el pacto»."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla pactada")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=10,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            body_cost_per_unit_override="0.002",
        )
        assert linea["body_cost_is_override"] is True
        assert Decimal(linea["body_cost_per_unit"]) == Decimal("0.002")

        response = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"quantity": 20},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert cuerpo["body_cost_is_override"] is True
        assert Decimal(cuerpo["body_cost_per_unit"]) == Decimal("0.002")
        # Y el importe corresponde al costo pactado, no al del maestro.
        assert Decimal(cuerpo["body_cost"]) == Decimal(20) * Decimal(500) * Decimal("0.002")

    async def test_retirar_el_pacto_es_una_decision_explicita(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Mandarlo a nulo SI lo quita: es la otra mitad de la regla."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla despactada")
        await valorizar(api, admin_csrf, pasta["id"])
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=10,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            body_cost_per_unit_override="0.002",
        )

        response = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"body_cost_per_unit_override": None},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert cuerpo["body_cost_is_override"] is False
        assert Decimal(cuerpo["body_cost_per_unit"]) == Decimal("0.0013")

    async def test_cambiar_de_material_no_hereda_el_pacto_del_anterior(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """El acuerdo se tomo sobre otra arcilla; arrastrarlo seria inventarlo."""
        primera = await crear_producto(api, admin_csrf, "Arcilla pactada A")
        segunda = await crear_producto(api, admin_csrf, "Arcilla pactada B")
        await valorizar(api, admin_csrf, primera["id"])
        await valorizar(api, admin_csrf, segunda["id"], purchase_cost="200")
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=10,
            body_material_id=primera["id"],
            body_unit_weight="500",
            body_cost_per_unit_override="0.002",
        )

        response = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"body_material_id": segunda["id"]},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert cuerpo["body_cost_is_override"] is False
        assert Decimal(cuerpo["body_cost_per_unit"]) == Decimal("0.0023")

    async def test_el_esmalte_propuesto_sigue_siendo_una_referencia(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La seleccion automatica deja el id puesto, y eso no es haber elegido.

        Si se perdiera la marca, la pantalla dejaria de avisar de que produccion
        usara otro esmalte, y la linea quedaria clavada en ese sin volver a
        proponer el mas caro.
        """
        pasta = await crear_producto(api, admin_csrf, "Arcilla con referencia")
        await valorizar(api, admin_csrf, pasta["id"])
        esmalte = await crear_producto(api, admin_csrf, "Esmalte propuesto")
        await valorizar(
            api,
            admin_csrf,
            esmalte["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="200",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )
        assert linea["glaze_is_reference"] is True

        response = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"quantity": 30},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        assert response.json()["glaze_is_reference"] is True

    async def test_la_referencia_se_vuelve_a_elegir_si_aparece_uno_mas_caro(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """Pecar por arriba es la regla, y solo se cumple si se reevalua."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla que reevalua")
        await valorizar(api, admin_csrf, pasta["id"])
        barato = await crear_producto(api, admin_csrf, "Esmalte barato")
        await valorizar(
            api,
            admin_csrf,
            barato["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="50",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
        )
        assert linea["glaze_material_id"] == barato["id"]

        caro = await crear_producto(api, admin_csrf, "Esmalte caro")
        await valorizar(
            api,
            admin_csrf,
            caro["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="300",
            transport_cost="0",
        )

        response = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"quantity": 20},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert cuerpo["glaze_material_id"] == caro["id"]
        assert cuerpo["glaze_is_reference"] is True

    async def test_un_esmalte_elegido_a_mano_no_lo_cambia_el_sistema(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        """La otra mitad: quien eligio manda, aunque aparezca uno mas caro."""
        pasta = await crear_producto(api, admin_csrf, "Arcilla con esmalte a mano")
        await valorizar(api, admin_csrf, pasta["id"])
        elegido = await crear_producto(api, admin_csrf, "Esmalte elegido a mano")
        await valorizar(
            api,
            admin_csrf,
            elegido["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="50",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
            glaze_material_id=elegido["id"],
        )
        assert linea["glaze_is_reference"] is False

        carisimo = await crear_producto(api, admin_csrf, "Esmalte carisimo")
        await valorizar(
            api,
            admin_csrf,
            carisimo["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="900",
            transport_cost="0",
        )

        response = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"quantity": 25},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        cuerpo = response.json()
        assert cuerpo["glaze_material_id"] == elegido["id"]
        assert cuerpo["glaze_is_reference"] is False

    async def test_soltar_el_esmalte_devuelve_la_decision_al_sistema(
        self, api: httpx.AsyncClient, admin_csrf: str
    ) -> None:
        pasta = await crear_producto(api, admin_csrf, "Arcilla que suelta")
        await valorizar(api, admin_csrf, pasta["id"])
        elegido = await crear_producto(api, admin_csrf, "Esmalte que se suelta")
        await valorizar(
            api,
            admin_csrf,
            elegido["id"],
            material_kind="GLAZE",
            purchase_quantity="1000",
            purchase_cost="50",
            transport_cost="0",
        )
        cotizacion = await crear_cotizacion(api, admin_csrf)
        linea = await anadir_linea(
            api,
            admin_csrf,
            cotizacion,
            quantity=20,
            body_material_id=pasta["id"],
            body_unit_weight="500",
            requires_glaze=True,
            glaze_material_id=elegido["id"],
        )

        response = await api.put(
            f"{V2}/{cotizacion}/products/{linea['id']}",
            json={"glaze_material_id": None},
            headers={"X-CSRF-Token": admin_csrf},
        )

        assert response.status_code == 200, response.text
        assert response.json()["glaze_is_reference"] is True


async def test_reenviar_el_mismo_material_no_borra_el_costo_pactado(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """Encontrado auditando 010D: el mismo error estaba aqui.

    «Que el material venga en la peticion» no es «que se haya cambiado». Un
    cliente que reenvia el formulario entero manda el mismo id de siempre, y
    tratarlo como un cambio retira el costo acordado con el cliente sin que
    nadie lo pida.
    """
    pasta = await crear_producto(api, admin_csrf, "Arcilla reenviada")
    await valorizar(api, admin_csrf, pasta["id"])
    cotizacion = await crear_cotizacion(api, admin_csrf)
    linea = await anadir_linea(
        api,
        admin_csrf,
        cotizacion,
        quantity=10,
        body_material_id=pasta["id"],
        body_unit_weight="500",
        body_cost_per_unit_override="0.002",
    )

    response = await api.put(
        f"{V2}/{cotizacion}/products/{linea['id']}",
        json={"body_material_id": pasta["id"], "quantity": 20},
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 200, response.text
    cuerpo = response.json()
    assert cuerpo["body_cost_is_override"] is True
    assert Decimal(cuerpo["body_cost_per_unit"]) == Decimal("0.002")


async def test_reenviar_un_material_retirado_avisa_en_vez_de_bloquear(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """La otra mitad del mismo error, encontrada en la segunda pasada de Codex.

    `cambio_de_material` ya se calculaba bien para conservar el costo pactado,
    pero la comprobacion de DISPONIBILIDAD seguia mirando la presencia de la
    clave. Un cliente que reenvia el formulario entero con el mismo material
    encallaba la linea si ese material se habia retirado despues, cuando 010C
    decidio expresamente que ese caso avisa y deja seguir.
    """
    pasta = await crear_producto(api, admin_csrf, "Arcilla reenviada y retirada")
    await valorizar(api, admin_csrf, pasta["id"])
    cotizacion = await crear_cotizacion(api, admin_csrf)
    linea = await anadir_linea(
        api,
        admin_csrf,
        cotizacion,
        quantity=10,
        body_material_id=pasta["id"],
        body_unit_weight="500",
    )
    baja = await api.put(
        f"{PRODUCTS}/{pasta['id']}",
        json={
            "name": pasta["name"],
            "product_type": "RAW_MATERIAL",
            "product_category_id": pasta["product_category_id"],
            "base_uom_code": "g",
            "active": False,
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert baja.status_code == 200, baja.text

    response = await api.put(
        f"{V2}/{cotizacion}/products/{linea['id']}",
        json={"body_material_id": pasta["id"], "quantity": 20},
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 200, response.text
    cuerpo = response.json()
    assert "V2_BODY_MATERIAL_UNAVAILABLE" in cuerpo["warnings"]
    # Y lo congelado se conserva.
    assert Decimal(cuerpo["body_cost_per_unit"]) == Decimal("0.0013")


async def test_elegir_hoy_un_material_retirado_se_sigue_rechazando(
    api: httpx.AsyncClient, admin_csrf: str
) -> None:
    """La correccion no puede abrir la puerta que 010C cerro.

    Reenviar el mismo avisa; elegir HOY uno retirado se rechaza. Es la
    distincion entera, y sin esta prueba el arreglo anterior podria haberla
    borrado sin que nada fallara.
    """
    buena = await crear_producto(api, admin_csrf, "Arcilla buena")
    retirada = await crear_producto(api, admin_csrf, "Arcilla ya retirada")
    await valorizar(api, admin_csrf, buena["id"])
    await valorizar(api, admin_csrf, retirada["id"])
    cotizacion = await crear_cotizacion(api, admin_csrf)
    linea = await anadir_linea(
        api,
        admin_csrf,
        cotizacion,
        quantity=10,
        body_material_id=buena["id"],
        body_unit_weight="500",
    )
    baja = await api.put(
        f"{PRODUCTS}/{retirada['id']}",
        json={
            "name": retirada["name"],
            "product_type": "RAW_MATERIAL",
            "product_category_id": retirada["product_category_id"],
            "base_uom_code": "g",
            "active": False,
        },
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert baja.status_code == 200, baja.text

    response = await api.put(
        f"{V2}/{cotizacion}/products/{linea['id']}",
        json={"body_material_id": retirada["id"]},
        headers={"X-CSRF-Token": admin_csrf},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "V2_MATERIAL_PRODUCT_INVALID"
