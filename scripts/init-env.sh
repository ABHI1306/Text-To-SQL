#!/usr/bin/env sh
# Creates .env from .env.example with generated secrets. Safe to re-run: it never overwrites.
#   make init-env
set -eu
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  echo ".env already exists; leaving it alone."
  exit 0
fi
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

cp .env.example .env
chmod 600 .env

# set KEY VALUE: replace the value of an existing KEY= line (value contains only [A-Za-z0-9_-=])
set_var() { sed -i "s|^$1=.*|$1=$2|" .env; }
random() { openssl rand -hex "$1"; }

for key in METADATA_ADMIN_PASSWORD METADATA_APP_PASSWORD DEMO_ADMIN_PASSWORD WAREHOUSE_READER_PASSWORD; do
  set_var "$key" "$(random 16)"
done
set_var JWT_SECRET "$(random 32)"
# a Fernet key is 32 random bytes, url-safe base64 encoded
set_var CONNECTION_ENCRYPTION_KEY "$(openssl rand -base64 32 | tr '+/' '-_')"
DBA_PASSWORD="$(random 8)Aa1"
set_var BOOTSTRAP_DBA_PASSWORD "$DBA_PASSWORD"

echo "Created .env with generated secrets."
echo
echo "First sign-in (you must change the password at once):"
echo "  username: $(sed -n 's/^BOOTSTRAP_DBA_USERNAME=//p' .env)"
echo "  password: $DBA_PASSWORD"
echo
echo "Next: make up && make models && make bootstrap"
