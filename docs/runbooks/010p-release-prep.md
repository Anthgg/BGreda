# GREDA 010P — preparación del release local

**Alcance:** preparación y auditoría local. Este documento no ejecuta pasos de release. No hacer push, merge, tag, deploy, migración, backup productivo, cambio IAM, cambio de tráfico ni limpieza de datos desde esta fase.

## Baseline comprobado

| Elemento | Estado de auditoría |
| --- | --- |
| Backend W4 code RC | `00a0d65df4ff328133d80ec0fe91cf54b6dc90ce` |
| Backend W4 evidence HEAD | `84511b82cc920f3a7db84486d50a013c01171cac` |
| Frontend W4 code RC | `0330bb90cdf2145973afcadef482f2578ffc9e0c` |
| Frontend W4 evidence HEAD | `8b28497ee1277e7ba0d1ce7252921b0fd4d0dbcb` |
| Backend / frontend branch | `feat/010p-w4-integration` |
| Backend / frontend origin/main | ancestro de la rama de fase en la auditoría; no se hizo push |
| Alembic productivo | `0041`; destino del RC `0045` |
| Backend productivo actual | `bgreda-api-00089-hew` |
| Frontend productivo actual | `fgreda-web-00104-hil` |
| Frontend runtime SA actual | `303244958634-compute@developer.gserviceaccount.com` |
| Producción | sin cambios en esta fase |

Los commits de preparación solo pueden cambiar configuración de release, scripts, workflows de CI y runbooks. Los SHAs W4 anteriores permanecen como RC funcional; los nuevos HEAD de esta rama son SHAs de preparación, no una recertificación funcional.

## Puertas manuales de PR y CI

No hay PR abierto para estos cambios. La secuencia futura es backend primero y frontend después: publicar la rama, abrir PR contra `main`, revisar y exigir que todos los checks indicados estén verdes en el SHA exacto del PR, y hacer squash merge. No repetir manualmente las suites W4; dejar que corra CI normal salvo conflicto, cambio funcional o regresión detectada.

**Checks backend obligatorios:**

- `CI / Lint, tipos y tests`
- `CI / Imagen de contenedor`
- `CI / Validar configuración Cloud Build`

**Checks frontend obligatorios:**

- `CI / Lint, tipos, tests y build`
- `CI / Imagen de contenedor`
- `CI / Validar configuracion Cloud Build y scripts de deploy`
- `CI / E2E de la revision (Chromium)`

GitHub no los marca como required checks en branch protection; el responsable del PR debe revisar manualmente cada check. Los scripts de candidato exigen `CI_RELEASE_GATES_VERIFIED=YES` como acuse manual y `EXPECTED_010P_MERGE_SHA` como el SHA completo del squash merge. Ambos comparan HEAD, `origin/main` y el SHA esperado, y abortan si no son idénticos o el árbol tiene archivos modificados/no rastreados.

El E2E local de la revisión sigue ejecutándose en PR contra PostgreSQL efímero. El E2E que usa `E2E_BASE_URL` de producción se retiró del evento PR y del cron semanal; ahora solo se puede iniciar manualmente con `run_production_e2e=true`, cuyo texto advierte que puede dejar datos persistentes `E2E-*`.

## Imágenes y pipelines

Convención: `p010p-<SHA completo de squash merge>` como etiqueta de consulta en Artifact Registry y Cloud Run. Resolver `sha256:<64 hex>` después del push y desplegar por `IMAGE@sha256:...`; la etiqueta nunca selecciona el artefacto para desplegar.

