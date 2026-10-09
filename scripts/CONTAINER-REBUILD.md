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
block Settings updates. It uses its updated source when next invoked. Compatible
changes to the installed helper, Compose files, or Railway configuration follow
the same rule: existing installations keep their proven host-owned configuration,
while new installations and later intentional host refreshes use the updated
reference source. `Caddyfile` is deliberately different: it is the active
self-hosted routing policy, so a change still requires a proxy reload.

## Deployment compatibility contract

The image and served application must remain compatible with the installed
host controller and deployment topology by default. Editing a host-owned source
file therefore does not, by itself, block a reviewed image replacement. A
release that actually requires new external behavior advances the corresponding
reserved marker:

| Required external change | Marker path | Fixed legacy bridge | Activation |
|---|---|---|---|
| Self-hosted helper capability or protocol | `deployment/self-hosted-helper.required` | revision comment in `scripts/install-rebuild-helper.sh` | Reinstall the helper |
| Self-hosted mount, port, network, privilege, or secret delivery | `deployment/self-hosted-topology.required` | revision comment in `docker-compose.yml` | Deploy from the current host topology |
| Railway service or deployment configuration | `deployment/railway-topology.required` | revision comment in `railway.toml` | Apply the Railway configuration |

The marker may be introduced only when the first incompatible migration needs
it. Its content is a monotonically advancing revision: never delete, rename,
reuse, decrement, or revert it. Update review compares endpoint trees, so this
monotonic identity is what makes an installation that skipped releases still
see every outstanding external requirement.

Every marker introduction or advance must set the same monotonically advancing
revision in a retained comment in the fixed legacy bridge file from the table.
Never delete, rename, reuse, decrement, or revert that bridge revision, even if
the surrounding configuration changes later. Installations that predate
marker-aware classification already treat those legacy paths as external
activation requirements, so their endpoint comparison sees the retained bridge
and stops safely too. This bridge is permanent: an owner may skip directly from
any older release, so do not assume the installed updater already understands
markers. The bridge comment may also explain the migration, but its monotonic
revision—not temporary wording—is the durable identity.

Compatibility is with configurations already installed in the field, not only
the repository's current defaults. Advance a marker when a new image or served
feature requires external behavior that an older installation lacks. Comments,
portable implementation fixes, optional features, and defaults used only by new
installs do not require a marker. A preflight that boots successfully is
valuable evidence, but cannot prove mounts, privileges, secrets, or security
policy that may matter only after readiness; release review owns this
declaration.

An image health receipt never proves an external migration completed. After the
operator follows the migration-specific instructions, verify the installed
helper or provider configuration and retire only that exact pending activation
requirement. Never clear the complete activation ledger merely because the new
image is healthy.

## Install

This controller is the general self-hosted update path; **Connect is not a
prerequisite and is not part of the replacement protocol**. Connect, SSH, a
hosting control panel, or an operator's local terminal may be used once to run
the installer below, but normal reviewed updates then travel only through
Settings → the persistent request inbox → the root-owned systemd worker. The
app never receives a Docker socket or host credentials.

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

Each admission attempt uses a unique Compose project and `container_name`,
while `io.mobius.admission.project` records the fixed original deployment
project. Shared named networks and volumes remain external resources with their
original names; app aliases and the persistent data mount are preserved.
No-start creation has no service dependencies to start or another attempt to
scale down. Each attempt reads an immutable root-owned
`/etc/mobius-rebuild/attempts/<token>.json`, not a changing shared override.

On reinstall, discovery unions running containers with that fixed owner label
and legacy containers with the original Compose project plus app-service labels.
Exactly one full CID must remain after deduplication; two containers are an
ambiguity, not permission to prefer one. Labels alone grant no trust: the
installer verifies the root-frozen original project, exact current override,
immutable attempt file derived from the validated token, image/startup argv,
read-only code mount, private state mount, and original data source. It rechecks
live identity under the installation locks.

For helper-owned containers, the installer copies only the unwrapped root-owned
`compose.yml` as the reusable base. It **preserves** a validated wrapped current
`image.override.yml` as the worker's live-identity pointer, without merging it
into that base. Legacy helper-created containers remain recognized by their
exact original frozen Compose-file pair. A base already containing admission
mounts, wrapper, or attempt labels is refused rather than freezing stale
authority. Other out-of-checkout Compose identities still fail closed.

The host must use systemd and Docker on amd64. Official Möbius images are not
currently published as a multi-architecture manifest, so other architectures
fail during installation rather than during a rebuild.

