#!/usr/bin/env bash
# Submit a controlled 010P backend candidate. This script never shifts traffic.
set -euo pipefail

PROJECT="cotizador-greda"
REGION="southamerica-west1"
SERVICE="bgreda-api"
IMAGE="southamerica-west1-docker.pkg.dev/cotizador-greda/cloud-run-source-deploy/bgreda-api"
CONFIG="cloudbuild.yaml"
EXPECTED="${EXPECTED_010P_MERGE_SHA:-}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]] || die 'Set EXPECTED_010P_MERGE_SHA to the full backend squash-merge SHA.'
[[ "${CI_RELEASE_GATES_VERIFIED:-}" == YES ]] || die 'Set CI_RELEASE_GATES_VERIFIED=YES only after manually confirming all required backend checks below.'
command -v gcloud >/dev/null || die 'gcloud is required.'
command -v jq >/dev/null || die 'jq is required.'
[[ -f "$CONFIG" ]] || die "Run from the repository root; $CONFIG is missing."

printf '%s\n' 'Required backend checks: CI / Lint, tipos y tests; CI / Imagen de contenedor; CI / Validar configuración Cloud Build.'
[[ -z "$(git status --porcelain)" ]] || { git status --short; die 'Working tree must be clean, including untracked files.'; }
[[ "$(git branch --show-current)" == main ]] || die 'Candidate release builds must run from main after squash merge.'
git fetch origin main --quiet
LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse origin/main)"
[[ "$LOCAL_SHA" == "$EXPECTED" && "$REMOTE_SHA" == "$EXPECTED" ]] || die "HEAD, origin/main, and EXPECTED_010P_MERGE_SHA must match exactly (head=$LOCAL_SHA origin=$REMOTE_SHA)."

TAG="p010p-$EXPECTED"
printf '\nBackend 010P candidate\nProject: %s\nRegion: %s\nService: %s\nMerge SHA: %s\nImage tag: %s\n' "$PROJECT" "$REGION" "$SERVICE" "$EXPECTED" "$TAG"
read -r -p 'Build and deploy this candidate with zero traffic? [y/N] ' CONFIRM
[[ "$CONFIRM" == y || "$CONFIRM" == Y ]] || { echo 'Cancelled.'; exit 0; }

BUILD_ID="$(gcloud builds submit \
  --config="$CONFIG" \
  --project="$PROJECT" \
  --substitutions="_RELEASE_SHA=$EXPECTED,_RELEASE_TAG=$TAG" \
  --format='value(id)' \
  .)"
[[ -n "$BUILD_ID" ]] || die 'Cloud Build did not return a build id.'

DIGEST="$(gcloud artifacts docker images describe "$IMAGE:$TAG" --project="$PROJECT" --format='value(image_summary.digest)')"
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || die 'Artifact Registry did not return a valid immutable digest.'
SERVICE_JSON="$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" --format=json)"
REVISION="$(jq -r --arg tag "$TAG" '.status.traffic[]? | select(.tag == $tag) | .revisionName' <<< "$SERVICE_JSON" | head -n 1)"
TAG_URL="$(jq -r --arg tag "$TAG" '.status.traffic[]? | select(.tag == $tag) | .url' <<< "$SERVICE_JSON" | head -n 1)"
[[ -n "$REVISION" && "$REVISION" != null && -n "$TAG_URL" && "$TAG_URL" != null ]] || die 'Candidate revision or tag URL is missing.'
REVISION_JSON="$(gcloud run revisions describe "$REVISION" --region="$REGION" --project="$PROJECT" --format=json)"
[[ "$(jq -r '[.status.conditions[]? | select(.type == "Ready") | .status][0] // ""' <<< "$REVISION_JSON")" == True ]] || die 'Candidate revision is not Ready.'
[[ "$(jq -r '.spec.containers[0].image' <<< "$REVISION_JSON")" == "$IMAGE@$DIGEST" ]] || die 'Candidate revision does not use the resolved digest.'
jq -e --arg rev "$REVISION" '([.status.traffic[]? | select(.revisionName == $rev) | (.percent // 0)] | add // 0) == 0' <<< "$SERVICE_JSON" >/dev/null || die 'Candidate revision has nonzero traffic.'
jq -e '([.status.traffic[]? | select(.revisionName != null) | (.percent // 0)] | add // 0) == 100' <<< "$SERVICE_JSON" >/dev/null || die 'Service traffic allocation does not total 100%.'

printf '\nBACKEND_CANDIDATE=READY\nBUILD_ID=%s\nIMAGE_TAG=%s\nIMAGE_DIGEST=%s\nREVISION=%s\nTAG_URL=%s\nCANDIDATE_TRAFFIC=0\nSERVICE_TRAFFIC_TOTAL=100\n' "$BUILD_ID" "$TAG" "$DIGEST" "$REVISION" "$TAG_URL"
