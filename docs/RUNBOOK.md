# Runbook

Commands run from the repository root. `make` targets use `.env`; add `GPU=1` or `IDENTITY=1`
to any of them if you started the stack that way.

## Is it healthy?

- `curl localhost:8000/health/ready` names each dependency (metadata database, model server,
  vector store) as `up` or `down` and answers 503 when one is down.
- In the UI, **Operations** (needs `view_audit`) shows tokens and model time per question and
  whether the scheduler is keeping up. "Behind" almost always means the worker is not running:
  `docker compose --env-file .env ps worker`, then `make logs`.
- Counters on **Operations** are per API process and reset when it restarts.

## Deploy

- **First time:** `make init-env`, `make up`, `make models`, `make bootstrap` (see the README).
- **Update the code:** `git pull`, then `make up` (rebuilds, restarts, applies migrations).
  Migrations are forward-only and run from `migrations/versions`; take a database backup first.
- **A new semantic model:** in the UI, **Connect and model** > **Deploy report** > **Deploy
  model**. It indexes the revision, then switches to it atomically; a failure leaves the
  previous model live. A revision that is already live cannot be deployed again (409).

## Re-index

The vector index is rebuilt from the deployed model, for example after changing
`EMBEDDING_MODEL` to a model with the same vector size:

```bash
make models                                  # pull the new embedding model first
make reindex PROJECT_ID=<project uuid>
```

If this fails part-way the project's index is deleted rather than left half-written, so
questions fail until you run it again. It checks the embedding model first to make that rare.
A model with a *different* vector size needs the Qdrant collection recreated: stop the stack,
remove the `qdrant-data` volume, `make up`, then re-index every project.

## Rotate keys

**`JWT_SECRET`** signs access tokens. Put a new value in `.env`
(`openssl rand -hex 32`) and `make up`. Everyone is signed out and signs in again; nothing is lost.

**`CONNECTION_ENCRYPTION_KEY`** encrypts the saved database passwords. Changing it without
re-encrypting makes every saved connection unreadable, so:

```bash
OLD=$(sed -n 's/^CONNECTION_ENCRYPTION_KEY=//p' .env)          # remember the current key
NEW=$(openssl rand -base64 32 | tr '+/' '-_')                  # a new Fernet key
sed -i "s|^CONNECTION_ENCRYPTION_KEY=.*|CONNECTION_ENCRYPTION_KEY=$NEW|" .env
make rotate-key OLD="$OLD"
```

It re-encrypts every project in one transaction, then restarts the api and worker. Keep a
copy of the old key until you have asked one question on each project.

- **Nothing is changed** if any connection cannot be read with `OLD`: the error names the
  first few projects. That is almost always a wrong `OLD`; if it is a project that is already
  unusable (for example one left behind by tests), repeat with `make rotate-key OLD="$OLD"
  SKIP_UNREADABLE=1`. Those projects are left exactly as they are and listed.
- If *no* connection is readable the run fails even with `SKIP_UNREADABLE=1`, so a wrong key
  can never look like a successful rotation.
- Rehearse on a copy first if the data matters: `pg_dump` the metadata database into a scratch
  database and run the same command with `METADATA_DATABASE_URL` pointed at it.

**Database passwords** (`METADATA_*`, `DEMO_ADMIN_PASSWORD`, `WAREHOUSE_READER_PASSWORD`) are
set when a database volume is first created. Change them with `ALTER ROLE` inside the database
and then in `.env`; editing `.env` alone breaks the connection.

## Add roles and accounts

- **Users and roles** screen: create accounts, set a role, project access and attributes
  (for example `region`) that row policies read.
- Built-in roles: **DBA** (everything), **Admin** (users and governance), **User** (ask and
  open what is shared). Create a custom role from the capability list; a role can only grant
  capabilities the person editing it holds (a DBA can grant any).
