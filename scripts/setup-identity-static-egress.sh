#!/usr/bin/env bash
# Runbook ejecutable: IP de salida estatica para la consulta de identidad.
#
# NO SE EJECUTA COMO PARTE DE NINGUN DESPLIEGUE AUTOMATICO. Ver
# docs/runbooks/identity-static-egress.md para el contexto completo antes de
# correr esto contra un proyecto real.
#
# Requiere aprobacion explicita del usuario y las variables de entorno de
# abajo. Sin --yes-i-understand, el script solo imprime lo que haria.
set -euo pipefail

PROJECT="${PROJECT:?Definir PROJECT (ej. cotizador-greda)}"
REGION="${REGION:?Definir REGION (ej. southamerica-west1)}"
VPC_NETWORK="${VPC_NETWORK:?Definir VPC_NETWORK (la red donde vive el servicio)}"
SERVICE="${SERVICE:-bgreda-api}"

IP_NAME="bgreda-identity-nat-ip"
SUBNET_NAME="bgreda-egress-subnet"
SUBNET_RANGE="${SUBNET_RANGE:-10.10.10.0/28}"
ROUTER_NAME="bgreda-identity-router"
NAT_NAME="bgreda-identity-nat"

DRY_RUN=true
if [[ "${1:-}" == "--yes-i-understand" ]]; then
  DRY_RUN=false
fi

run() {
  if [[ "$DRY_RUN" == true ]]; then
    echo "[dry-run] $*"
  else
    echo "+ $*"
    "$@"
  fi
}

if [[ "$DRY_RUN" == true ]]; then
  echo "=== MODO SOLO LECTURA ==="
  echo "Nada de lo siguiente se ejecuta de verdad. Vuelva a llamar con"
  echo "--yes-i-understand solo despues de leer el runbook completo y tener"
  echo "la aprobacion explicita para tocar produccion."
  echo
fi

run gcloud compute addresses create "$IP_NAME" \
  --project="$PROJECT" --region="$REGION"

run gcloud compute networks subnets create "$SUBNET_NAME" \
  --project="$PROJECT" --region="$REGION" --network="$VPC_NETWORK" \
  --range="$SUBNET_RANGE"

run gcloud compute routers create "$ROUTER_NAME" \
  --project="$PROJECT" --region="$REGION" --network="$VPC_NETWORK"

run gcloud compute routers nats create "$NAT_NAME" \
  --project="$PROJECT" --region="$REGION" --router="$ROUTER_NAME" \
  --nat-external-ip-pool="$IP_NAME" \
  --nat-custom-subnet-ip-ranges="$SUBNET_NAME"

echo
echo "Infraestructura de red lista (o simulada, en modo dry-run)."
echo "El servicio de Cloud Run NO se actualiza automaticamente: revisar"
echo "docs/runbooks/identity-static-egress.md, paso 5, y desplegar primero"
echo "con --no-traffic antes de mover trafico real."

if [[ "$DRY_RUN" == true ]]; then
  run gcloud compute addresses describe "$IP_NAME" \
    --project="$PROJECT" --region="$REGION" --format="value(address)"
fi