Los pipelines backend y frontend construyen, publican la etiqueta inmutable por SHA, resuelven el digest, despliegan con `--no-traffic --tag=p010p-<sha>` y verifican `Ready`, imagen por digest, URL de tag, candidato con 0% y distribución de tráfico del servicio que todavía suma 100%. Imprimen `BUILD_ID`, digest, revisión y tag URL. No ejecutan smoke ni `update-traffic`; el smoke queda explícito para el operador y el cutover es un comando manual independiente. El despliegue sin tráfico/tag y el cambio posterior de tráfico están descritos en [Cloud Run rollouts](https://cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration).

La app frontend es Nginx estático con configuración pública `API_BASE_URL`/`BACKEND_ORIGIN`; no contiene cliente GCP, lectura de Secret Manager ni credenciales privadas. Matriz de runtime frontend:

| Permiso | Por qué | Alcance |
| --- | --- | --- |
| Ninguno | Assets estáticos y config pública; no llama GCP | No aplica |
| `roles/secretmanager.secretAccessor` | No requerido; frontend no lee secretos | No conceder |
| `roles/storage.objectCreator` | No requerido por frontend runtime | No conceder |
| `roles/run.invoker` | Nginx proxy no envía token de identidad al backend | No conceder a frontend runtime |
| `roles/iam.serviceAccountUser` | El principal que despliega necesita adjuntar la SA runtime | Binding sobre la SA dedicada solamente |

Runtime frontend requerido: **roles NONE; secrets NONE**. La futura cuenta `fgreda-web-runtime@cotizador-greda.iam.gserviceaccount.com` debe crearse antes de desplegar; no crearla durante prep. Identificar el principal exacto de despliegue en la configuración real de Cloud Build antes de conceder el binding.

Comandos futuros, aún no aplicados:

```bash
gcloud iam service-accounts create fgreda-web-runtime \
  --project=cotizador-greda \
  --display-name='FGreda web runtime (static frontend)'

# Sustituir el miembro por la cuenta de servicio que Cloud Build use para
# desplegar; el binding queda en la SA dedicada, no en el proyecto.
gcloud iam service-accounts add-iam-policy-binding \
  fgreda-web-runtime@cotizador-greda.iam.gserviceaccount.com \
  --project=cotizador-greda \
  --member='serviceAccount:<CLOUD_BUILD_DEPLOYER_SA>' \
  --role=roles/iam.serviceAccountUser
```

La configuración frontend nueva selecciona esta SA al crear la revisión. El deploy debe abortar si el runtime actual no coincide con ella. No hay roles de runtime que otorgar ni secrets a añadir.

El backend sigue ejecutando como `bgreda-api-sa@cotizador-greda.iam.gserviceaccount.com`. Sus permisos observados son `roles/secretmanager.secretAccessor` con alcance individual en cada uno de los siete secrets referenciados por el servicio (`supabase-publishable-key`, `database-url`, `csrf-secret`, `supabase-secret-key`, `identity-hash-secret`, `peru-api-token`, `decolecta-api-token`) y `roles/storage.objectCreator` solo sobre `cotizador-greda-db-backups`. No se detectó rol amplio en el proyecto para la SA backend. La cuenta default de Compute sí tiene `roles/editor` y `roles/secretmanager.secretAccessor` a nivel proyecto y no debe seguir siendo runtime del frontend.

## Job de migración

Auditoría actual de `bgreda-db-migrate`: imagen fijada por digest, pero anterior y no certificada como el RC W4: `bgreda-api@sha256:da675e2d58755035cda927006932510e40f61e5a43f8b80e0b65e026b9e29442`; comando `bash`; un contenedor/tarea; `bgreda-api-sa`; `DATABASE_URL` referencia Secret Manager `database-url:latest`; timeout 600 s; retries 0; task count 1. El script del job se conserva.

Después de squash merge/build backend, confirmar el digest del artefacto. El job se actualiza solo por digest y su config debe mostrar la imagen esperada, secret reference, SA, command/args y límites sin cambios:

```bash
BACKEND_IMAGE='southamerica-west1-docker.pkg.dev/cotizador-greda/cloud-run-source-deploy/bgreda-api'
BACKEND_DIGEST='sha256:<64-hex-digest-del-build-010P>'

gcloud run jobs update bgreda-db-migrate \
  --image="${BACKEND_IMAGE}@${BACKEND_DIGEST}" \
  --region=southamerica-west1 \
  --project=cotizador-greda

gcloud run jobs describe bgreda-db-migrate \
  --region=southamerica-west1 \
  --project=cotizador-greda
```

El config Cloud Build de migración exige digest hex y valida que la imagen contenga un único Alembic head `0045`. Para ejecutar la migración en la futura ventana autorizada, el operador debe ejecutar el job manualmente con `gcloud run jobs execute bgreda-db-migrate --region=southamerica-west1 --project=cotizador-greda --wait`. Ese comando no se ejecuta durante prep.

## Backup y manifest

Job existente `bgreda-db-backup`: imagen `postgres:17` sin pin de digest, SA backend, secreto `database-url:latest`, timeout 1200 s, retries 0, una tarea. El script vigente crea un objeto `before-0028`, no comprueba que el origen sea 0041 y no genera el manifest requerido. Sus logs históricos no prueban por sí solos el checksum y la TOC de un backup 010P. El bucket identificado por el job es `cotizador-greda-db-backups`; la SA del backend tiene `roles/storage.objectCreator` a nivel bucket.

La nueva imagen se construye con `cloudbuild.backup.yaml`; ese pipeline solo construye/publica y resuelve digest. No ejecuta el job. Tras build backend/frontend y antes de la ventana, preparar el job con el digest exacto y valores capturados de las revisiones productivas. `--update-env-vars` conserva la referencia secreta ya configurada; `--args=''` borra el script base64 antiguo:

```bash
BACKUP_IMAGE='southamerica-west1-docker.pkg.dev/cotizador-greda/cloud-run-source-deploy/bgreda-backup'
BACKUP_DIGEST='sha256:<64-hex-digest-del-build-de-backup>'

gcloud run jobs update bgreda-db-backup \
  --image="${BACKUP_IMAGE}@${BACKUP_DIGEST}" \
  --command=/usr/local/bin/backup-010p.sh \
  --args='' \
  --update-env-vars="BACKUP_BUCKET=cotizador-greda-db-backups,RELEASE_BACKEND_SHA=<backend-merge-sha>,RELEASE_FRONTEND_SHA=<frontend-merge-sha>,BACKEND_PROD_REVISION=<revision-backend-capturada>,FRONTEND_PROD_REVISION=<revision-frontend-capturada>" \
  --region=southamerica-west1 \
  --project=cotizador-greda
```

El script aborta si no corre como Cloud Run Job, si no tiene `DATABASE_URL`, si Alembic no está exactamente en `0041`, si falta/quiebra la tabla de Alembic o `public.quotations` en la TOC, si la carga a Storage no devuelve objeto/tamaño/MD5 coincidentes, o si la identidad no valida las variables requeridas. No imprime URL/token. El dump es formato custom; calcula bytes y SHA256 y verifica `pg_restore --list`. Cloud Storage valida `Content-MD5` y devuelve hash del objeto ([validación de datos](https://docs.cloud.google.com/storage/docs/data-validation?hl=en), [Objects: insert](https://docs.cloud.google.com/storage/docs/json_api/v1/objects/insert)). Crea objeto `bgreda-prod-before-010p-<UTC>-<execution>.dump` y sidecar `.manifest.json` con release, revisiones Alembic, timestamps UTC, bucket/object, tamaño, SHA256, entradas TOC, estado del restore-list, ejecución Cloud Run, revisiones backend/frontend fuente y SHAs 010P. `CLOUD_RUN_EXECUTION` lo proporciona el runtime de Cloud Run Jobs ([contrato runtime](https://docs.cloud.google.com/run/docs/container-contract)). El ejemplo vacío está en `010p-backup-manifest.example.json`.

La ejecución futura debe guardar el stdout estructurado y descargar el sidecar como evidencia local protegida. Comparar SHA y tamaño del log con el manifiesto, guardar el URI `gs://...`, validar `PG_RESTORE_LIST_STATUS=PASS` y verificar que el objeto sea el recién creado:

```bash
gcloud storage cp "gs://$BACKUP_BUCKET/$MANIFEST_OBJECT" "$EVIDENCE_DIR/"
python -m json.tool "$EVIDENCE_DIR/$(basename "$MANIFEST_OBJECT")" >/dev/null
```

El nombre antiguo `before-0028` no cuenta como respaldo 010P. Este job aún no se actualizó ni ejecutó.

## Inventario de writers, scheduler y disparadores

| Servicio / mecanismo | Puede escribir DB | Automático | Estado observado |
| --- | --- | --- | --- |
| `bgreda-api` | Sí, en requests de negocio | Sí, request-driven | Servicio activo; tráfico 100%; escritor productivo principal |
| `fgreda-web` | No directamente; el navegador usa API | Request-driven | Nginx/proxy; ninguna credencial DB ni GCP runtime requerida |
| `bgreda-db-migrate` | Sí | No | Job manual; sin trigger Cloud Build visible |
| `bgreda-db-backup` | No (lectura DB); escribe solo objetos GCS | No | Job manual; no ejecutado en prep |
| Cloud Build triggers | Podrían iniciar pipelines | No observado | `0` triggers en el proyecto; builds/pipelines del release son manuales |
| GitHub Actions frontend E2E | Sí antes de este cambio, vía API E2E | PR/crontab antes de este cambio | En la rama local queda manual con confirmación; necesita PR/merge para regir en GitHub |
| GitHub Actions E2E de revisión | Solo PostgreSQL efímero | PR/push | No producción; permanece como gate |
| Cloud Scheduler / Cloud Tasks | Podrían activar jobs si estuviesen configurados | Desconocido | API Cloud Scheduler y Cloud Tasks no aparecen habilitadas. No habilitarlas durante prep. No se pudo listar Scheduler jobs con API apagada |

Se buscaron invocaciones de `bgreda-db-migrate`, `gcloud run jobs execute`, Cloud Scheduler/Tasks y cron en configuración, scripts, docs y workflows locales; no hay path automático visible. Por la imposibilidad de consultar Scheduler sin habilitar su API, **AUTOMATIC_MIGRATION_PATH queda UNKNOWN**, no `NO`. Gate 2 debe resolver esta incógnita mediante inventario autorizado de Cloud Scheduler (o confirmación autoritativa de que el proyecto no tiene jobs) antes de migrar.

## Maintenance lock y drenaje

El lock preparado es IAM en el servicio API; ingress sigue configurado como `all`. No usar un cambio de ingress como sustituto sin validar previamente el camino privado/red. La policy API actual incluye `allUsers:roles/run.invoker`; se retira temporalmente al abrir la ventana. Antes de tocarla, guardar fuera de Git la configuración completa del servicio, policy y datos de revisión/tráfico/ingress/SA:

```bash
EVIDENCE_DIR='artifacts/local/010p-release-<execution-id>'
mkdir -p "$EVIDENCE_DIR"
gcloud run services describe bgreda-api --region=southamerica-west1 --project=cotizador-greda --format=json > "$EVIDENCE_DIR/backend-service-before.json"
gcloud run services get-iam-policy bgreda-api --region=southamerica-west1 --project=cotizador-greda --format=json > "$EVIDENCE_DIR/backend-iam-before.json"
gcloud run services describe fgreda-web --region=southamerica-west1 --project=cotizador-greda --format=json > "$EVIDENCE_DIR/frontend-service-before.json"
gcloud run services get-iam-policy fgreda-web --region=southamerica-west1 --project=cotizador-greda --format=json > "$EVIDENCE_DIR/frontend-iam-before.json"
```

Identificar un principal humano/de release ya autorizado; no crearle rol de proyecto. Conceder invocación solo sobre `bgreda-api`, comprobar el request autenticado, retirar `allUsers` y comprobar request anónimo `403`/denied:

```bash
RELEASE_SMOKE_MEMBER='user:<release-operator-authorized-account>'
BACKEND_SERVICE_URL='https://bgreda-api-303244958634.southamerica-west1.run.app'
BACKEND_TAG_URL='<tag-url-p010p-devuelta-por-el-build>'

gcloud run services add-iam-policy-binding bgreda-api \
  --region=southamerica-west1 --project=cotizador-greda \
  --member="$RELEASE_SMOKE_MEMBER" --role=roles/run.invoker
gcloud run services remove-iam-policy-binding bgreda-api \
  --region=southamerica-west1 --project=cotizador-greda \
  --member=allUsers --role=roles/run.invoker

curl -sS -o /dev/null -w '%{http_code}\n' "$BACKEND_SERVICE_URL/live"  # esperado: 403
ID_TOKEN="$(gcloud auth print-identity-token --audiences="$BACKEND_SERVICE_URL")"
curl -fsS -H "Authorization: Bearer $ID_TOKEN" "$BACKEND_TAG_URL/live"
unset ID_TOKEN
```

Para una URL de revisión con tag, el token debe llevar como audience la URL base del servicio, no la tag URL; así define Cloud Run la autenticación servicio-a-servicio ([documentación oficial](https://docs.cloud.google.com/run/docs/authenticating/service-to-service?hl=en)). Si el operador no tiene una identidad autorizada o el tag no acepta ese request, abortar y mantener el estado seguro. Después del release, reponer el binding público que estaba en el snapshot, retirar solo el principal temporal si no existía antes y comparar la policy normalizada contra la captura; detenerse ante cualquier cambio concurrente de IAM.

`bgreda-api` tiene timeout máximo observado 300 s y concurrencia 40. Drenar después del lock por al menos **360 s** (timeout 300 s + margen 60 s), pero no usar el reloj como prueba única. Luego permitir hasta 120 s de visibilidad del dato y verificar dos ventanas consecutivas completas de 60 s de `run.googleapis.com/container/max_request_concurrencies` con `state=active` sin concurrencia registrada para `bgreda-api`; revisar también request logs hasta después del lock y tomar dos snapshots de solo lectura de `pg_stat_activity` sin transacciones DML activas ajenas al proceso de inspección. No imprimir texto de query/PII. Si hay muestras activas, logs tardíos, conexión DML abierta o métrica/logs no disponibles, no migrar; seguir esperando o abortar.

`DRAIN_COMPLETE=YES` exige todos los puntos anteriores y registro UTC del último request previo al lock. Los requests se registran al terminar; por eso el timeout del servicio determina el primer tiempo de espera y la latencia de publicación de métricas se considera aparte. Referencias del metric y sus ventanas están en [métricas de Cloud Run](https://docs.cloud.google.com/monitoring/api/metrics_gcp_p_z) y [monitorización de Cloud Run](https://docs.cloud.google.com/run/docs/monitoring).

## Compatibilidad y smoke

`OLD_FRONTEND_NEW_BACKEND=CONDITIONAL`. El frontend productivo `fgreda-web-00104-hil` sigue usando APIs existentes; W4 mantiene V1 histórico read-only y añade superficies 010P/V2. La certificación W4 cubre la combinación W4; no demuestra cada flujo del frontend antiguo frente al backend nuevo. Además, el proxy Nginx antiguo/nuevo no presenta identidad Cloud Run, así que durante el lock el navegador no puede completar login ni llamadas API. Mantener el lock hasta que la revisión frontend 010P esté `Ready`, se haya validado su carga/configuración estática, esté en tráfico y se esté listo para restaurar el acceso API. No anunciar `SAFE_FOR_ALL_EXISTING_FLOWS` en el intervalo.

Con el API locked, probar backend candidate por tag con ID token: `/live`, `/ready`, `/api/v1/auth/csrf` y únicamente GETs seguros con identidad aprobada. El backend target no puede desplegar antes de DB 0045: `NEW_BACKEND + OLD_DB = UNSAFE`. El smoke de frontend con API, login y lecturas de negocio se realiza justo después del unlock y solo con cuenta aprobada y lecturas no mutantes; bajo lock se limita a `/` y `/runtime-config.js`. No crear cotizaciones, productos, órdenes, inventario ni datos de prueba en producción.

Después del unlock, la lectura final puede cubrir `/live`, `/ready`, login, RBAC denial, productos, lecturas históricas de cotización, PDF V1 histórico, V2 existente/PDF si aplica, inventory, kilns, WIP y Solo Quema. Login significa autenticación con cuenta existente; no crear una cuenta ni datos comerciales. No mandar POST/PUT/PATCH/DELETE.

## Invariantes post-0045

`docs/runbooks/010p-post-0045-invariants.sql` reproduce los checks W4 aplicables y añade movimientos huérfanos, aplicación duplicada por origen, y delivery superior al saldo disponible previo según la secuencia de movimientos. Es una transacción read-only con timeout 30 s. No ejecutarla durante esta preparación.

Después de migrar, exigir `alembic_head=0045`, `all_invariants_zero=true` y todos los contadores cero: saldo negativo, saldo negativo por lote, diferencias agregado/lotes, lote sin agregado, movimientos huérfanos, origen duplicado, secuencia `balance_after`, `PRODUCTION_IN` vs resultados, totales inválidos, entregas superiores al saldo disponible y movimientos sin origen requerido. El cálculo de saldo previo deriva el inventario de apertura desde el saldo agregado actual menos todos los movimientos, para no asumir que la primera fila del ledger parte de cero. Guardar el JSON resultante en evidencia local protegida; si falla un conteo, mantener API locked y no routear.

## Orden controlado y comandos de tráfico

Orden recomendado para que el respaldo sea el último estado estable: (1) revisar PRs/CI, capturar revisiones, tráfico, policies, digest y plan; (2) probar principal release y aplicar API lock; (3) drenar; (4) actualizar el job de backup a digest y ejecutarlo una sola vez desde 0041; (5) revisar manifest/dump; (6) actualizar `bgreda-db-migrate` a digest backend 010P y ejecutar manualmente 0041→0045; (7) verificar Alembic 0045 e invariantes; (8) ejecutar el script de candidato backend, verificar por tag y hacer smoke autenticado read-only; (9) cambio manual backend; (10) validar frontend candidate con su SA y smoke estático; (11) cambio manual frontend; (12) restaurar acceso invoker público según snapshot; (13) login y smoke de lectura; (14) observar errores, latencia, auth y DB por 30 minutos. Si cualquier gate falla, detenerse en la ventana y preservar el lock.

Los scripts solo preparan candidatos. Cuando el release gate autorice cutover, operador ejecuta, usando nombres de revisión exactos devueltos por VERIFY:

```bash
gcloud run services update-traffic bgreda-api \
  --region=southamerica-west1 --project=cotizador-greda \
  --to-revisions='<backend-010p-revision>=100'

gcloud run services update-traffic fgreda-web \
  --region=southamerica-west1 --project=cotizador-greda \
  --to-revisions='<frontend-010p-revision>=100'
```

Rollback antes de DB: abortar y restaurar policy/traffic capturados. Si la migración falla, seguir locked, no desplegar y forward-fix o restaurar backup por decisión explícita. Si backend falla luego de migrar, seguir locked; revisar compatibilidad antes de routear backend anterior `bgreda-api-00089-hew`. Si frontend falla, conservar DB/backend 010P; routear frontend anterior `fgreda-web-00104-hil` solo si su compatibilidad está confirmada; si no, permanecer locked y forward-fix. Después de escrituras reales en 0045, no hacer downgrade Alembic automático. Restaurar backup es último recurso y requiere una decisión explícita sobre pérdida de writes posteriores.

Una vez disponible todo el paquete candidato, probar la revisión frontend/backend con no-traffic, sin cambiar asignaciones de tráfico. El smoke bajo lock se limita a la API autenticada y frontend estático por la limitación de identidad Nginx descrita arriba. La ruta frontend→API se verifica con cuenta aprobada inmediatamente después del unlock.

## Bloqueo pendiente para Gate 2

La API de Cloud Scheduler está deshabilitada y no se habilitó para esta auditoría. Cero triggers Cloud Build, ausencia de referencia a ejecutar `bgreda-db-migrate` en código/config local, y Cloud Tasks no habilitado son evidencias favorables, pero no permiten afirmar ausencia de Scheduler jobs. Resolverlo por canal autorizado antes de autorizar la ventana; mientras siga sin inventario, `AUTOMATIC_MIGRATION_PATH=UNKNOWN` y `READY_FOR_RELEASE_GATE_2=NO`.
