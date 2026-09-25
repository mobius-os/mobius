# Platform reference

Rarely needed platform facts: why the app container has no Docker and how
external Recovery works, host-owned container replacement, where chat, app, and
storage files live, and how to view an app directly. Open only the section the
current task needs; everyday activation and restarts are in
`platform-maintenance`.

---

## The container boundary and external Recovery

The normal Möbius app container intentionally has no Docker daemon or CLI and
does not mount the host's Docker socket. `sudo` grants root inside that
container only. Treat Docker's absence there as an expected trust boundary,
not a broken dependency. Do not install a Docker CLI, start Docker-in-Docker,
or request a host-socket mount for agent tests: the CLI alone has no daemon,
while a socket or privileged daemon would cross into operator-owned host
authority and would not work consistently on managed deployments.

Recovery is not a daemon, listener, alternate boot mode, or second process
inside Möbius. If the interface is unavailable, ask the partner to open
**Recovery** from the deployment card in Möbius Launch. The launcher creates a
separate temporary worker on demand and pins Railway SSH to the exact live
Möbius service instance. Commands reach that container as root, while the
worker itself remains outside the container and is deleted when the session
finishes or expires. Never try to start or repair an in-container Recovery
service; none should exist.

Self-hosted operators use the authority they already own:

```bash
docker compose exec -u 0 app bash
```

That also attaches to the normal live container; it does not select a Recovery
boot profile.

## Host-owned container replacement

A container recreation is not an ordinary server restart. On a self-hosted
Host, use one of the two owning paths:

- `scripts/deploy-prod.sh` for a checkout/image deployment; or
- the installed Settings replacement controller documented in
  `scripts/CONTAINER-REBUILD.md` for an official-image refresh.

Both paths open a root-owned cutover challenge, ask the still-running worker to
park and nonce-bind exact active chat runs, then let Docker perform the only
stop. A failed replacement explicitly re-arms the same receipt for one rollback
boot. This is why a raw `docker compose up --force-recreate`, `docker restart`,
or direct container replacement is not an equivalent shortcut: it bypasses the
handoff and intentionally falls back to conservative manual Resume after boot.
Unexpected crashes remain manual by design; never make arbitrary boots look
planned merely to hide recovery prompts.

The running image must already contain the frozen `external-cutover-v1` helper.
The first upgrade from an older image cannot manufacture that root capability;
`deploy-prod.sh` says when it is using the legacy owner-presence gate, and that
one upgrade installs the helper for later replacements.

---

## File locations

- Uploaded files: `/data/chats/{chat_id}/uploads/`
- Chat media: `/data/chats/{chat_id}/media/`
- Encrypted app credentials: `/data/app-secrets/{app_id}/` — use the app-secret
  API, never edit ciphertext files.
- Per-app storage (numeric id): `/data/apps/{app_id}/<path>`
- Per-app source (slug): `/data/apps/{slug}/`
- Shared storage: `/data/shared/<path>`
- Compiled bundles: read the exact `compiled_path` from `GET /api/apps/{id}`
- Cron logs: `/data/cron-logs/`
- Owner service token: `/data/service-token.txt` (mode 0600)

Chat files are purged when their chat is permanently deleted after the
retention window. Put data that must outlive a chat in per-app or shared
storage.

## Viewing apps directly

Capture an app through the authenticated shell, which supplies the frame-init
message a standalone frame does not receive:

```bash
bash "$SCRIPTS_DIR/agent-screenshot.sh" --content-only /app/<id>
```

The frame URL is stable and cache-revalidated, but opening it alone normally
ends at “Loading timeout.” Use the authenticated capture helper or the live
shell.
