# GREDA 010L — Auditoría de producción existente y plan

## 010L_EXISTING_PRODUCTION_AUDIT

**EXISTING_BATCH_MODEL:** no hay hornada operativa. `firings` (+ `firing_kiln_sessions`, `firing_lines`) es la
**hoja de costeo Legacy**: lleva dinero (`subtotal`, `total_cost`, `rate_snapshot`), el factor por ocupación ×3
(`occupancy_factor`, `kiln_occupancy_factors`) que V2 abandonó, estados COMERCIALES DRAFT/CONFIRMED/CANCELLED
(sin STARTED/COMPLETED), y UNA hoja agrupa VARIOS hornos y LOS DOS ciclos (sesiones hijas). Sus líneas son
piezas sueltas, sin relación con órdenes de producción. **No es compatible** con «un horno + un ciclo + una
ejecución con varias órdenes», y adaptarla obligaría a modificar Legacy, que debe quedar INTACTO hasta 010N.

**EXISTING_KILN_MODEL:** `kilns` (código, nombre, `capacity_volume_cm3`, `firing_days_per_batch`, `active`).
Extensible: N hornos activos, sin IDs fijos. Tarifas V2 en `v2_kiln_rates` (horno × ciclo × tipo de cliente).
`FiringType` LOW/HIGH ya existe como enum compartido.

**EXISTING_ASSIGNMENT_MODEL:** ninguno. Ninguna orden apunta a una quema. Lo único cercano son las notas
`FIRING_NOTE` de la orden (horno + ciclo + texto libre), que son registro, no planificación.

**EXISTING_PRODUCTION_ORDER:** `production_orders` con EXACTAMENTE UN ORIGEN (CHECK `exactly_one_origin`):
cotización Legacy, prototipo o puente V2 (`v2_handoff_id`, UNIQUE). Estados CREATED → STARTED → COMPLETED,
CREATED → CANCELLED, con CHECK de coherencia de fechas. Idempotencia por `idempotency_key` UNIQUE + bloqueo.
Contratos de Producción **sin importes** para nadie. RBAC: leer `CurrentUserDep`; escribir (crear, arrancar,
completar, consumos, notas) `WorkshopUserDep` = ADMIN + OPERATOR; anular solo ADMIN.

**V2_NORMAL_BRIDGE:** dos pasos. (1) ADMIN: cotización V2 CONFIRMED → `v2_production_handoffs` (UNIQUE por
cotización, huella congelada, sin inventario). (2) ADMIN/OPERATOR: `POST /production-orders` crea la orden
desde el puente (idempotente en 3 capas). La orden **no copia líneas**: lee la cotización confirmada, que es
inmutable. El volumen ya está congelado CON separación: cabecera `firing_total_volume_cm3`,
`piece_separation_cm_snapshot`, `firing_mode`, `low/high_fire_enabled`, `kiln_id`; por pieza
`v2_quotation_products.unit_volume_cm3` / `total_volume_cm3` y `quantity`.

**SOLO_QUEMA_BRIDGE:** no existe (K5 diferido de 010K). Solo Quema CONFIRMED ya tiene congelado:
`total_volume_cm3`, `firing_mode`, `low/high_fire_enabled`, `kiln_id`, `glaze_enabled` y por línea
`unit_volume_cm3` / `total_volume_cm3` / `quantity`, todo con separación.

**REUSABLE:** `kilns`, `FiringType`, `SequenceService`, `AuditRecorder`, patrón handoff → orden, patrón de
idempotencia (clave + bloqueo + UNIQUE + SAVEPOINT), `WorkshopUserDep`/`AdminUserDep`, `business_date` de Lima,
volúmenes congelados de V2 y Solo Quema, timeline de la orden.

**MISSING:** hornada operativa, asignación orden↔hornada, reserva de capacidad concurrente, puente Solo
Quema → producción, carga interna, motor de sugerencias, pantalla de planificación.

---

## PLAN

### Decisión 1 — Entidad nueva y NEUTRAL: `kiln_batches`

