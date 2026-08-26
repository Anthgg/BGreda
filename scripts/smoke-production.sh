#!/usr/bin/env bash
# smoke-production.sh — Smoke test del backend en producción.
#
# Valida endpoints de solo lectura y endpoints de salud.
# NO ejecuta operaciones de escritura ni mutaciones.
#
# Uso:
#   ./scripts/smoke-production.sh [BASE_URL]
#
# Ejemplo:
#   ./scripts/smoke-production.sh https://bgreda-api-303244958634.southamerica-west1.run.app

set -euo pipefail

BASE_URL="${1:-https://bgreda-api-303244958634.southamerica-west1.run.app}"

# ---------------------------------------------------------------------------
# Colores
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*" >&2; FAILURES=$((FAILURES + 1)); }
info() { echo -e "${BLUE}[INFO]${NC} $*"; }

FAILURES=0

check_status() {
  local desc="$1"
  local url="$2"
  local expected="$3"
  local actual
  actual=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url")
  if [ "$actual" = "$expected" ]; then
    ok "$desc → HTTP $actual"
  else
    fail "$desc → esperado HTTP $expected, obtenido HTTP $actual  ($url)"
  fi
}

check_status_range() {
  local desc="$1"
  local url="$2"
  local lo="$3"
  local hi="$4"
  local actual
  actual=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url")
  if [ "$actual" -ge "$lo" ] && [ "$actual" -le "$hi" ]; then
    ok "$desc → HTTP $actual (en rango $lo-$hi)"
  else
    fail "$desc → HTTP $actual fuera del rango esperado $lo-$hi  ($url)"
  fi
}

echo ""
echo "=================================================="
echo "  SMOKE TEST — BGreda Backend"
echo "=================================================="
echo "  BASE_URL: $BASE_URL"
echo "=================================================="
echo ""

# Health checks
info "=== Health ==="
check_status "/live"  "$BASE_URL/live"  "200"
check_status "/ready" "$BASE_URL/ready" "200"

# Auth (CSRF no requiere sesion, /me requiere sesion)
info "=== Auth ==="
check_status "/api/v1/auth/csrf" "$BASE_URL/api/v1/auth/csrf" "200"
check_status_range "/api/v1/auth/me (sin sesion)" "$BASE_URL/api/v1/auth/me" "401" "403"

# Endpoints de datos (requieren sesion, esperamos 401 sin cookies)
info "=== Recursos (sin sesion → 401 esperado) ==="
check_status_range "/api/v1/clients"   "$BASE_URL/api/v1/clients"   "401" "403"
check_status_range "/api/v1/products"  "$BASE_URL/api/v1/products"  "401" "403"
check_status_range "/api/v1/recipes"   "$BASE_URL/api/v1/recipes"   "401" "403"
check_status_range "/api/v1/kilns"     "$BASE_URL/api/v1/kilns"     "401" "403"
check_status_range "/api/v1/quotations" "$BASE_URL/api/v1/quotations" "401" "403"

# No debe haber 500s ni 502s en los endpoints de salud
info "=== Verificar ausencia de 5xx en health ==="
for path in /live /ready; do
  actual=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE_URL$path")
  if [ "$actual" -ge "500" ]; then
    fail "$path devolvió HTTP $actual (5xx)"
  fi
done

echo ""
echo "=================================================="
if [ "$FAILURES" -eq 0 ]; then
  echo -e "${GREEN}  SMOKE: PASS ($FAILURES fallos)${NC}"
  echo "=================================================="
  exit 0
else
  echo -e "${RED}  SMOKE: FAIL ($FAILURES fallos)${NC}"
  echo "=================================================="
  exit 1
fi
