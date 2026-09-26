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
not a broken dependency, and don't try to add Docker: the CLI alone has no
daemon, and a socket or privileged daemon would cross into host authority.

Recovery is not a daemon, listener, alternate boot mode, or second process
inside Möbius. If the interface is unavailable, ask the partner to use their
deployment's Recovery path:

- **Managed deployment:** open **Recovery** from the deployment's card in the
  service that launched it. It opens a temporary root shell into the live
  Möbius container from outside, and is removed when the session ends.
- **Self-hosted:** the operator already owns the host and can attach directly:

  ```bash
  docker compose exec -u 0 app bash
  ```

Either way the shell attaches to the normal live container; there is no
separate Recovery boot profile. Never try to start or repair an in-container
Recovery service; none should exist.

## Host-owned container replacement

A container recreation is not an ordinary server restart. Use the Settings
replacement controller; never `docker restart` or force-recreate the container
by hand, because that bypasses the handoff that parks active chats and falls
back to manual Resume after boot.

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