For an owner-controlled image built from a trusted local checkout, run
`scripts/deploy-prod.sh` on that Docker host through whatever operator access is
already available. When the root replacement helper is installed, its lock is
root-owned in a private directory, so the **entire production deploy** must run
with host privileges, not just the final Compose command. From the trusted,
current host checkout, use an absolute script path, for example:

```sh
sudo /absolute/path/to/trusted-checkout/scripts/deploy-prod.sh
```

The script refuses an unprivileged helper-managed production deploy before its
expensive build. It does not elevate itself or relax the lock permissions.
Privileged invocations use `/run/mobius-deploy` for their deployment lock,
ignoring caller-controlled cache paths and refusing symlink lock files.
`--check` remains verification-only and `--target=test` does not require the
production helper lock. `sudo` can change `HOME`, Git identity/configuration,
safe-directory trust, and credential access, which matter to this script's Git
fetch and release checks. Verify the privileged invocation can read the trusted
checkout and configured release remote before deploying; use narrowly scoped
Git configuration if needed, **not** broad environment preservation such as
`sudo -E`. Alternatively, use the reviewed Settings update through the installed
helper rather than a manual deploy. Connect is one optional way to reach the
host, not a product dependency. The manual path scratch-boots the exact locally built image before
cutover and uses the same authenticated chat handoff and rollback contract.
It can carry image-definition and protected-runtime changes. A release that
changes Python packages stops before source installation when the running image
predates the image-owned boot transaction; cross it with a container-only
upgrade first (below).

## Container-only upgrade for images before the boot transaction

An image that predates `backend/runtime/boot-protocol` cannot install a release
that changes Python packages: its served code refuses source whose packages it
does not have, and no newer served code can reach it. Move only the container
first, with the helper installed above. From inside the Möbius container, as the
`mobius` user, run the script from the fetched upstream, so nothing new has to
be installed:

```sh
if [ "$(git -C /data/platform rev-parse --is-shallow-repository)" = true ]; then
  git -C /data/platform fetch --no-tags --unshallow origin +refs/heads/main:refs/remotes/origin/main
else
  git -C /data/platform fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
fi
git -C /data/platform show origin/main:scripts/request-container-upgrade.py | python3 -
```

The script queues the helper for the latest official image (`--target` names an
exact release instead; `--check` only reports whether it would) after checking,
under the updater's lock, that the target is an official release that ships this
bridge and is newer than the running image and the source's official package
declarations, that no update is prepared or parked, and that the helper is
idle. The helper drains chats, replaces only
the container, and attempts to restore the previous container if the new one is
unhealthy. If exact restoration cannot be confirmed, the host retains the
transaction and reports `needs_recovery`; the request is not safely retryable.

The new image's boot transaction serves the unchanged source only after proving
from the image's own history that none of that source's official Python
package declarations is newer than the image, then probes it, with the image's
own release as the floor. Install the release from Settings right away:
its packages are now in the image, so the ordinary update proceeds (served code
from #1311 on, 23 September 2026, discounts package inputs the running image
already carries; the script refuses older served code, which needs an update
to at least that release first). An explicit `--target` must carry exactly the
latest official release's Python packages, since Settings installs that
release next. Until then,
older source runs on the newer packages; the probe proves that it imports,
nothing more. A release that also advances `deployment/self-hosted-helper.required`
then asks you to reinstall the helper from a current trusted checkout.

## Self-updating worker

The installer puts a small frozen **launcher** at
`/usr/local/libexec/mobius-rebuild-host` (`scripts/mobius-rebuild-launcher.py`)
and seeds it with the checkout's **worker** (`scripts/mobius-rebuild-host.py`).
The systemd units are unchanged. The launcher contains no replacement logic: it
runs the active worker with an allowlisted environment (`python3 -I -S`, fixed
`PATH`, `/` as working directory). It stores workers and their selection record
(`workers.json`, with sha256 per file) root-private in `/var/lib/mobius-rebuild`,
and refuses any file that is not root-owned, private and unchanged.

After a replacement succeeds (or finds the image already live), the worker
takes `/app/platform-baked/scripts/mobius-rebuild-host.py` out of that exact
image ID. It creates a container without starting it, accepts exactly one
regular file within a size and time bound, and removes the container and its
volumes. It offers the file to the launcher as a **candidate** only when the
file compiles and declares a `WORKER_REVISION` (read as text, never by running
it) above every revision offered or installed so far. That high-water mark
never drops, so neither a dropped candidate nor an older official image's
worker is ever offered again.

