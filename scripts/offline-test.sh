#!/usr/bin/env bash
# D9-3 offline test: the whole stack, including the worker, OpenLDAP and Keycloak, with outbound
# traffic blocked. Needs .env (see .env.example), certificates from scripts/ldap/make-certs.sh,
# and the models already pulled into the ollama volume.
#
#   make offline            # brings the stack up, runs every check, leaves it running
#   KEEP=0 make offline     # also takes it down afterwards
set -u
cd "$(dirname "$0")/.."

COMPOSE=(docker compose --env-file .env -f docker-compose.yml -f docker-compose.ldap.yml
         -f docker-compose.oidc.yml -f docker-compose.offline.yml --profile identity-demo)
SERVICES=(api worker metadata-db demo-db qdrant ollama openldap keycloak)
PUBLIC_IP=1.1.1.1
fail=0

pass() { printf 'PASS  %s\n' "$1"; }
bad()  { printf 'FAIL  %s\n' "$1"; fail=1; }

# OIDC_CLIENT_SECRET and KEYCLOAK_ADMIN_PASSWORD must be set (in .env or the environment).
# set -a so .env values are visible to this script and to compose
set -a; . ./.env; set +a

echo "== starting the stack"
"${COMPOSE[@]}" up -d --wait --wait-timeout 300 || { bad "stack did not become healthy"; exit 1; }

echo "== outbound traffic is blocked from every service"
for service in "${SERVICES[@]}"; do
  # bash's /dev/tcp needs no extra tools; python covers the api image if bash is missing
  out=$("${COMPOSE[@]}" exec -T "$service" sh -c "
    if command -v timeout >/dev/null && command -v bash >/dev/null; then
      timeout 3 bash -c 'echo > /dev/tcp/$PUBLIC_IP/443' >/dev/null 2>&1 && echo OPEN || echo CLOSED
    elif command -v python3 >/dev/null; then
      python3 -c \"import socket,sys; s=socket.socket(); s.settimeout(3)
try: s.connect(('$PUBLIC_IP',443)); print('OPEN')
except OSError: print('CLOSED')\"
    else echo NOPROBE; fi" 2>/dev/null | tail -n 1)
  case "$out" in
    CLOSED) pass "$service cannot reach $PUBLIC_IP" ;;
    OPEN)   bad  "$service CAN reach $PUBLIC_IP" ;;
    *)      bad  "$service: no way to probe (got '$out')" ;;
  esac
done

echo "== DNS for a public name does not resolve from the api"
if "${COMPOSE[@]}" exec -T api python3 -c "import socket; socket.gethostbyname('example.com')" >/dev/null 2>&1; then
  bad "api resolved example.com"
else
  pass "api cannot resolve example.com"
fi

echo "== the stack works with no outside network"
ready=$("${COMPOSE[@]}" exec -T api python3 -c "
import json,urllib.request,urllib.error
try: r=urllib.request.urlopen('http://127.0.0.1:8000/health/ready'); body=r.read()
except urllib.error.HTTPError as e: body=e.read()
print(body.decode())" 2>/dev/null | tail -n 1)
case "$ready" in *'"status":"ok"'*) pass "/health/ready: $ready" ;; *) bad "/health/ready: $ready" ;; esac

api() { "${COMPOSE[@]}" exec -T api python3 - "$@"; }

login_status() {  # username password -> HTTP status of POST /auth/login
  api "$1" "$2" <<'PY'
import json,sys,urllib.request,urllib.error
req=urllib.request.Request('http://127.0.0.1:8000/api/v1/auth/login',
    data=json.dumps({'username':sys.argv[1],'password':sys.argv[2]}).encode(),
    headers={'content-type':'application/json'})
try: print(urllib.request.urlopen(req).status)
except urllib.error.HTTPError as e: print(e.code)
PY
}

if [ -n "${OFFLINE_LOCAL_USER:-}" ] && [ -n "${OFFLINE_LOCAL_PASSWORD:-}" ]; then
  [ "$(login_status "$OFFLINE_LOCAL_USER" "$OFFLINE_LOCAL_PASSWORD")" = 200 ] \
    && pass "local login ($OFFLINE_LOCAL_USER)" || bad "local login ($OFFLINE_LOCAL_USER)"
else
  echo "SKIP  local login: set OFFLINE_LOCAL_USER and OFFLINE_LOCAL_PASSWORD to check it"
fi
# OpenLDAP itself: StartTLS plus a bind as the service account and as a demo user. A full app
# login also needs a group-to-role mapping (PUT /identity/mappings), which is deployment data.
ldap=$("${COMPOSE[@]}" exec -T api uv run --no-sync python - <<'PY' 2>&1 | tail -n 1
import os, ssl
from ldap3 import Connection, Server, Tls
tls = Tls(ca_certs_file=os.environ["LDAP_CA_CERT_FILE"], validate=ssl.CERT_REQUIRED)
server = Server(os.environ["LDAP_URI"], tls=tls)
def bind(dn, password):
    conn = Connection(server, user=dn, password=password)
    return conn.open() is None and conn.start_tls() and conn.bind()
ok = bind(os.environ["LDAP_SERVICE_DN"], os.environ["LDAP_SERVICE_PASSWORD"]) \
    and bind("uid=asha," + os.environ["LDAP_USER_BASE_DN"], "demo-asha-password")
print("OK" if ok else "BIND_FAILED")
PY
)
ldap=${ldap//$'\r'/}
[[ "$ldap" == *OK ]] && pass "LDAP: StartTLS and binds (service account, asha) work offline" \
  || bad "LDAP bind check: $ldap"
if [ -n "${OFFLINE_LDAP_USER:-}" ] && [ -n "${OFFLINE_LDAP_PASSWORD:-}" ]; then
  [ "$(login_status "$OFFLINE_LDAP_USER" "$OFFLINE_LDAP_PASSWORD")" = 200 ] \
    && pass "LDAP app login ($OFFLINE_LDAP_USER)" || bad "LDAP app login ($OFFLINE_LDAP_USER)"
else
  echo "SKIP  LDAP app login: needs an identity mapping; set OFFLINE_LDAP_USER and OFFLINE_LDAP_PASSWORD"
fi

# Keycloak's dev start takes a while and has no healthcheck, so wait for it.
oidc=ERR
for _ in $(seq 1 30); do
  oidc=$(api <<'PY'
import urllib.request
try:
    urllib.request.urlopen('http://keycloak:8080/realms/text2sql/.well-known/openid-configuration', timeout=5)
    print('OK')
except Exception as e: print('ERR', e)
PY
)
  [[ "$oidc" == *OK ]] && break
  sleep 5
done
[[ "$oidc" == *OK ]] && pass "OIDC: api reads the Keycloak discovery document" || bad "OIDC discovery: $oidc"

state=$("${COMPOSE[@]}" ps --format '{{.Service}} {{.State}}' worker | tail -n 1)
[ "$state" = "worker running" ] && pass "worker is running" || bad "worker state: $state"
"${COMPOSE[@]}" logs --no-log-prefix worker 2>&1 | grep -q "Worker started" \
  && pass "worker started and reads jobs from the database" || bad "worker never reported starting"

echo
if [ "$fail" = 0 ]; then echo "OFFLINE TEST PASSED"; else echo "OFFLINE TEST FAILED"; fi
[ "${KEEP:-1}" = 0 ] && "${COMPOSE[@]}" down
exit "$fail"
