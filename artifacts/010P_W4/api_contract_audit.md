# Auditoría de contrato API — GREDA 010P W4

Fecha: 2026-09-29. Auditoría local y de solo lectura sobre `feat/010p-w4-integration`.

## Evidencia

- Especificación FastAPI generada desde el árbol backend W4: `openapi_w4.json`.
- Contraste del grafo de código en `BGreda-010p-w4` y `FGreda-010p-w4`, con inspección de las rutas y DTO de producción, inventario, producto, cotizador V2 y Solo Quema.
- E2E Playwright usa PostgreSQL desechable en loopback, backend W4 local y frontend W4. La autenticación usa el doble local de pruebas; las llamadas de dominio van al backend real, sin interceptar endpoints de la aplicación.

## Resultado

**PASS para los contratos W4 auditados.** Los métodos y paths usados por los módulos de producto, cotización V2, producción, inventario y Solo Quema aparecen en el OpenAPI generado. También aparece `/api/v1/production/wip`.

Contratos verificados:

- `StockDeliveryInput` corresponde a `StockDeliveryCreate`: `product_id`, `location_id`, `quantity`, `v2_quotation_id`, `production_order_id` y `reason`. La cantidad cruza como decimal textual; el backend exige valor positivo.
- `ProductionOrderCreateIn` corresponde a `ProductionOrderCreateIn`: cotización V1, cotización V2 o prototipo como origen único; almacén explícito e idempotency key opcional. El backend rechaza campos extra y valida el origen.
- `ProductionOrderCompleteIn` transporta `results`; los campos `line_ref`, `good_quantity`, `scrap_quantity` y `scrap_reason` coinciden con el esquema backend. Los límites y el formato de `line_ref` se validan en servidor.
- Pricing V2 se consulta y actualiza en `/quotations-v2/{quotation_id}/pricing`. La pantalla presenta los importes de `V2Pricing` que devuelve el servidor y los formatea; no calcula el precio comercial autoritativo en React.
- Las llamadas de productos y de Solo Quema usan `/products` y `/firing-quotations-v2` con métodos presentes en OpenAPI. El backend genera la referencia interna del producto; `ProductInput` no admite `id` ni `internal_reference`.

## Discrepancia corregida

El detalle backend `GET /products/{product_id}` devuelve `source_v2_quotation_product_id`, pero `fetchProduct` tipaba la respuesta como `Product`. Se añadió `ProductDetail` y el wrapper ahora devuelve ese tipo. La procedencia queda solo en el contrato de lectura; no se agregó a `ProductInput` ni a la creación.

## Seguridad de altas rápidas

Las rutas de creación de producto, trabajador y técnica reciben `MastersQuickCreateDep`: permite `ADMIN` o `OPERATOR` con `MASTERS_QUICK_CREATE`. La suite incluye comprobaciones para operador sin/sí capability; el E2E W4 valida altas, ausencia de login/stock/movimiento no solicitado y restricciones de valorización. La ruta POST estándar de producto no queda abierta a usuarios anónimos.

## Alcance y limitaciones

El contrato cubre los módulos W4 citados; no pretende certificar todos los módulos API ajenos a 010P. El OpenAPI documenta nombres y restricciones de servidor; los E2E contra el backend verifican la serialización y el comportamiento real de los flujos. La última corrida integral de Playwright pasó 75/75; la auditoría de inventario registró 27 movimientos y cero discrepancias, y la reconciliación local de lotes dejó 0 `UNRECONCILED`.