No `v2_kiln_batch`: la hornada es un concepto operativo compartido (§7). Tampoco se reutiliza `firings`
(ver auditoría). Campos:

- `id`, `code` (talonario nuevo `KILN_BATCH`, prefijo `HOR`), `kiln_id` → `kilns` RESTRICT;
- `firing_type` LOW|HIGH (enum existente). **Una hornada = un ciclo** (§11);
- `scheduled_date` (Date, convención de `firings.scheduled_date`);
- `status` PLANNED | STARTED | COMPLETED | CANCELLED, con CHECK de coherencia de fechas como la orden;
- `kiln_name_snapshot`, `capacity_snapshot_cm3` congelados al crear (§77, §87);
- `assigned_volume_cm3` desnormalizado + **CHECK `assigned_volume_cm3 <= capacity_snapshot_cm3`**: la base
  hace IMPOSIBLE superar el 100 %, aunque falle el servicio (§33, §35);
- `exclusive` (bool derivado de la asignación exclusiva, ver decisión 5);
- `version` entera (concurrencia optimista para la UI y contrato de 010M);
- `idempotency_key` UNIQUE, `notes`, `created_by(_name)`, `started_at`, `completed_at`, `cancelled_at`,
  `cancel_reason`, timestamps.

**Sin READY** (§27): no aporta nada que 010L necesite; 010M puede añadir su *readiness* de acomodo.

Transiciones: PLANNED → STARTED → COMPLETED; PLANNED → CANCELLED. PLANNED es lo único editable (asignar,
quitar, mover, reprogramar). STARTED: sin cambios de asignación. COMPLETED y CANCELLED: solo lectura. Cancelar
no borra: libera las asignaciones y conserva la historia.

### Decisión 2 — Asignación por PIEZAS, no por porcentaje

`kiln_batch_assignments`: una fila por (hornada, línea de origen) con **cantidad de piezas**. El volumen es
`cantidad × unit_volume_cm3` congelado (ya incluye separación, §14). Así:

- varias órdenes por hornada y varias hornadas por orden (§9, §10);
- 120 % → 100 % + 20 % repartiendo piezas (§34);
- 010M recibe **qué piezas** hay en cada hornada, no solo un porcentaje (PIECE_SOURCE, §114).

Origen de la línea: exactamente uno de `v2_quotation_product_id`, `v2_firing_quotation_line_id`,
`internal_load_line_id` (CHECK como `exactly_one_origin`). Más `production_order_id` o `internal_load_id`.
`status` ACTIVE | RELEASED (quitar = RELEASED con `released_at`, no DELETE: la historia queda).

Invariante por línea y ciclo: piezas activas asignadas ≤ cantidad de la línea.

### Decisión 3 — Concurrencia (§36, §37)

Asignar / quitar / mover en UNA transacción:

1. bloquear la orden (o carga interna) `FOR UPDATE` → serializa repartos de las mismas piezas;
2. bloquear las hornadas implicadas `FOR UPDATE` **en orden ascendente de id** (sin interbloqueos al mover);
3. recalcular y validar: estado PLANNED, ciclo compatible, capacidad, exclusividad, piezas restantes;
4. escribir, actualizar `assigned_volume_cm3`, `version += 1`;
5. el CHECK de capacidad es la garantía final.

Caso §81 (libre 35 %, A pide 30 %, B pide 20 % a la vez): el bloqueo de la hornada serializa; el segundo
encuentra 5 % libre y se rechaza con 409. Nunca 115 %.

Idempotencia: `idempotency_key` UNIQUE en crear hornada y en asignar/mover; el reintento devuelve el mismo
resultado.

### Decisión 4 — K5, puente Solo Quema → Producción (§18–§22)

Espejo del puente V2 para no inventar un flujo:

- `v2_firing_production_handoffs` (UNIQUE `v2_firing_quotation_id`, huella congelada). ADMIN, como V2.
  Doble clic, reintento y concurrencia → un solo puente (bloqueo + UNIQUE + SAVEPOINT);
