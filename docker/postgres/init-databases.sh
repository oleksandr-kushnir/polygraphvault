#!/bin/sh
set -eu

: "${NEXTCLOUD_DB_PASSWORD:?NEXTCLOUD_DB_PASSWORD is required}"
: "${SYNCER_DB_PASSWORD:?SYNCER_DB_PASSWORD is required}"

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=nc_password="$NEXTCLOUD_DB_PASSWORD" \
  --set=sync_password="$SYNCER_DB_PASSWORD" <<'EOSQL'
SELECT format('CREATE ROLE nextcloud LOGIN PASSWORD %L', :'nc_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nextcloud')
\gexec
SELECT format('CREATE ROLE ncragsync LOGIN PASSWORD %L', :'sync_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ncragsync')
\gexec
EOSQL

if ! psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align \
  --command "SELECT 1 FROM pg_database WHERE datname = 'nextcloud'" | grep -q '^1$'; then
  createdb --username "$POSTGRES_USER" --owner nextcloud nextcloud
fi

if ! psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --tuples-only --no-align \
  --command "SELECT 1 FROM pg_database WHERE datname = 'ncragsync'" | grep -q '^1$'; then
  createdb --username "$POSTGRES_USER" --owner ncragsync ncragsync
fi