The launcher moves a candidate into a durable recovery-owner pin before its
one trial. Only that exact, hash-verified worker reconciles its unfinished
transaction; the old active worker cannot interpret a new worker's preboot
stages. All worker selection and execution share one dispatch lock, inherited
by active, candidate and recovery children. It survives launcher death,
including the window before the worker creates its journal, so a stale active
selection cannot bypass a newly pinned owner. Neither recovery
nor an interrupted launcher replays the candidate's original `run`.

A fresh successful outcome promotes the candidate only when its recorded
predecessor remains active. The pin retains the baseline operation/status so
recovery can finish promotion after a crash without trusting stale success.
A rollback or failed trial keeps the previous active worker. Worker files are
small and never deleted while the index can still name them.

Worker changes normally reach installed hosts through ordinary updates. Changes
to the frozen launcher, admission wrapper, or systemd units instead require an
explicit helper migration. Revision **5** retains launcher revision **2** and
installs the reviewed boot-admission wrapper. An image update alone does not
install these host prerequisites. Reinstall from the reviewed checkout only
during separately authorized host maintenance.

The installer refuses **any existing transaction**, including an unwrapped
legacy rollback whose Start may already have been issued, before changing
controller code or units; it checks again while holding the dispatch and
replacement locks in launcher order. This also prevents the timer from selecting
a stale worker to run immediately after installation. Each lock acquisition has
a 30-second budget: a busy controller makes installation refuse, not terminate
active recovery or wait indefinitely.
Keep the transaction and handoff evidence intact and settle the incident through
its existing recovery owner or explicitly authorized manual maintenance first.
Installation cannot retroactively enroll an old consumer or grant a second boot.
It does not restart the healthy app, clear the restart ledger, or invoke recovery;
it only publishes helper capabilities. Normal request/timer processing resumes
after installation and may dispatch independently queued approved requests.

### Explicit boot-admission migration

The installer atomically pins reviewed, tracked, clean
`scripts/mobius-boot-admission.py` at
`/usr/local/libexec/mobius-boot-admission.py`, owned by root. It also pins the
reviewed `scripts/mobius-manual-cutover.py` adapter at
`/usr/local/libexec/mobius-manual-cutover.py`, so manual deployment enters the
same installed worker protocol rather than issuing an independent replacement.
Both helpers must be tracked and clean; installation validates both before
publishing either. Its root-owned
`/var/lib/mobius-rebuild/admission` directory is mode **0700**, outside `/data`
and the legacy entrypoint's recursive ownership repair. Reinstallation preserves
admission records and lock identities; it never clears consumed slots.

For each newly created target **and rollback**, the worker mounts that code
read-only at `/run/mobius-boot-admission.py` and state read-write at
`/run/mobius-admission`. It preserves the original image ENTRYPOINT/CMD and opens
admission only after verifying the exact container, image, and effective wrapper
configuration. The wrapper runs before the untouched original entrypoint.
Thus the recorded **exact legacy last-good image** can remain a rollback option
without claiming its frozen ledger implements the new shared lock. This is an
explicit reviewed host deployment change, not replacement of legacy runtime
bytes or an implicit capability gained by an image update.

One rollback slot has monotonic attempt generations. The wrapper durably admits
at most one host-authorized handoff for that slot. Once admitted, the host never
starts, rearms, cancels, or recreates that rollback. Ordinary autonomous Docker
restarts may still boot service with a fresh boot ID, without renewing this
cutover's authority, even before transaction settlement. A later service-only
boot must retire leftover matching acceptance from a crash between admission
and the legacy ledger's consumption; unrelated fresh restart authority is not
silently erased. This is not a promise of only one physical Docker process start.

The preexisting **unwrapped source** does not acquire a gate merely because the
helper was installed. After its exact CID is removed, continuation eligibility
must be established from the still-unconsumed original acceptance and original
source boot ID, not the root receipt alone. Source consumption, partial evidence,
or an unexpected source restart permanently disables continuation for this
operation; wrapped target/rollback containers may restore service without
reissuing that handoff. This degraded outcome retains honest `needs_recovery`
evidence. An already-issued unwrapped recovery incident still requires its
existing recovery owner or explicit manual settlement before installation.

An ambiguous Start is never retried on the same CID. Only a provably unadmitted
attempt may be durably CLOSED under the admission lock, then removed by its
exact full CID. The old attempt stays denied across host death. A new generation
may be created only after removal is confirmed and explicit unconsumed
cancellation lineage survives. An admitted, unexplained missing, changed, or
previously attempted rollback does not become recreateable from Docker metadata
or a v1 acceptance witness. Missing proof remains `needs_recovery`.