- `production_orders.v2_firing_handoff_id` UNIQUE; `exactly_one_origin` pasa a CUATRO ramas, cada una
  nombrando los cuatro campos. Solo migración nueva: no se edita ninguna fusionada;
- la orden no copia líneas: lee la Solo Quema confirmada. **Sin técnicas de fabricación** (§20), sin
  inventario al crearla ni al arrancarla (§21);
- el vidriado viaja como requisito de la orden (`glaze_enabled` visible en la orden); el consumo real del
  esmalte es el consumo explícito de 010I (§22);
- la Solo Quema con puente ya no se puede anular (misma regla que 010H). **El PDF no cambia** (§73).

### Decisión 5 — Compartida y exclusiva (§50–§53)

El modo sale del snapshot de la cotización (`firing_mode`):

- una asignación EXCLUSIVE solo entra en una hornada VACÍA y la marca `exclusive`;
- una hornada `exclusive` no acepta ninguna otra asignación y su libre **no se ofrece** como disponible;
- compartida: el libre se ofrece a otras compatibles.

Aplica igual a Cotizador V2 y a Solo Quema. La carga interna es siempre compartida.

### Decisión 6 — Carga interna (§23, §24, §91)

`internal_loads` (+ `internal_load_lines`): nombre, notas, piezas con cantidad, L/A/H, separación, volumen
calculado con la misma fórmula (`piece_volume`), ciclos que necesita (baja/alta). Talonario `INTERNAL_LOAD`,
prefijo `CI`. **Sin precio, sin CTZ, sin factor.** Se asigna a hornadas exactamente como una orden. No es una
`production_order`: meterla ahí obligaría a inventar un origen falso en `exactly_one_origin` y un almacén.

### Decisión 7 — Motor de sugerencias (§38–§49, §92)

Para una orden (o carga) y un ciclo, lista hornadas que cumplan TODO:

- mismo `firing_type`; estado PLANNED; `scheduled_date` ≥ hoy (fecha de negocio de Lima);
- no `exclusive`; y si lo pedido es EXCLUSIVE, solo hornadas vacías;
- capacidad libre > 0 para las piezas restantes.

Orden: primero las que admiten TODO lo restante, luego fecha más cercana, luego el ajuste más justo (menos
libre sobrante), luego horno. Cada una dice «Capacidad estimada disponible: X %» y cuánto del pedido
cubriría. **Nunca autoasigna** (§46). Siempre se ofrece además «Crear hornada nueva» (§47). Wording: nunca
«caben garantizado» (§40–§41).

### Decisión 8 — Baja y alta (§54, §55)

Hoy **no existe** regla de secuencia. Propuesta mínima, a validar por Gemini: para una pieza que necesita
AMBOS ciclos, no se puede **arrancar** una hornada ALTA que contenga piezas de esa línea mientras sus piezas
no hayan pasado por una hornada BAJA COMPLETED. Si Gemini lo considera invención, se deja solo como aviso.

### Decisión 9 — Orígenes asignables

V2_QUOTATION y FIRING_V2 (volumen congelado con separación) e INTERNAL. **Legacy y prototipos NO** se asignan
en 010L: no tienen un volumen aprobado con separación y Legacy se retira en 010N. Documentado, no oculto.

### Decisión 10 — API (naming del repo)

- `GET /kiln-batches` (filtros: horno, ciclo, desde/hasta, estado, origen; paginado; una sola consulta con
  agregados, sin N+1) · `POST /kiln-batches` · `GET /kiln-batches/{id}` · `PUT /kiln-batches/{id}` (fecha,
  notas; con `expected_version`) · `POST /kiln-batches/{id}/start` · `/complete` · `/cancel`;
- `POST /kiln-batches/{id}/assignments` (lote de líneas y cantidades, `expected_version`, idempotente) ·
  `POST /kiln-batches/{id}/assignments/{aid}/release` · `POST /kiln-batches/moves` (origen, destino, líneas);
- `GET /production-orders/{id}/firing-plan` (piezas por ciclo: requeridas, asignadas, restantes; hornadas)
  · `GET /production-orders/{id}/batch-suggestions?firing_type=` ;
