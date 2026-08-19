# Container rebuild controller contract

Settings exposes one operation: replace this instance's app container with the
official image for the upstream revision already applied inside Möbius. The
browser never chooses an image, deployment, path, project, or provider.

## Normalized job

The platform expects a durable external job with these states:

`idle`, `queued`, `preparing`, `waiting_for_work`, `replacing`, `verifying`,
`succeeded`, `no_change`, `failed`, `rolled_back`, or `needs_recovery`.

Every response is a JSON object containing `supported`, `operation_id`,
`state`, `expected_sha`, `code`, `message`, and `updated_at`. The controller,
not the disposable app container, owns the job record.

## Self-hosted

Run `sudo scripts/install-rebuild-helper.sh` from the trusted host checkout.
The installer refuses untracked or modified helper/Compose inputs. It resolves
the deployment once into a root-owned Compose snapshot, creates a dedicated SSH
identity, and installs a forced-command dispatcher that permits only `status`
and `rebuild`. The worker never rereads the checkout. It grants no Docker-group
membership and accepts no caller-supplied Compose arguments. Re-run the
installer after an intentional Compose topology or configuration change.

The helper pulls `ghcr.io/mobius-os/mobius:sha-<expected_sha>`, verifies the
source, revision, and architecture labels, freezes the image ID, recreates only
the recorded `app` service, waits for its health check, and restores the prior
image if verification fails. It does not prune caches or build locally.

## Möbius Launch / Railway

Launch is the durable controller for Railway deployments. The platform uses
the existing instance SSO credential for:

- `POST /api/managed/instances/{instance}/container-rebuilds`
- `GET /api/managed/instances/{instance}/container-rebuilds/current`

POST receives only `{ "expected_sha": "<40 lowercase hex>" }` and an
`Idempotency-Key`. Launch must verify that the requested revision is an
available official Möbius image, serialize rebuilds per instance, trigger the
Railway replacement, and retain status across the old deployment disappearing.
It must never accept a caller-supplied Railway project, service, image, branch,
path, build command, or deployment arguments.

Before starting, Launch should reject resource constraints without disrupting
the current deployment. Stable error codes should distinguish at least:

- `image_unavailable`
- `insufficient_memory`
- `insufficient_disk`
- `plan_deployment_unavailable`
- `already_running`
- `provider_unavailable`
- `verification_failed`
- `rollback_failed`

The message should name the practical next action. In particular, low-cost
Railway plans may lack enough temporary disk or memory to stage a replacement;
that is a normal preflight failure, not a reason to remove Railway support or
attempt a local build inside the app container.