- From the terminal: `make user-create USERNAME=maria ROLE=User`, then the person changes
  the temporary password at first sign-in.
- Row-level rules live under **Row policies**; a user whose attribute is missing sees no rows.

## Reset a DBA (or any local account)

```bash
make user-reset-dba USERNAME=demo.dba      # prompts for a new temporary password
```

If no DBA can sign in at all and none exists, set `BOOTSTRAP_DBA_USERNAME` and
`BOOTSTRAP_DBA_PASSWORD` in `.env` and run `make bootstrap` (it refuses an existing username;
use the reset command for those).

## Configure LDAP

1. In `.env` set `LDAP_URI`, `LDAP_START_TLS` (or an `ldaps://` URI), `LDAP_CA_CERT_FILE`,
   `LDAP_SERVICE_DN`, `LDAP_SERVICE_PASSWORD`, `LDAP_USER_BASE_DN` and `LDAP_GROUP_BASE_DN`.
   Plain LDAP is refused unless `LDAP_ALLOW_INSECURE=true` (tests only).
   `LDAP_ATTRIBUTE_MAP=l=region,departmentNumber=department` copies directory attributes into
   the attributes row policies use.
2. Map directory groups to roles: `PUT /api/v1/identity/mappings` (the UI has an **Identity
   mapping** panel under Users and roles). **A person whose groups map to no role cannot sign
   in**, by design.
3. Restart the api: `make up`.

Demo directory: `sh scripts/ldap/make-certs.sh`, set `LDAP_ADMIN_PASSWORD` and
`LDAP_SERVICE_PASSWORD` in `.env`, then `make up IDENTITY=1`. Demo users `asha`, `ravi`,
`mina` are in `scripts/ldap/bootstrap.ldif`. If a local account with the same username exists
it wins, so pick a name that is not already local.

## Configure OIDC (single sign-on)

1. In `.env` set `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URI`
   (`https://<host>/api/v1/auth/oidc/callback`), and optionally `OIDC_GROUPS_CLAIM` (default
   `groups`; a dotted path reads nested claims) and `OIDC_ATTRIBUTE_MAP`. Issuer and redirect
   must be https unless `OIDC_ALLOW_INSECURE=true` (local demo only). If the api reaches the
   provider at a different address than browsers do, set `OIDC_DISCOVERY_URL`.
2. Map provider groups to roles with `PUT /api/v1/identity/mappings` using `"source": "oidc"`.
3. Restart the api. The sign-in page then offers "Sign in with single sign-on".

An account is pinned to the provider's `sub` at first login; a different `sub` for the same
username is refused. Demo: set `OIDC_CLIENT_SECRET` and `KEYCLOAK_ADMIN_PASSWORD` in `.env`,
then `make up IDENTITY=1` (Keycloak on <http://localhost:8081>, users in
`scripts/oidc/realm-text2sql.json`).

## Schedule jobs

- **Saved questions:** ask, then **Schedule this question** (or the **Scheduled questions**
  screen). Presets or a five-field cron expression with a time zone; no more often than every
  15 minutes. A run executes as the person who scheduled it, with their data access, and a
  failed run sends them a notice.
- **Profiling and drift:** `PUT /api/v1/projects/{id}/profiling-schedule` (needs
  `manage_profiling`); reports at `GET .../drift-reports`.
- Jobs are run by the `worker` container. If the worker is down, slots are caught up once when
  it returns (one run, not one per missed slot). Pause, resume and run history are on the
  Scheduled questions screen; a job whose owner lost the capability is paused, not run.

## Add a database

The API resolves hosts from inside its container. See *Connecting your own PostgreSQL
database* in the README, including `DB_HOST_ALLOWLIST`. Always use a read-only account.

## Run with no outside network

`make offline` brings the stack up with LDAP and Keycloak and proves nothing can reach the
internet. It needs `OIDC_CLIENT_SECRET` and `KEYCLOAK_ADMIN_PASSWORD` set.