- `POST /firing-quotations-v2/{id}/send-to-production` (ADMIN) · origen `v2_firing_quotation_id` en
  `POST /production-orders`;
- `internal-loads`: CRUD mínimo + `firing-plan` + `batch-suggestions` iguales.

RBAC: lectura `CurrentUserDep`; planificar (crear, asignar, quitar, mover, arrancar, completar)
`WorkshopUserDep`; cancelar hornada solo ADMIN (espejo de anular orden). Ningún importe en ningún contrato.

### Decisión 11 — Frontend

`/produccion/hornadas`: lista por fecha con filtros, barra de capacidad (ocupado / disponible), estado, horno,
ciclo, órdenes. Ficha de hornada: capacidad, asignaciones por orden (código, origen, cliente, productos,
% asignado), arrancar/completar/cancelar. En la ficha de la orden: panel «Planificación de hornadas» con
piezas por ciclo, sugerencias, asignar con cantidad, quitar y mover. Solo Quema: botón «Enviar a producción».
Sin posiciones físicas (§57).

### Decisión 12 — Pruebas

Unitarias del motor (65+20=85; 65+40 rechazado; exclusiva; compartida; multihornada 100+20; LOW≠HIGH;
sugerencias 3→2). DB: concurrencia real con dos sesiones (35 % libre, 30 % y 20 %), idempotencia, snapshot
de capacidad, estados, CTZ y PDF idénticos antes/después de mover (JSON + hash del PDF), K5 una sola orden
bajo doble clic y concurrencia, vidriado que llega, carga interna sin CTZ, rendimiento 50 hornadas / 100
órdenes con número de consultas acotado. E2E de revisión §100–§103. Regresiones 010I, 010J (11 864,90), 010K
(4 157,73).

### Migración

`0040_kiln_batches`, aditiva: talonarios `KILN_BATCH` y `INTERNAL_LOAD`, `kiln_batches`,
`kiln_batch_assignments`, `internal_loads`, `internal_load_lines`, `v2_firing_production_handoffs`,
`production_orders.v2_firing_handoff_id` y el CHECK de cuatro orígenes. No edita 0039. Cabeza única.
`downgrade` se niega si hay datos.

### Bloques

L1 modelo + migración + estados · L2 asignaciones + capacidad + concurrencia · L3 K5 · L4 sugerencias ·
L5 frontend · L6 E2E + integración. Gate Gemini + Codex entre bloques.

---

## REVISIÓN 1 — tras las auditorías del plan

Gemini: PASS. Codex: FAIL (2 BLOCKER, 2 HIGH, 2 MEDIUM). Todos los hallazgos de Codex se verificaron contra el
código y se aceptan. Decisiones de negocio de Gemini incorporadas.

### R1 [Codex BLOCKER 1] Capacidad: garantía REAL en la base

El CHECK sobre un contador solo vale si el contador es verdad. Se hace verdad por construcción:

- **trigger** `AFTER INSERT OR UPDATE OR DELETE` en `kiln_batch_assignments` que aplica el **delta** al
  contador de la hornada: `UPDATE kiln_batches SET assigned_volume_cm3 = assigned_volume_cm3 + delta`.
  Alta ACTIVE suma; ACTIVE→RELEASED resta; cambio de cantidad ajusta; DELETE de una ACTIVE resta.
  Delta y no `SUM(...)`: bajo READ COMMITTED el UPDATE que espera un bloqueo reevalúa sobre la ÚLTIMA versión
  de la fila (EvalPlanQual), así que los deltas componen bien; un `SET = (SELECT SUM ...)` podría leer una
  foto vieja y perder la suma del otro;
- el **CHECK** `assigned_volume_cm3 <= capacity_snapshot_cm3` actúa sobre ese valor: ninguna escritura, venga
  de donde venga, deja una hornada por encima del 100 %;
- el servicio sigue bloqueando la hornada `FOR UPDATE` antes, para validar exclusividad y ciclo y devolver un
  409 limpio en vez de un error de restricción;
