#!/usr/bin/env bash
# deploy-backend.sh — Release reproducible de BGreda a Cloud Run.
#
# USAR SIEMPRE DESDE LA RAÍZ DEL REPOSITORIO.
# Solo despliega desde main. Nunca desde un working tree sucio o feature branch.
#
# Uso:
#   ./scripts/deploy-backend.sh
#
# Prerequisitos:
#   - gcloud auth login && gcloud config set project cotizador-greda
#   - cloud-sdk instalado y en PATH
#
# El script:
#   1. Verifica que el working tree está limpio
#   2. Verifica que el HEAD local coincide con origin/main
#   3. Ejecuta Cloud Build con el SHA exacto
#   4. Informa la revisión y URL del Cloud Run resultante

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
PROJECT="cotizador-greda"
REGION="southamerica-west1"
SERVICE="bgreda-api"
CLOUDBUILD_CONFIG="cloudbuild.yaml"

# ---------------------------------------------------------------------------
# Colores
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[DEPLOY]${NC} $*"; }
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ---------------------------------------------------------------------------
# 1. Verificar working tree limpio
# ---------------------------------------------------------------------------
log "Verificando que el working tree está limpio..."
if ! git diff --quiet || ! git diff --cached --quiet; then
  err "El working tree tiene cambios no confirmados."
  err "STATUS: DEPLOY_ABORTED_DIRTY_WORKING_TREE"
  git status --short
  exit 1
fi
ok "Working tree limpio."

# ---------------------------------------------------------------------------
# 2. Verificar que estamos en main
# ---------------------------------------------------------------------------
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "main" ]; then
  err "No estás en la rama main (rama actual: $CURRENT_BRANCH)."
  err "Los deploys de producción solo se ejecutan desde main."
  err "STATUS: DEPLOY_ABORTED_NOT_ON_MAIN"
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. Verificar que el HEAD local coincide con origin/main
# ---------------------------------------------------------------------------
log "Sincronizando con origin..."
git fetch origin main --quiet

LOCAL_HEAD=$(git rev-parse HEAD)
ORIGIN_HEAD=$(git rev-parse origin/main)

if [ "$LOCAL_HEAD" != "$ORIGIN_HEAD" ]; then
  err "El HEAD local ($LOCAL_HEAD) no coincide con origin/main ($ORIGIN_HEAD)."
  err "Ejecuta 'git pull origin main' antes de desplegar."
  err "STATUS: DEPLOY_ABORTED_SOURCE_SHA_MISMATCH"
  exit 1
fi

SHORT_SHA="${LOCAL_HEAD:0:7}"
ok "HEAD verificado: $LOCAL_HEAD (${SHORT_SHA})"

# ---------------------------------------------------------------------------
# 4. Confirmar antes de continuar
# ---------------------------------------------------------------------------
echo ""
echo "=================================================="
echo "  DEPLOY BACKEND — BGreda"
echo "=================================================="
echo "  Proyecto:  $PROJECT"
echo "  Región:    $REGION"
echo "  Servicio:  $SERVICE"
echo "  SHA:       $LOCAL_HEAD"
echo "  Short SHA: $SHORT_SHA"
echo "=================================================="
echo ""
read -r -p "¿Continuar con el deploy? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  warn "Deploy cancelado por el usuario."
  exit 0
fi

# ---------------------------------------------------------------------------
# 5. Ejecutar Cloud Build
# ---------------------------------------------------------------------------
log "Iniciando Cloud Build..."
BUILD_ID=$(gcloud builds submit \
  --config="$CLOUDBUILD_CONFIG" \
  --project="$PROJECT" \
  --substitutions="COMMIT_SHA=$LOCAL_HEAD,SHORT_SHA=$SHORT_SHA,REPO_NAME=BGreda" \
  --format="value(id)" \
  .)

ok "Cloud Build completado. BUILD_ID: $BUILD_ID"

# ---------------------------------------------------------------------------
# 6. Verificar estado final del servicio
# ---------------------------------------------------------------------------
log "Verificando estado del servicio..."
sleep 5
LATEST_READY=$(gcloud run services describe "$SERVICE" \
  --region="$REGION" \
  --project="$PROJECT" \
  --format="value(status.latestReadyRevisionName)")

# `status.traffic` lista TAMBIEN las revisiones antiguas que conservan un tag,
# y esas entran primero. Leer `traffic[0]` daba el nombre de una revision de
# hace meses: en el deploy de 8b8787e imprimio bgreda-api-00005-rix cuando el
# 100% estaba en 00048-zox. El deploy era correcto y el informe decia que no,
# que es la peor forma de equivocarse: invita a revertir algo que esta bien.
#
# Se toman las entradas con percent > 0 y se comprueba que suman 100. Si el
# reparto quedara dividido, hay que verlo, no elegir la primera y callar.
TRAFFIC_JSON=$(gcloud run services describe "$SERVICE" \
  --region="$REGION" \
  --project="$PROJECT" \
  --format="json(status.traffic)")

TRAFFIC_REV=$(echo "$TRAFFIC_JSON" | python -c "
import json, sys
reparto = json.load(sys.stdin).get('status', {}).get('traffic', [])
sirviendo = [t for t in reparto if t.get('percent', 0) > 0]
total = sum(t['percent'] for t in sirviendo)
print(' + '.join(f\"{t['revisionName']}={t['percent']}%\" for t in sirviendo) or 'NINGUNA')
print(total)
")
TRAFFIC_TOTAL=$(echo "$TRAFFIC_REV" | tail -1)
TRAFFIC_REV=$(echo "$TRAFFIC_REV" | head -1)

echo ""
echo "=================================================="
echo "  DEPLOY BACKEND: COMPLETO"
echo "=================================================="
echo "  BUILD_ID:         $BUILD_ID"
echo "  GIT_SHA:          $LOCAL_HEAD"
echo "  LATEST_READY:     $LATEST_READY"
echo "  SIRVIENDO:        $TRAFFIC_REV"
echo "=================================================="

if [ "$TRAFFIC_TOTAL" != "100" ]; then
  err "El trafico no suma 100% (suma $TRAFFIC_TOTAL)."
  err "STATUS: DEPLOY_TRAFFIC_NOT_FULLY_MIGRATED"
  exit 1
fi

ok "STATUS: BACKEND_DEPLOY_COMPLETE"
