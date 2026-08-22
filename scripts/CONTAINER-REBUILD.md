# Container replacement controller

Settings can replace a self-hosted app container with the official image for
the upstream revision already applied inside Möbius. The browser never chooses
an image, path, Compose project, or Docker argument. Railway deliberately
reports this action as unsupported until its provider transaction exists.

## Install

From the trusted host checkout that owns the running app:

```sh
sudo scripts/install-rebuild-helper.sh
```

The installer reads the running container's Compose project, working directory,
and config-file labels. It refuses untracked inputs, a different checkout, or a
resolved network set that differs from the live container. This preserves both
the bundled-Caddy and shared `edge-caddy` topologies. The resolved Compose model
is frozen under root ownership; rerun the installer after an intentional
topology or configuration change.

The host must use systemd and Docker on amd64. Official Möbius images are not
currently published as a multi-architecture manifest, so other architectures
fail during installation rather than later during replacement.

## Boundary and lifecycle

The app writes one fixed `request.json` into the persistent `/data` inbox. A
root-owned `systemd.path` unit starts a one-shot worker; durable status is
mirrored back into `/data` for polling without a host process or network
handshake. A boot-time one-shot reconciles any active status left behind by a
host power loss. The request contains only the expected 40-character upstream
SHA and is claimed atomically on the same persistent filesystem before use.

The worker:

1. checks Docker free space and pulls `ghcr.io/mobius-os/mobius:sha-<sha>`;
2. verifies the image source, revision, and amd64 architecture;
3. returns `no_change` without disturbing chats if that image is already live;
4. asks the running worker to use Möbius's existing restart drain, which closes
   admission, parks active turns, and authorizes their boot continuation;
5. recreates only `app` from the frozen topology and verifies health;
6. restores the prior image on failure; and
7. retains only the current helper-owned SHA tag plus one last-good rollback
   tag, without a host-wide prune.

Möbius refuses the request when its activation receipt records local-only image
or baked-runtime changes that the official target does not contain. Undeclared
manual changes inside a container cannot be discovered reliably and remain
ephemeral; replacing any container removes them.