- pruebas: contador == SUM(ACTIVE) tras operaciones concurrentes, y una inserción DIRECTA por SQL que se salta
  el servicio y aun así es rechazada.

El volumen de cada asignación se guarda en la fila (`assigned_volume_cm3 = quantity × unit_volume_snapshot`)
con CHECK de que coincide, para que el delta nunca dependa de otra tabla.

### R2 [Codex BLOCKER 2] Restricciones de `kiln_batch_assignments`

- `source_kind` V2_QUOTATION | FIRING_V2 | INTERNAL, con CHECK por ramas que nombra LOS CINCO campos:
  - V2_QUOTATION ⇒ `production_order_id` y `v2_quotation_product_id`; los demás nulos;
  - FIRING_V2 ⇒ `production_order_id` y `v2_firing_quotation_line_id`; los demás nulos;
  - INTERNAL ⇒ `internal_load_id` y `internal_load_line_id`; los demás nulos.
  Eso da «exactamente un origen de línea» y «exactamente un padre» a la vez, y la coherencia entre los dos;
- INTERNAL: **FK compuesta** `(internal_load_line_id, internal_load_id)` → `internal_load_lines(id, load_id)`
  con UNIQUE `(id, load_id)`: la base impide colgar la línea de otra carga;
- V2 y Solo Quema: la cadena orden → puente → cotización → línea tiene tres saltos y no cabe en una FK. Se
  valida en el servicio BAJO EL BLOQUEO de la orden, resolviendo la cotización de SU puente y aceptando solo
  líneas de ella; prueba explícita de «línea ajena» rechazada (422);
- UNIQUE parcial `(batch_id, línea) WHERE status = 'ACTIVE'` por cada tipo de línea: una línea tiene como mucho
  una asignación activa por hornada (se ajusta la cantidad, no se duplica la fila);
- piezas activas por (línea, ciclo) ≤ cantidad de la línea: validado bajo el bloqueo de la orden/carga.

### R3 [Codex HIGH 3] Idempotencia de operaciones en lote

Tabla `kiln_batch_operations`: `idempotency_key` UNIQUE, `kind` (ASSIGN | RELEASE | MOVE | CREATE_BATCH),
`payload_fingerprint` (SHA-256 del payload canónico), `batch_id`, resultado, autor, fecha. Patrón real de
`production.py:708-733`: bloqueo consultivo por clave; misma clave + misma huella → se devuelve el resultado
original sin reaplicar; misma clave + huella distinta → 409 `IDEMPOTENCY_KEY_REUSED`. Pruebas de los dos casos
en asignar, quitar y mover.

### R4 [Codex HIGH 4] Solo Quema con puente no se anula

`V2FiringQuotationService.cancel` hoy no mira el puente (`firing_quotation_v2.py:1036`). Pasa a hacer lo mismo
que V2 (`quoter_v2_lifecycle.py:472`): bloquea la cabecera, busca el puente DESPUÉS del bloqueo y rechaza
con 409 si existe. Y `send_to_production` bloquea la misma cabecera, así que la carrera anular-contra-enviar
queda serializada: gana uno y el otro ve el resultado. Prueba de la carrera.

### R5 [Codex MEDIUM 5] Talonarios

`SequenceType.KILN_BATCH` e `INTERNAL_LOAD`, y el CHECK de `document_sequences` ampliado en 0040 (hoy acaba en
`FIRING_V2`, `sequence.py:125`). Semilla en `tests/db/conftest.py`.

### R6 [Codex MEDIUM 6] «Hoy» es Lima

Toda comparación de fecha futura (sugerencias, reprogramar, crear) usa `business_date(db_now())` de
`quoter_v2_lifecycle.py:86`, nunca `date.today()` ni UTC.

### Decisiones de negocio de Gemini

- **Baja antes que alta: BLOQUEO** (Decisión 8). Arrancar una hornada ALTA se rechaza si contiene piezas de
  una línea que necesita AMBOS ciclos y esas piezas no pasaron por una hornada BAJA COMPLETED. Se evalúa **por
  línea y cantidad**: si una línea se partió en dos bajas, en alta solo pueden arrancar tantas piezas como ya
  completaron su baja. Una línea que solo necesita alta (Solo Quema de pieza ya bizcochada) no se bloquea;
