# Container rebuild controller

Settings reviews one exact release before replacing the app container with its
official image. **Finish update** selects the already-applied release’s image;
it remains available after a failed replacement. Source is installed through
one shared Apply path before either a self-hosted or Railway cutover. Startup
only recovers interrupted work and runs installed source plus local changes;
it never fetches or selects a newer release. There is no separate unreviewed
maintenance-rebuild action: replacement requests enter through the exact plan
returned by the update review, including retries of unfinished updates.

The classifier reports independent `required_actions`; its aggregate `level`
is a display summary, not proof that one action covers the others. An image
replacement may also satisfy in-container restart/dependency work, but never
Compose/Railway configuration, proxy policy, or the installed host helper.
Mixed updates require agent assistance before replacement. This is checked
again at the mutation boundary, not only in Settings. Image verification must
not clear outstanding external activation work.

The Host `deploy-prod.sh` freezes the source commit from the image that passed
scratch preflight, exports that exact commit from its complete build checkout,
and applies it through `install_platform_release.py` before cutover. The image
seed itself is shallow; a shallow or moved host checkout must be corrected
before source transfer rather than sending an incomplete Git bundle. Failure leaves the current container running. `--skip-build` does not
install source. Post-cutover checks verify the frozen image source, not moving
`origin/main`. Container rollback does not choose or install another source
release; it retains persistent source and the ordinary recovery safeguards.

Fresh volumes seed the editable clone from the image. If that seed is invalid,
startup serves the baked fallback rather than silently cloning a newer release.
The browser never chooses an image, path, Compose project, or Docker argument.
Self-hosted installations use the host helper below; linked Railway deployments
use the Möbius account service.

Changes to the optional `deploy-prod.sh` command do not require running it or
block Settings updates. It uses its updated source when next invoked. Changes
to the installed host helper are different: update that root-owned helper with
the installer below; an app restart or image replacement cannot replace it.

## Install

From the trusted host checkout that owns the running app:

```sh
sudo scripts/install-rebuild-helper.sh
```

The installer reads the running container's Compose project, working directory,
config-file labels, exact image reference, and absolute environment-file labels. It refuses
untracked topology inputs, environment-file symlinks or relative paths, a
different checkout, or a resolved network set that differs from the live
container. Environment-file contents are never printed; Compose uses them only
to resolve the root-owned frozen model. This preserves both the bundled-Caddy
and shared `edge-caddy` topologies. Rerun the installer after an intentional
topology or configuration change.

If an earlier helper already recreated the container, its Compose labels point
to the frozen root-owned topology rather than the original checkout. The
installer recognizes only that exact helper-owned pair, preserves the frozen
topology, and upgrades the reviewed controller/override from the trusted
checkout. Other out-of-checkout Compose labels still fail closed.

The host must use systemd and Docker on amd64. Official Möbius images are not
currently published as a multi-architecture manifest, so other architectures
fail during installation rather than during a rebuild.

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
3. snapshots only the root-owned `/app/runtime` bytes already executing,
   three-way merges any active local override onto the incoming official
   runtime, and stops before draining chats if the two changed the same lines;
4. returns `no_change` without disturbing chats if that image and prepared
   runtime generation are already live;
5. opens a root-owned one-boot cutover challenge, then asks the running worker
   to close admission, park active turns, and bind them to that exact id;
6. accepts the matching app intent without self-stopping, so Compose owns the
   only stop and the authorization cannot be consumed by an intermediate boot;
7. recreates only `app` from the frozen topology, mounts the prepared runtime
   generation read-only, and verifies readiness, the exact served image
   revision, and the exact mounted-runtime digest;
8. explicitly re-arms the same root receipt for one rollback boot if the new
   container never becomes serviceable, then retires it after either healthy
   outcome; and
9. retains only the active and last-good runtime generations, the current
   helper-owned SHA tag, and one last-good image tag, without a host-wide prune.

The canonical local `scripts/deploy-prod.sh` uses the same challenge → drain →
accept contract when the image identity changes. A running image from before
this protocol cannot provide the frozen root helper, so the first upgrade uses
the existing owner-presence gate and says so explicitly; subsequent Host
rebuilds preserve eligible active chats even with `--force-now`.

An installation of the older Host helper is reported as **upgrade required**
and cannot queue a Settings rebuild. Re-run
`sudo scripts/install-rebuild-helper.sh` from the current trusted checkout;
the refusal happens before chat admission is closed or an image is touched.

## One-time Railway upgrade

A Railway deployment can serve a current `/data/platform` checkout while its
baked image still predates the managed cutover supervisor. Settings reports
that capability in the ordinary update review rather than offering a separate
unreviewed upgrade. The confirmed review pins the exact official revision and
image digest; the browser cannot select an image or provider resource. Finish
uses a matching durable replacement receipt to recover the applied image's
digest, or verifies that discovery still names that same applied release. If
neither proves the image identity, Settings asks the owner to review the latest
update instead of silently changing the target.

The account service verifies the current deployment and rollback point, issues
a one-use handoff, and owns the Railway deployment. Möbius starts it only when
no chat runner is alive, closes admission, and revalidates the reviewed source
before consuming the nonce. A stale review reopens admission without starting
the deployment. The candidate image must become healthy and publish the `external-cutover-v1`
capability witness or the account service rolls back both the deployment and
configured image source. The witness is bound to the candidate boot id, so a
stale marker on the shared volume cannot make a rolled-back legacy image look
capable. Once this succeeds, later Railway rebuilds use the normal managed
challenge, drain, and receipt protocol.

Ordinary local source remains in `/data/platform` and follows the normal merge
reconciliation after boot. Protected runtime is deliberately narrower: the
controller carries forward only bytes already active under the previous
root-owned image boundary. Newer editable `backend/runtime` source remains
pending and is never promoted to root code merely because an image was
rebuilt. Dockerfile, dependency, bootstrap-script, and other local-only image
inputs remain blockers because they have no equivalent active-generation
receipt.

## Resolving an active-runtime conflict

A real overlap stops before chat drain and reports the exact paths. Resolve the
file in a reviewed commit that contains the requested official target, then an
operator may stage only those detected conflict files:

```sh
sudo /usr/local/libexec/mobius-rebuild-host stage-runtime-resolution \
  <running-container-id> <official-target-sha> <reviewed-resolution-commit>
```

The root helper recomputes the conflict from the running `/app/runtime`, the
two immutable Git histories, and the fixed persistent platform checkout. It
accepts only the exact conflict path set, binds the staged files to the current
runtime digest and official target, and does not mutate or restart the live
container. A later Settings rebuild consumes the receipt only after the
new container is healthy; a failed rebuild retains it for a safe retry.
