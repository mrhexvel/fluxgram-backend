# Plugin Reputation Publisher

The publisher runs separately from the public API and receives its Vault token from Vault Agent.
The production-oriented Compose fragment runs Vault Agent and the publisher worker. It assumes a
Vault service named `vault`, Transit mounted at `transit`, the key `plugin-reputation`, and publisher
UID/GID `65534`. It pins Vault Agent 1.21.0 by image digest and requires an immutable digest reference
for the backend image.

## Local Docker Deployment

`compose.local.yml` starts a complete development stack on Docker Desktop without a host Vault CLI:

- PostgreSQL 18 and database migrations;
- the regular public API and Telegram webhook bot when the `application` profile is enabled;
- a TLS-enabled Vault 1.21 server with persistent file storage;
- initialization, restart-safe unseal, Transit key, policy and AppRole provisioning;
- Vault Agent auto-auth with a response-wrapped, single-use SecretID;
- the publisher worker with only the CA, read-only token sink and public authority key ID.

From the repository root, start it with:

```sh
docker compose --env-file .env -f deploy/plugin-reputation/compose.local.yml \
  --profile application up --build -d
```

The regular API, Telegram bot, publisher worker and reputation API in this composition use the same
PostgreSQL service and the same migration run. Do not run the root `docker-compose.yml` PostgreSQL or
a host bot alongside this complete stack. Configure the Telegram variables in the repository-local
`.env` before enabling the profile. Compose passes only the explicit Telegram variables to the bot,
while its database address remains fixed to the Compose PostgreSQL service. The regular API binds to `127.0.0.1:8080`, the bot to
`127.0.0.1:8081`, and the reputation API to `127.0.0.1:18080` by default. Point the Telegram webhook
tunnel to port `8081` and the client reputation endpoint tunnel to port `18080`.

Register the Telegram webhook after its tunnel is available:

```sh
docker compose --env-file .env -f deploy/plugin-reputation/compose.local.yml \
  --profile application exec bot python -m fluxgram_backend webhook set
```

The first start creates local-only CA, Vault root token and unseal key inside private Docker volumes.
They are not written to the checkout or exposed to the worker. A local supervisor unseals Vault after
a container restart and keeps a short-lived wrapped SecretID ready for Vault Agent recovery. Vault
and PostgreSQL bind only to
`127.0.0.1` on ports `18200` and `55432`; `VAULT_PORT` and `POSTGRES_PORT` can override them. Host
Vault clients must explicitly trust the generated CA from the Docker volume. The normal verification
is the Compose service state and worker log; no host Vault access is required.

Stop the stack while preserving PostgreSQL, Vault and its local authority key:

```sh
docker compose -f deploy/plugin-reputation/compose.local.yml down
```

Reset all local credentials, records and keys:

```sh
docker compose -f deploy/plugin-reputation/compose.local.yml down -v
```

The local supervisor is intentionally not a production unseal or credential-delivery mechanism. It
keeps access to one unseal key and a root token in a local Docker volume so component restarts are
automatic. Production must use the external issuer, protected runtime directories and Agent-only
fragment described below.

After the worker is running, execute a real queue-to-Vault verification:

```sh
docker compose -f deploy/plugin-reputation/compose.local.yml \
  --profile verify run --rm --no-deps verify
```

The command creates one local clear decision, waits for the running worker, reads the persisted
record and verifies its canonical P-256 signature against the generated authority key ID. The
expected final line is `Local Vault publisher and public API verification passed`.

## Publisher Process

The publisher process is started separately from the API and Telegram bot:

```sh
python -m fluxgram_backend.publisher
```

It requires these environment variables:

```text
DATABASE_URL or DATABASE_URL_FILE
PLUGIN_REPUTATION_AUTHORITY_KEY_ID
PLUGIN_REPUTATION_WORKER_ID
VAULT_ADDRESS
VAULT_TRANSIT_MOUNT
VAULT_TRANSIT_KEY_NAME
VAULT_TRANSIT_KEY_VERSION
VAULT_AGENT_TOKEN_FILE
VAULT_CA_FILE
```