- **Puente en DOS PASOS** (Decisión 4), como V2;
- **Sin READY**.
- Observación de Gemini aplicada: al quedar vacía una hornada PLANNED, `exclusive` vuelve a `false`.

### MUST_FIX LOW de Gemini — sin cambio, con evidencia

Pedía que `POST /production-orders` recibiera el id del puente «como V2». El V2 real recibe `v2_quotation_id`
—la COTIZACIÓN— y el servicio resuelve el puente (`app/schemas/production.py:45`, `production.py:680`). Por
tanto `v2_firing_quotation_id` ya es la paridad exacta. NO_CHANGE_NEEDED.

### Huecos de prueba de Codex: todos entran en el plan de pruebas

Migración 0040 (cabeza única, talonarios, CHECK de 4 orígenes, filas Legacy/prototipo/V2 existentes intactas,
downgrade con datos); restricciones de asignación; drift del contador; concurrencia real (35 %: 30 % + 20 %) y
movimientos cruzados A→B / B→A sin interbloqueo; idempotencia misma/distinta huella; K5 doble clic y carrera
anular-enviar, una sola orden, hash del PDF igual; RBAC y ausencia de importes en todos los contratos nuevos;
número de consultas del listado con 50 hornadas / 100 órdenes; «hoy» de Lima.

---

## REVISIÓN 2 — cierre del MUST_FIX 1 de Codex (capacidad)

Codex cerró 2–6 y dejó abierto el 1: el CHECK de fila no blinda la capacidad si una cantidad o un volumen
pudieran ser negativos, porque un delta negativo **restaría** del contador y abriría hueco. Se declaran
explícitamente, además de los de R1:

**En `kiln_batch_assignments`:**

- `quantity > 0` — una asignación de cero piezas no es una asignación;
- `unit_volume_snapshot_cm3 > 0` — estrictamente positivo: una línea sin medidas no ocupa horno y NO es
  asignable (el servicio la rechaza con 422 antes; el CHECK es la garantía);
- `assigned_volume_cm3 > 0`;
- `assigned_volume_cm3 = quantity * unit_volume_snapshot_cm3` — el volumen de la fila es exactamente el de sus
  piezas, así que el delta nunca depende de otra tabla.

**En `kiln_batches`:**

- `capacity_snapshot_cm3 > 0`;
- `assigned_volume_cm3 >= 0`;
- `assigned_volume_cm3 <= capacity_snapshot_cm3`.

**Delta del trigger, definido con exactitud:** `delta = vol_activo(NEW) - vol_activo(OLD)`, donde
`vol_activo(fila) = fila.assigned_volume_cm3` si `fila.status = 'ACTIVE'` y `0` en otro caso (y `0` si la fila
no existe: OLD en INSERT, NEW en DELETE). Cubre INSERT, UPDATE de estado, UPDATE de cantidad y DELETE. Si una
UPDATE cambiara `batch_id` (no lo hace ningún camino: mover = RELEASED en origen + ACTIVE en destino), el
trigger resta en la hornada vieja y suma en la nueva. Con `quantity > 0` y volúmenes positivos, el único modo
de que el contador baje es liberar volumen que de verdad estaba asignado.

Pruebas añadidas: una fila con cantidad 0, cantidad negativa o volumen 0 rechazada por la base; contador ==
SUM(ACTIVE) tras INSERT, RELEASE, cambio de cantidad y DELETE hechos por SQL directo.

**Regla baja antes que alta**, con las condiciones que pidió Codex: al arrancar una ALTA se cuentan solo
asignaciones LOW `ACTIVE` de hornadas `COMPLETED` (las RELEASED y las de hornadas CANCELLED no cuentan), por
línea, y se evalúa bajo el bloqueo de la hornada que arranca y de las órdenes/cargas implicadas.