A new worker refuses a replacement trial under launcher revision 1 without
changing status or claiming the request, allowing that launcher to put the
candidate back for the installer. The app's unclaimed-request status makes
that queued condition actionable without changing the root trial outcome.
An interrupted legacy launcher can still
lose that pending candidate; its revision high-water protection is not bypassed.
A fixed helper without a launcher remains supported and reconciles its own work.

### Recovery transitions

Every worker transition below holds `replace.lock`. A transaction is removed
only after publishing its durable outcome. Reconciliation repeats observations,
not an already-consumed chat handoff.

| Durable state | Evidence required | Permitted next action |
| --- | --- | --- |
| Prepared replacement | Exact previous image and accepted cutover | Journal replacement intent before Compose mutates the app |
| Replacement started | Fresh whole-container observation | Observe the target; retain an old source still running; restore only the recorded previous image |
| Rollback creating | Recognized target/previous image, no prior rollback boot | Compose creates without starting; journal exact rollback container |
| Rollback prepared | Exact verified wrapped container and open, unconsumed admission slot | Persist start intent before one Start; refresh handoff only within the original receipt lifetime |
| Rollback starting | Authoritative admission slot under its shared lock | If admitted, observe only; otherwise durably CLOSE and fence the exact CID before a new generation, never retry ambiguous Start |
| Rollback running/restarting | Exact previous image | Observe readiness; never recreate a boot that is making progress |
| Healthy with missing handoff proof | No exact consumed receipt | Keep service running and retain `needs_recovery`; do not manufacture an ACK |
| Durable outcome | Exact outcome already recorded | Republish status and clear the journal, without another boot |

A failed container observation retries discovery and inspection together.
Multiple containers are ambiguous, not a reason to pick the first ID. The
recurring systemd timer continues reconciliation after transient Docker errors
or a lost immediate recovery attempt. Budgets are separate from individual
query timeouts. Each remove/create/start mutation has a 300-second ceiling;
target and rollback readiness each have 600 seconds. One full cutover/recovery
pass allows six mutations (source removal, target create/start, target fencing,
rollback create/start), both readiness windows, and 600 seconds of bounded
observation/settlement overhead: **3600 seconds**. Both the main service's
`TimeoutStopSec` and the boot reconciler's `TimeoutStartSec` use that allowance.
The main service's explicit `TimeoutStartSec=9000` adds the 3600-second image-pull
budget and 1800 seconds for preflight, provenance, adoption, and cleanup. A
transient failure or interrupted pass retains its journal for the periodic
reconciler; these budgets never authorize another consumed boot.

The wrapper's admission lock and durable slot serialize consumer admission with
host revocation; the outer replacement lock alone cannot do so. The legacy
supervisor still consumes its ledger independently, but the host never refreshes
or rearms that rollback after wrapper admission. Docker's `created` state or zero
`StartedAt` cannot prove that authorization is unconsumed. The original receipt
expires after one hour; refreshing acceptance does not extend it. Expiry can
leave a serviceable rollback with manual chat recovery still required. Never
clear that transaction merely because health is green.

The root status records `worker_revision`, `launcher_revision` and the last
`worker_adoption` outcome for operators. Fixed helpers keep working: the app does not require the launcher, and
`deployment/self-hosted-helper.required` advances only for a change the
launcher itself or an older fixed helper cannot provide.

**Trust.** The worker runs as root on the host, so the authority to publish
`ghcr.io/mobius-os/mobius:sha-*` becomes authority to publish host-root code on
self-hosted installations that update. Only a push to `main` publishes (see
`.github/workflows/main-image.yml`). The worker checks the image's labels
(revision, source, architecture) for consistency. They are not a cryptographic
publisher identity, and it pins the verified image ID rather than the mutable
tag. The app, even compromised, can only choose among published official SHAs,
resend or withdraw requests, and consume pull and disk resources. It cannot
choose the repository, executable, worker, Compose topology, or host commands.
Revision monotonicity stops an older worker from being selected; it does not
stop an older official application image from being deployed.

## Boundary and lifecycle

The app writes one fixed `request.json` into the persistent `/data` inbox. A
root-owned `systemd.path` unit starts a one-shot worker; durable status is
mirrored back into `/data` for polling without a host process or network
handshake. A boot-time one-shot attempts to reconcile interrupted transactions
left by a host power loss; failed recovery remains visible for operator action.
The request contains only the expected 40-character upstream
SHA and, from request version 2, an app-generated nonce that the helper echoes
as `request_nonce` in its status, so the app can tell its exact replacement's
outcome from any earlier one. It is claimed atomically on the same persistent
filesystem before use. The app requires a helper that advertises request
version 2 (`request_versions`); `deployment/self-hosted-helper.required`
revision 1 asks older installations to reinstall it. Installing the launcher
once makes later worker changes arrive with updates (see above).

