#!/usr/bin/env bash
# Fail-closed, read-only source backup for the 010P migration window.
# It does not change database state. Never run this while preparing the RC.
set -euo pipefail
umask 077

die() { printf 'BACKUP_STATUS=FAIL\nERROR=%s\n' "$*" >&2; exit 1; }

: "${DATABASE_URL:?DATABASE_URL secret is required}"
: "${CLOUD_RUN_EXECUTION:?This script must run as a Cloud Run Job execution}"
: "${BACKUP_BUCKET:?Set BACKUP_BUCKET to the approved backup bucket}"
: "${RELEASE_BACKEND_SHA:?Set the merged 010P backend release SHA}"
: "${RELEASE_FRONTEND_SHA:?Set the merged 010P frontend release SHA}"
: "${BACKEND_PROD_REVISION:?Capture the serving backend revision before the lock}"
: "${FRONTEND_PROD_REVISION:?Capture the serving frontend revision before the lock}"
[[ "$RELEASE_BACKEND_SHA" =~ ^[0-9a-f]{40}$ ]] || die 'RELEASE_BACKEND_SHA is not a full lowercase commit SHA'
[[ "$RELEASE_FRONTEND_SHA" =~ ^[0-9a-f]{40}$ ]] || die 'RELEASE_FRONTEND_SHA is not a full lowercase commit SHA'
[[ "$BACKUP_BUCKET" == cotizador-greda-db-backups ]] || die 'Unexpected backup bucket; confirm the approved bucket before running'

START_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SOURCE_ALEMBIC_REVISION="$(psql "${DATABASE_URL/postgresql+asyncpg:/postgresql:}" -X -A -t -v ON_ERROR_STOP=1 -c 'SELECT version_num FROM alembic_version')"
[[ "$SOURCE_ALEMBIC_REVISION" == 0041 ]] || die "Expected source Alembic 0041; found '$SOURCE_ALEMBIC_REVISION'"
export SOURCE_ALEMBIC_REVISION

OBJECT="bgreda-prod-before-010p-${TIMESTAMP}-${CLOUD_RUN_EXECUTION}.dump"
MANIFEST_OBJECT="${OBJECT%.dump}.manifest.json"
DUMP_FILE="/tmp/${OBJECT}"
TOC_FILE="/tmp/${OBJECT}.toc"
MANIFEST_FILE="/tmp/${OBJECT}.manifest.json"
trap 'rm -f "$DUMP_FILE" "$TOC_FILE" "$MANIFEST_FILE" /tmp/backup-upload-response.json /tmp/manifest-upload-response.json' EXIT

printf 'BACKUP_START_UTC=%s\nSOURCE_ALEMBIC_REVISION=%s\nJOB_EXECUTION_ID=%s\nBUCKET=%s\nOBJECT=%s\n' \
  "$START_UTC" "$SOURCE_ALEMBIC_REVISION" "$CLOUD_RUN_EXECUTION" "$BACKUP_BUCKET" "$OBJECT"

pg_dump --format=custom --no-owner --no-privileges --dbname="${DATABASE_URL/postgresql+asyncpg:/postgresql:}" --file="$DUMP_FILE"
pg_restore --list "$DUMP_FILE" > "$TOC_FILE" || die 'pg_restore --list could not read the custom-format dump'
grep -Eq 'TABLE public alembic_version([[:space:]]|$)' "$TOC_FILE" || die 'Dump TOC is missing public.alembic_version'
grep -Eq 'TABLE public quotations([[:space:]]|$)' "$TOC_FILE" || die 'Dump TOC is missing public.quotations'
PG_RESTORE_LIST_STATUS=PASS
TOC_COUNT="$(grep -Ec '^;[[:space:]]+[0-9]+[[:space:]]+[0-9]+[[:space:]]' "$TOC_FILE")"
OBJECT_SIZE="$(stat -c '%s' "$DUMP_FILE")"
SHA256="$(sha256sum "$DUMP_FILE" | cut -d ' ' -f 1)"
[[ "$TOC_COUNT" =~ ^[0-9]+$ && "$TOC_COUNT" -gt 0 ]] || die 'Dump TOC is empty'
[[ "$OBJECT_SIZE" =~ ^[0-9]+$ && "$OBJECT_SIZE" -gt 0 ]] || die 'Dump file is empty'
[[ "$SHA256" =~ ^[0-9a-f]{64}$ ]] || die 'Could not compute a valid SHA256'
unset DATABASE_URL

TOKEN_JSON="$(curl -fsS -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token')" \
  || die 'Could not obtain the Cloud Run job identity token'