`PLUGIN_REPUTATION_AUTHORITY_KEY_ID` is the pinned `p256:<sha256-spki>` identity of the Vault
Transit authority key, not a plugin developer key. `VAULT_AGENT_TOKEN_FILE` points to the Agent token
sink and must not be replaced by a static `VAULT_TOKEN`. The process also accepts the bounded optional
worker interval and retry settings documented by `PluginReputationWorkerSettings`.
Production uses `DATABASE_URL_FILE`; the direct environment value remains available for local tools.

The same production composition also runs a read-only `public-api` container. It receives a
separate database credential file and the public authority key ID, connects only to the external
database and public ingress networks, and has no Vault network, token sink, CA, AppRole bootstrap,
publisher worker or administrative queue capability. The ingress proxy must join `PUBLIC_NETWORK`
and route `POST /v1/plugin-reputation/query` to port `8080`. It must replace
`X-Forwarded-For` with one canonical client IPv4 address and have its container address inside
`REPUTATION_API_TRUSTED_PROXY_NETWORK`; headers from other peers are ignored. Unknown exact identities return `404`;
the API never synthesizes a clear response. Invalid or unavailable stored evidence returns `503`.

The API and publisher database credentials should use separate PostgreSQL roles. The API role needs
only `CONNECT`, schema `USAGE` and `SELECT` on `plugin_reputation_records`; it must not receive write
access to reputation, decision, operation or audit tables. Set its DSN in the file referenced by
`PLUGIN_REPUTATION_API_DATABASE_URL_FILE`, owned by UID/GID `65534` with mode `0400`.
Readiness checks these capabilities against PostgreSQL and remains unavailable for an overprivileged
or incomplete role. PostgreSQL commonly grants `TEMPORARY` to `PUBLIC`; revoke it from `PUBLIC` on
the dedicated reputation database, then grant it back only to roles which actually require it. The
API role must also have no role memberships, database/schema `CREATE`, `MAINTAIN`, superuser,
role/database creation, replication or row-security bypass capability.
The two sources are mutually exclusive.

Missing or invalid configuration exits with code `2` and prints field names only. Values from `.env`,
database credentials and Vault tokens are never included in the error.

## Vault Bootstrap

An administrator enables Transit and AppRole, creates a non-exportable P-256 key, uploads
`vault/publisher-policy.hcl`, and creates the `plugin-reputation-publisher` AppRole from
`vault/publisher-role.json`. The role has:

- only the `plugin-reputation-publisher` policy;
- no default policy;
- a renewable service token with a 15-minute period;
- one use per SecretID and a maximum SecretID TTL of 10 minutes.

The administrative process uploads `vault/secret-id-issuer-policy.hcl` and gives its trusted
orchestrator a short-lived token with only that policy and no default policy. The orchestrator writes
the RoleID and a response-wrapped SecretID to the Agent bootstrap directory. The wrapping token must
have a creation path of
`auth/approle/role/plugin-reputation-publisher/secret-id`, one use, and a short TTL. No root token,
plain SecretID, database credential, or publisher token belongs in this repository or environment.
The issuer policy cannot modify the role, policy, Transit mount, or key and enforces wrapping between
one second and five minutes.

The issuer token is created outside the API process with only `plugin-reputation-secret-id-issuer`,
no default policy, a short TTL, and renewal disabled. The issuer requests the SecretID with
`-wrap-ttl=5m`; an
unwrapped request and wrapping outside the policy range are rejected. Delivery writes temporary
files in the destination directory, applies the final owner and mode, synchronizes them, and renames
them atomically. Runtime credentials stay outside the checkout.

## Filesystem Boundary

Vault Agent and the publisher run as UID/GID `65534`. Agent owns a private writable bootstrap
directory and a tmpfs-backed token sink directory. The publisher receives only the sink directory as
a read-only mount. The RoleID file uses mode `0400`; the wrapped SecretID and token sink use `0600`.
Parent directories are owned by the deployment administrator and are not writable by unrelated
users or groups. The database URL is stored separately as one UTF-8 line owned by `65534:65534` with
mode `0400`; it is mounted only into the worker and is never passed through environment variables.