The worker:

1. checks Docker free space and pulls `ghcr.io/mobius-os/mobius:sha-<sha>`;
2. verifies the image source, revision, and amd64 architecture;
3. returns `no_change` without disturbing chats if that exact image is already
   live and its protected runtime comes directly from the image;
4. opens a root-owned one-boot cutover challenge, then asks the running worker
   to close admission, park active turns, and bind them to that exact id;
5. accepts the matching app intent without self-stopping, so Compose owns the
   only stop and the authorization cannot be consumed by an intermediate boot;
6. recreates only `app` from the frozen topology and verifies readiness, the
   exact served image revision, and that `/app/runtime` is the image's own
   immutable protected code rather than a host-generated mount;
7. explicitly re-arms the same root receipt for one rollback boot if the new
   container never becomes serviceable, then retires it after either healthy
   outcome; and
8. retains only the current helper-owned SHA tag and one last-good image tag,
   without a host-wide prune.

The canonical local `scripts/deploy-prod.sh` uses the same challenge → drain →
accept contract when the image identity changes. A running image from before
this protocol cannot provide the frozen root helper, so the first upgrade uses
the existing owner-presence gate and says so explicitly; subsequent Host
rebuilds preserve eligible active chats even with `--force-now`.

An installation of the older Host helper is reported as **upgrade required**
and cannot queue a Settings rebuild. This includes the retired helper that used
host-generated protected-runtime overlays despite advertising the current chat
handoff version. Re-run
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

Ordinary local source remains in `/data/platform` and follows the normal overlay
reconciliation after boot. Privileged runtime follows the same rule as any
other served module: the frozen `/app/runtime/served_runtime_launcher.py`
validates and starts the identity broker from `/data/platform/backend/runtime`,
so an edit there is activated by the next restart and never blocks a
replacement. If that served copy is missing, symlinked,
group/world-writable, or does not compile, boot selects the complete baked
platform instead. A served backend never runs beside the image's broker.

Everything else under `backend/runtime` (the restart-ledger supervisor, the
launcher itself, and any module added there later) stays image-owned and
follows the Dockerfile rule: it must be present in the exact reviewed image or
the replacement blocks before chat drain. Identity keys and linked state remain
under persistent `/data/identity-broker`.

The image must therefore contain the launcher before a served broker becomes
authoritative. The one-time transition from an image that predates the
launcher is handled by the updater version shipped in that old image; current
boot has only the single whole-platform source decision described above.

## Disposable release replay

The **Tests** workflow has a manual `release_replay` input (off by default).
It runs `scripts/test-host-helper.sh` on a fresh GitHub-hosted Ubuntu runner,
using published releases `238360c3e762be161e4fdb0a6c5d909c3af9e98b` (worker 1)
and `ab689f035552fca839bf221c4882c98ff931b24b` (worker 2). It does not build or
publish an image and needs no live-host credentials. Ordinary PR, merge-queue,
and manual runs without the input do not run this job.

The replay installs the previous release and its helper once. It prepares the
reviewed target source, then uses the real inbox, root launcher, cutover and
container replacement. Persistent fixture declarations request `figlet` and
`pyfiglet==1.0.4`; the pristine target image must lack both. After replacement,
the target source must be loaded, its update record retired, setup results
ready, both packages usable, and the fixture data preserved—without a manual
setup rerun or installation in the target container. A second real replacement
back to the previous image must be performed by worker 2 and promote that worker;
its active file and recorded hash must match the target release's worker bytes.
The historical pair has unchanged platform dependency locks and database schema;
changing the pair requires reviewing downgrade compatibility again.

For an equivalent **fresh disposable systemd host only**, with Docker Compose
and both commits fetched:

```sh
sudo env MOBIUS_RELEASE_REPLAY=1 scripts/test-host-helper.sh \
  238360c3e762be161e4fdb0a6c5d909c3af9e98b \
  ab689f035552fca839bf221c4882c98ff931b24b
```

Never run this against a live installation. It uses the `mobius` container,
volume and systemd names and installs root-owned helper files; the entire runner
is disposable. The replay refuses an existing container, data volume or helper
configuration, helper state, executable or units. It proves this historical upgrade path, not deployment-specific
state on another host, and does not exercise post-update conflict resolution.
Allow up to 60 minutes; network/registry/package failures fail the test rather
than count as success. Hosted execution and any publication of the test branch
are separate owner decisions. Running locally available syntax/contract tests
is preparation, not evidence that the real image replay passed.