ACCESS_TOKEN="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' <<< "$TOKEN_JSON")"
unset TOKEN_JSON

upload_object() {
  local file="$1" object="$2" content_type="$3" response_file="$4" encoded_object http_status expected_md5
  encoded_object="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$object")"
  expected_md5="$(env -u DATABASE_URL python3 - "$file" <<'PY'
import base64
import hashlib
import sys

digest = hashlib.md5()
with open(sys.argv[1], "rb") as content:
    for block in iter(lambda: content.read(1024 * 1024), b""):
        digest.update(block)
print(base64.b64encode(digest.digest()).decode("ascii"))
PY
)"
  http_status="$(printf 'header = "Authorization: Bearer %s"\n' "$ACCESS_TOKEN" | \
    curl --config - -sS -o "$response_file" -w '%{http_code}' -X POST \
      -H "Content-Type: $content_type" -H "Content-MD5: $expected_md5" --data-binary "@$file" \
      "https://storage.googleapis.com/upload/storage/v1/b/${BACKUP_BUCKET}/o?uploadType=media&name=${encoded_object}")" \
    || die "Upload request failed for object $object"
  [[ "$http_status" == 200 || "$http_status" == 201 ]] || die "Upload returned HTTP $http_status for object $object"
  env -u DATABASE_URL python3 - "$response_file" "$object" "$(stat -c '%s' "$file")" "$expected_md5" <<'PY'
import json
import sys

path, expected_name, expected_size, expected_md5 = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
with open(path, encoding="utf-8") as response:
    payload = json.load(response)
if payload.get("name") != expected_name or int(payload.get("size", -1)) != expected_size:
    raise SystemExit("Cloud Storage upload response name/size did not match the uploaded object")
if payload.get("md5Hash") != expected_md5:
    raise SystemExit("Cloud Storage upload response checksum did not match the uploaded file")
PY
}

upload_object "$DUMP_FILE" "$OBJECT" 'application/octet-stream' /tmp/backup-upload-response.json
END_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export BACKUP_START_UTC="$START_UTC" BACKUP_END_UTC="$END_UTC"
export BACKUP_OBJECT="$OBJECT" BACKUP_OBJECT_SIZE="$OBJECT_SIZE" BACKUP_SHA256="$SHA256"
export BACKUP_TOC_COUNT="$TOC_COUNT" BACKUP_PG_RESTORE_LIST_STATUS="$PG_RESTORE_LIST_STATUS"
env -u DATABASE_URL python3 - "$MANIFEST_FILE" <<'PY'
import json
import os
import sys

manifest = {
    "release": "010P",
    "release_backend_sha": os.environ["RELEASE_BACKEND_SHA"],
    "release_frontend_sha": os.environ["RELEASE_FRONTEND_SHA"],
    "source_revision": os.environ["SOURCE_ALEMBIC_REVISION"],
    "target_revision": "0045",
    "timestamp_utc": os.environ["BACKUP_START_UTC"],
    "backup_end_utc": os.environ["BACKUP_END_UTC"],
    "bucket": os.environ["BACKUP_BUCKET"],
    "object": os.environ["BACKUP_OBJECT"],
    "size": int(os.environ["BACKUP_OBJECT_SIZE"]),
    "sha256": os.environ["BACKUP_SHA256"],
    "toc_entries": int(os.environ["BACKUP_TOC_COUNT"]),
    "pg_restore_list": os.environ["BACKUP_PG_RESTORE_LIST_STATUS"],
    "job_execution": os.environ["CLOUD_RUN_EXECUTION"],
    "backend_prod_revision": os.environ["BACKEND_PROD_REVISION"],
    "frontend_prod_revision": os.environ["FRONTEND_PROD_REVISION"],
}
with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump(manifest, output, indent=2, sort_keys=True)
    output.write("\n")
PY
upload_object "$MANIFEST_FILE" "$MANIFEST_OBJECT" 'application/json' /tmp/manifest-upload-response.json

unset ACCESS_TOKEN
printf 'OBJECT_SIZE=%s\nSHA256=%s\nPG_RESTORE_LIST_STATUS=%s\nTOC_COUNT=%s\nBACKUP_END_UTC=%s\nMANIFEST_OBJECT=gs://%s/%s\nBACKUP_STATUS=PASS\n' \
  "$OBJECT_SIZE" "$SHA256" "$PG_RESTORE_LIST_STATUS" "$TOC_COUNT" "$END_UTC" "$BACKUP_BUCKET" "$MANIFEST_OBJECT"