`compose.vault-agent.yml` mounts only the CA file read-only and the bootstrap and sink directories
read-write only into Agent. Bind mounts must already exist; Compose does not create missing source
paths. Their common `/run/fluxgram/vault-agent` parent is a root-owned tmpfs with
`nodev,nosuid,noexec`; its `bootstrap` and `secrets` children are owned by `65534:65534` with mode
`0700`. Agent startup fails if either child has another owner or mode. The publisher receives only
the exact database file, TLS CA and sink directory as read-only mounts and does not receive the
bootstrap directory. Both containers have read-only root filesystems, no Linux capabilities, and
`no-new-privileges`; read-only tmpfs mounts override the image's `/vault/file` and `/vault/logs`
volumes. Agent is limited to half a CPU, 128 MiB of memory and 64 processes; the worker uses half a
CPU, 256 MiB and 128 processes.

One host setup matching `vault-agent.env.example` is:

```sh
mount -t tmpfs -o nodev,nosuid,noexec,mode=0755 tmpfs /run/fluxgram/vault-agent
install -d -o 65534 -g 65534 -m 0700 /run/fluxgram/vault-agent/bootstrap
install -d -o 65534 -g 65534 -m 0700 /run/fluxgram/vault-agent/secrets
install -d -o 65534 -g 65534 -m 0700 /run/fluxgram/publisher
install -o 65534 -g 65534 -m 0400 /secure/source/database-url \
  /run/fluxgram/publisher/database-url
```

The Agent removes the wrapped SecretID after reading it. A cold restart after token revocation or
loss requires the orchestrator to deliver a new response-wrapped SecretID. The backend does not
perform AppRole login, SecretID creation, token renewal, or fallback to a static environment token.
Compose health status does not restart an unhealthy container. The deployment orchestrator must
monitor health, deliver a fresh wrapped SecretID, and explicitly restart Agent after token loss.

Provision the three external Vault, database and monitoring networks before startup. The database
network must provide the hostname used inside the database URL file. The configured operations CIDR
must match the dedicated monitoring network and must not overlap the Vault or database networks.
Start Agent and worker together:

```sh
docker compose --env-file deploy/plugin-reputation/vault-agent.env \
  -f deploy/plugin-reputation/compose.vault-agent.yml up -d
```

The healthcheck performs authenticated `lookup-self`; a revoked or expired token does not remain
healthy merely because the old sink file still exists.

The worker exposes port `8090` only inside its Docker networks and accepts non-loopback clients only
from `PLUGIN_REPUTATION_OPERATIONS_ALLOWED_NETWORK`, with at most 32 concurrent connections.
`/health/live` reports the running
event loop, `/health/ready` requires a recent successful queue poll or lease renewal, and `/metrics`
returns fixed Prometheus counters without archive, decision, plugin, database or Vault identifiers.
Compose does not publish this port to the host. An internal monitoring service may scrape it through
the dedicated monitoring network. Before claiming queue work and every 30 seconds, the worker reads
the pinned key and performs a real bounded Transit signature check; loss of that capability exits the
process instead of leaving a false-ready worker. The worker also exits on unknown programming or SQL
errors and Docker restarts it; an expired claim is recovered through the existing fencing and lease
state machine.

## TLS

`vault/agent.hcl` requires Vault at `https://vault:8200` and verifies
`/vault/tls/vault-ca.pem`. The publisher mounts the same CA read-only and configures
`VAULT_ADDRESS=https://vault:8200`, `VAULT_AGENT_TOKEN_FILE=/vault/secrets/token`, and
`VAULT_CA_FILE=/vault/tls/vault-ca.pem`. The certificate must contain `vault` in its SAN.

## Key Policy

The tracked policy reads metadata for exactly one key and signs only SHA-256 digests with
`prehashed=true`. Vault 1.21 rejects value constraints for a correct numeric `key_version` and for
`marshaling_algorithm=asn1`, so the policy requires both fields while allowing the remaining Transit
request fields. The key starts with version `1`, the policy denies rotation, and the host adapter
fixes version `1` and ASN.1 and verifies both the signature version and resulting DER signature
locally. An administrator must treat key rotation as a separate trust transition. The policy does
not grant list, create, rotate, config, backup, export, or delete access.
