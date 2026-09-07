# Möbius Social public host candidate

`app.social_host:app` is the minimal ASGI service for the existing Common
public directory and board. It listens on port 8080 with one explicit worker.
It imports the same `common_protocol`, `common_public`, `common_transport`, and
canonical `net_utils.validate_url_safe` code as the personal router. The image
does not copy the frontend, database code, agent runtimes, owner identity, or
owner keys. Its unprivileged `social` user is uid/gid 1000, matching the
personal image's `mobius` user so existing public files need no ownership
rewrite.

## Module and storage contract

The sidecar exposes only:

- `GET /healthz`
- `GET /version`
- `GET|POST /api/common/directory`
- `GET|POST /api/common/board`
- `GET /api/common/board/media/{id}`
- `GET /api/common/board/{id}/replies`
- `POST /api/common/board/react`
- `POST /api/common/board/reply`

FastAPI documentation and OpenAPI routes are disabled. Reads are anonymous.
Writes require the unchanged `common/0` Ed25519 envelope, a timestamp within
600 seconds, and a remote actor key fetched through DNS-pinned, redirect-free,
SSRF-safe transport. The aggregate public write ingress is limited to 120
requests/minute per actual TCP peer; forwarding headers are deliberately not
trusted.

Set `SOCIAL_DATA_DIR=/data`. **The compatible public data root is therefore
exactly `/data/common`**, not `/data` itself and not `/data/apps/...`. Existing
`directory.json`, `board/*.json`, and `board-media/*.{jpg,png,webp}` files can
be mounted there without conversion. Posts, replies, registrations, media,
and stable IDs are never expired or pruned. New admission stops at 2,000
directory entries, 10,000 board posts, 200 replies per post, and 1 MiB per
image. Reactions stop admitting new hosts at 2,000 per post, and the remote
actor-key cache stops growing at 4,096 entries; imported data beyond an
admission ceiling remains browseable.

An exact reaction-envelope retry is recorded inside its post under bounded
host-private `_reaction_replays` metadata (600-second lifetime, maximum 2,048
live entries). This metadata and the raw `likes` membership map are removed
from feed projections. Only expired replay metadata is compacted; no user data
is involved. All directory and post read-modify-write operations use local
thread locks plus advisory file locks shared across a personal/sidecar cutover,
and files/media use same-directory fsync + atomic rename. The sidecar still
runs one explicit Uvicorn worker so rate-limit and runtime state are singular.

## Exact candidate image commands

From the reviewed repository commit:

The invocation assumes the operator-owned `edge-social` bridge and
`mobius_social_data` volume already exist, as required by the deployment
candidate; it does not create or mutate host topology.

```sh
SOURCE_SHA="$(git rev-parse HEAD)"
docker build \
  --file Dockerfile.social \
  --build-arg "SOURCE_SHA=$SOURCE_SHA" \
  --tag "mobius-social:$SOURCE_SHA" \
  .

docker run --detach \
  --name mobius-social \
  --restart unless-stopped \
  --init \
  --network edge-social \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --mount type=volume,src=mobius_social_data,dst=/data \
  --env SOCIAL_DATA_DIR=/data \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  "mobius-social:$SOURCE_SHA"
```

The source SHA is validated during the build and written to a root-owned,
read-only image file. `/version` never consults an environment variable. The
container needs no network request, database, key, or owner configuration to
become healthy; the `edge-social` network is needed only for ordinary public
HTTPS ingress and remote actor-key lookup after a write arrives. `/data` and
`/tmp` are the only writable mounts in the command above.

The image has no `HEALTHCHECK`; the deploying service should probe
`http://<container>:8080/healthz` over its ordinary service network. Keep the
previous personal-host route and untouched data mount as the rollback until
cutover verification is complete.
