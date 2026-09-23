# Platform and app update architecture

**Status:** proposed target contract, not a description of the current updater.

This document defines the update behavior Möbius should converge on. The
current implementation is mapped in `ARCHITECTURE.md`; where it differs, this
document is the plan rather than a claim that the work has shipped.

The goal is simple from the owner's point of view:

1. Review the exact update and what it will require.
2. Apply it once.
3. Keep using Möbius immediately when no restart is needed.
4. Restart or replace the container only when the changed layer requires it.
5. If activation fails, attempt automatic rollback where compatibility and an
   executor are proven; otherwise preserve the reviewed manual recovery plan.
   In-app repair depends on recovery-runtime compatibility.

Möbius starts an agent only when the owner clicks **Resolve in chat** for
overlapping edits or **Open repair chat** after a failed activation. Predictable
preparation, validation, activation, instructions, and rollback are platform
work and must not start an agent.

## 1. One update, four possible activation levels

Every update is first prepared and checked without changing the served version.
The changed files then determine the smallest activation level:

| Level | Examples | Owner-visible action |
|---|---|---|
| **Live** | frontend, docs, tests, ordinary app files | Apply; no interruption |
| **Server restart** | backend Python, runtime agent instructions | One explicit Restart action after all such changes are batched |
| **Image replacement** | base Python/Node, native or system packages, protected boot code | One reviewed container replacement |
| **Host action** | port, mount, resource, self-hosted proxy, Docker engine, host kernel, or provider topology | Clear external instructions; never disguised as an in-app restart |

The label is an outcome of the prepared candidate, not a choice the owner must
understand in advance. Internally, activation is a **set of required actions**:
an image replacement does not automatically complete an unrelated proxy reload
or provider-topology change. Settings shows the highest owner-visible label,
the reasons, prerequisites, deployment scope, and separate completion evidence
for every required action.

The existing detailed classes map into those labels as follows:

| Existing class | Owner-visible label |
|---|---|
| `live` | Live |
| `server_restart` | Server restart |
| `dependency_sync` | Image replacement until generation-specific dependency environments exist; Server restart afterward |
| `image_rebuild` | Image replacement |
| `proxy_reload`, `container_recreate`, `host_maintenance` | Host action |

This deliberately replaces the current default “Update needs help → Ask
Möbius” behavior for predictable external work with direct instructions;
**Ask Möbius** remains a secondary help action.

### Minimise higher levels

- Frontend and app builds publish atomically and remain live-first.
- Batch all pending backend changes into one restart.
- A compatible Python or JavaScript dependency may be prepared live only in a
  **generation-specific environment**, never by mutating the shared interpreter
  and hoping it can be restored. The exact declaration and lock change lands at
  the same time. A Python environment under `/data` is keyed by base-runtime and
  lock fingerprints; the boot launcher selects it before importing the new
  source. JavaScript keeps its similarly fingerprinted dependency tree. Native
  builds, base-runtime changes, or unresolved compatibility require an image.
- Reserve image replacement for base/runtime incompatibility, native or system
  dependencies, protected image-owned code, and changes that cannot be made
  safely reversible in the live container.
- Reserve host actions for facts the container cannot own: mounts, ports,
  resources, provider settings, Docker/host upgrades, and kernel work.

Live installation is an optimisation, not a second source of truth. The locks
remain the durable reproducible definition. Settings should report that the
current dependency generation is newer than the image until a later image
replacement absorbs it. Until isolated generations exist, Python dependency
changes remain image-level; the current shared `pip install` path is not an
exactly reversible foundation.

## 2. Git model: preserve history; merge the current trees once

### What “linear overlay” means today

The current platform updater treats every local commit as a patch that must be
replayed, one by one, on top of each new upstream target. It tries to decide
which old commits have already landed upstream, drops those, and recreates the
rest. This produces a tidy straight line, but forces the updater to preserve and
reinterpret historical commit boundaries forever. That is the source of much
of its special machinery.

### Target model: an ordinary three-way merge

Keep normal Git history:

```text
                 L1--L2   owner/agent work
                /      \
upstream: A--B--C--D-----M   prepared merge candidate
```

- Upstream commits retain their original order.
- Local commits retain their original order.
- Git finds the common ancestor and combines the **net content change** on each
  side once.
- The merge commit `M` records both parents. It does not copy every upstream
  commit onto the local branch, and it does not squash or discard local history.
- Work already present in both histories normally contributes no remaining
  content difference. Squash-landed, independently recreated, reverted, or
  later-edited contributions can still conflict; ordinary Git does not prove
  semantic equivalence.
- A textual conflict is sent to a resolver. A clean textual merge can still be
  semantically wrong, which is why the merged candidate is validated before it
  is published.

Settings should therefore report useful present-tense facts—changed files,
remaining local content differences, activation level, and genuine conflicts—
not imply that every historical local commit must be carried forever.

Fast-forward remains the simplest case when local history is already contained
in the target. A clean ordinary merge is the divergent case. A conflict creates
one candidate for one resolver chat; the served branch remains untouched.
Unrelated or rewritten upstream history is refused rather than guessed through.
Shallow history is deepened far enough to establish the real merge base.

In owner terms: **your changes stay yours; each update is combined with them
once.** If Git reports an unresolved conflict, nothing changes until the owner
chooses to resolve it.

## 3. Platform update transaction

The platform update is one crash-recoverable transaction with four phases. One
versioned state record replaces the current collection of conflict, offline,
rolled-back, reconcile-pre, restart-needed, and progress flags. It records the
candidate, active and last-ready generations, phase, activation operation,
rollback budget, and last failure. This does not justify a new database or a
generic workflow engine.

### Served generation: the real publication boundary

A Git ref is history, not by itself a complete running version. A **served
generation** names the exact backend tree, frontend artifact, dependency
environment, image/protected-runtime compatibility, and desired core-service
state. All named artifacts are written and checked first; one atomic manifest
pointer selects the complete set.

The editable `/data/platform` checkout remains the owner's workspace. Edits made
after Apply remain there as drafts but cannot change the frozen generation the
next restart will boot. Frontend-only updates may advance the frontend pointer
live. When frontend and backend must move together, the old coherent pair stays
served until restart selects the new pair.

### Phase A — discover and prepare during Review

1. Fetch and pin the exact upstream source commit. If an official image is
   relevant, pin its immutable digest too.
2. Snapshot the current branch, index, relevant tracked edits, untracked source
   paths, modes, and symlinks.
3. Carry uncommitted source into the isolated candidate as a transient commit.
   Review never commits, resets, or otherwise mutates the served checkout.
4. Create one isolated candidate worktree.
5. Fast-forward or perform one ordinary whole-tree merge into that candidate.
6. If Git reports a textual content conflict, keep the served version unchanged
   and offer one explicit **Resolve in chat** action. Review itself never starts
   an agent.
7. Freeze the candidate identity: source tree, both parents, target, activation
   reasons, dependency inputs, and any image digest.

Review prepares the exact candidate Apply will use. Review may take a few
minutes; closing Settings retains it. Apply may cheaply recompute from a changed
workspace only to prove that the resulting tree and activation requirements are
still identical. It must never silently apply a different outcome.

### Phase B — validate the isolated candidate

Run the checks this container can own without serving the candidate:

- source and manifest checks;
- frontend compilation into a staging generation;
- backend imports and complete router registration;
- dependency resolution and reproducibility checks;
- protected-runtime and image-input compatibility;
- migration dry run when database code changed.

Tests remain release/CI evidence, not a variable production-instance gate.
Image build and scratch boot belong to the executor that can actually build the
image: release CI, a self-hosted helper, or Railway. If no such executor is
connected, the candidate is **awaiting external validation**, not “validated.”

For database changes, a fresh empty database is not enough. The dry run uses a
transactionally consistent, WAL-aware disposable copy of the deployed database
and isolated copies of any filesystem state the migrations touch. Schedulers,
outbound effects, and production credentials remain disabled. The previous
ready generation is also tested against the migrated copy when automatic
rollback is claimed.
The September restart incident caused by renamed migration identities is the
reason for this rule: a clean database can pass while a long-lived installation
fails at boot.

The candidate is not yet served. Failed validation is a safe Review failure,
not an outage and not a rollback event.

### Phase C — compare again, then publish last

Immediately before Apply:

1. Re-snapshot the relevant source content, modes, symlinks, index, dependency
   inputs, and served-generation identity.
2. If the workspace changed, recompute the merge in isolation. Proceed only
   when its tree and complete required-action set exactly equal the reviewed
   candidate; otherwise mark Review stale.
3. Acquire publication admission, repeat the final comparison while holding it,
   and retain admission through publication. Changed inputs require a retry or
   stale Review. Do not hold admission across human review.
4. Create and durably record the complete frozen generation and activation
   receipt.
5. For a live update, compare-and-swap the expected active generation. For a
   deferred restart or replacement, publish it as **pending** while the served
   manifest remains unchanged; Phase D selects it. History-ref updates are
   crash-reconciled from the transaction record, not treated as the serving
   commit point.

This closes the publication race. The restart race is closed separately because
boot loads the frozen reviewed generation rather than whatever happens to be in
the editable checkout later. If another backend candidate is batched before
restart, it creates a new combined generation and activation receipt; the old
approval does not silently authorize it.

### Phase D — activate and verify

**Live:** atomically select the build generation and verify the served source/build
identity. No restart card.

**Server restart:** show one typed Restart action. Immediately before stopping,
repeat the quick production-import/router probe against the exact source to be
booted. The new process must prove:

- it is a later boot than the request;
- it serves the expected source commit;
- database and chat writer are ready;
- required routers loaded;
- boot-critical supervisors report readiness for the expected generation and boot.

A later unrelated healthy boot must not satisfy an old activation wait.

**Image replacement:** an external controller performs the cutover. The
in-container platform can prepare and request it, but must not control the Docker socket. The new
deployment must report the expected image digest/source identity and pass
readiness before completion.

**Host action:** preserve the prepared transaction and show the exact external
step. Recheck the transaction after the owner or hosting provider completes it.

## 4. Failure and rollback

The normal failure path stays inside Möbius:

1. A failed candidate boot records a bounded status with the failing stage and
   traceback or log excerpt.
2. Before trying recovery, its owner—the frozen supervisor for source activation
   or the external controller for image activation—durably spends the one
   rollback attempt for that activation operation.
3. Source activation restores the complete last generation that reached
   readiness: backend, frontend, dependencies, desired core services, and
   compatible configuration. Image activation is rolled back only by its
   external cutover owner, which must also select the previous complete served
   generation; restoring an old image while retaining failed active source is
   not a rollback.
4. If that boot succeeds, Möbius opens normally, shows that the update was
   rolled back, and offers a repair chat containing the evidence.
5. If the mutable platform cannot render, the complete baked shell/backend and
   matching dependency environment are selected together. The repair route is
   available only when that recovery runtime can safely authenticate and operate
   on current persistent data; otherwise it exposes database-independent
   diagnostics and external recovery instructions.
6. If the one rollback also fails, stop cycling. Surface the external recovery
   instructions and preserve both failure records.

Do not roll back the whole platform because one optional app schedule or
non-critical service failed. Required infrastructure decides core readiness;
optional services report degraded state and may restart independently.

Automatic rollback cannot make every database change reversible. Shipped
migration identities and code remain append-only. Schema/filesystem changes
must be backward compatible with the recorded last-ready generation, or the
update must declare an explicit non-automatic migration/cutover plan before
Apply. Compatibility with an old baked fallback is not assumed merely because
the image boots.

## 5. Container and Dockerfile design

The Docker image should become a stable bootstrap floor, not the normal feature
delivery mechanism. That minimises image replacements and host actions without
weakening recovery.

### Keep image-owned

- base OS, Python and Node runtimes;
- native/system libraries and command-line tools;
- root entrypoint, privilege drop, and restart ledger;
- the protected identity boundary;
- a complete baked backend and shell for fallback;
- pinned, reproducible dependency locks and image provenance labels.

The external cutover helper is host/controller-owned and independently
versioned. The image may carry a reviewed copy for installation, but the helper
that replaces the image cannot be owned only by the image it is replacing.

### Keep out of the image when practical

- ordinary frontend and backend feature delivery, served from frozen
  generations derived from `/data/platform` (the image still contains a
  complete fallback copy);
- mini-apps and their generated artifacts;
- policies and defaults that can be safely read from the served source;
- provider-specific actions that belong in an external adapter rather than the
  entrypoint.

### High-value Dockerfile improvements

1. **Keep the protected bootstrap interface small and versioned.** The stable
   launcher should select and prove one complete served generation. New feature
   code should not require editing root boot code.
2. **Make reproducibility complete.** Keep deterministic multi-stage builds,
   lockfiles, checksums, source revision labels, and architecture checks; pin
   base images by digest and make the OS-package source/version policy explicit.
3. **Keep the fallback as a complete unit.** Never mix a mutable backend with an
   incompatible baked privileged module or shell.
4. **Add a first-class image self-test target.** It should run imports, router
   registration, protected-runtime parity, and a scratch health/readiness boot.
   The same test runs locally, in CI, on Railway-built images, and for self-hosts.
5. **Add generation-specific dependency selection.** Record the base-runtime,
   declared-lock, and installed fingerprints so boot can select the exact
   environment and a later image can absorb it without guesswork.
6. **Optimise rebuild time, not ownership.** Keep expensive pinned runtime layers
   before frequently changing source and use build caches where the host supports
   them. Faster builds reduce pain; they do not justify an in-container Docker
   socket.

These changes reduce how often the image must change. They cannot remove host
actions for ports, mounts, resources, provider settings, Docker itself, or the
host kernel because those are outside the container by definition.

## 6. Managed Railway and self-hosted execution

The validation contract is provider-independent. The provider changes **who
executes the cutover**, not what counts as a valid candidate.

### Railway with a linked Möbius account service

Möbius can discover the newest completely published official GHCR image, pin
its immutable digest and source revision, ask the linked account service to
redeploy that exact image, then verify the reported digest/source after
readiness. This avoids building unreviewed moving source inside the owner's live
service.

Replacing the image does not replace the owner's served source. Local changes to
image-owned inputs therefore block replacement unless the official image is
proven compatible; they are never silently discarded.

Railway itself can also build a repository Dockerfile when a service is connected
to source, or when `railway up` uploads source. That is a Railway capability, not
proof that this Möbius instance is linked to the Möbius account service.

### Railway without that link

An unlinked Railway service has two distinct paths:

- **Image-based service:** show the exact official image tag/digest and the
  manual dashboard/CLI redeploy step. Confirm the running source/image identity
  afterward where the deployment exposes it.
- **Source-connected service:** the reviewed merge must reach the owner's
  configured deploy branch; Railway then builds that Dockerfile. Verify the
  reported source revision after readiness. There may be no container digest
  visible to Möbius, so do not pretend there is one.

Source-only updates still work in Möbius. An ordinary server restart is offered
only after the installation proves its effective restart capability; merely
having `ALWAYS` in a repository checkout does not update Railway's active
service configuration, and Railway does not offer `ALWAYS` on every plan.

For an image replacement Möbius keeps the reviewed candidate and waits without
turning the absence of a link into an error. No link means manual execution, not
weaker validation and not a forced agent diagnosis.

### Self-hosted Docker

An external host helper owns replacement. It may either:

- build the reviewed source locally, scratch-boot the exact image, and cut over;
  or
- pull the official digest/revision, verify its labels and architecture, and cut
  over.

Today `scripts/deploy-prod.sh` owns the local-build path and
`scripts/mobius-rebuild-host.py` owns the official-image path.

Neither path depends on Connect. For official updates, a one-time host install
creates a root-owned systemd path worker; Settings publishes a fixed reviewed
SHA into its persistent inbox, and that worker performs the replacement. For a
custom local image, the operator runs `scripts/deploy-prod.sh` on the Docker
host using any existing host access (terminal, SSH, control panel automation,
or Connect). These are executor transports around the same cutover contract,
not separate update semantics, and Möbius never needs the Docker socket.

Before cutover it pins the current image as the one rollback target. After
cutover it checks health, readiness, protected-runtime parity, and served source.
On failure it restores that pinned image once. The helper runs outside the app
container so it survives the replacement it initiates.

### Image validation before deployment

An image build succeeding proves only that layers assembled. The executor that
built it must start the exact image in an isolated scratch container, answer
health and readiness, report the expected source/protected runtime, and pass its
self-test before production cutover. Release CI does this for official GHCR
images; the self-hosted helper does it for local builds. Railway's production
build/health gate does **not** substitute for isolated scratch validation.
Without an executor providing that evidence for the exact image, including an
unlinked source-connected Railway service, the candidate remains **awaiting
external validation**. Post-deploy identity verification is still required; if
running identity cannot be proven, activation remains awaiting verification.

A schema-changing update additionally needs the separate migration dry run
described above; a scratch boot with an empty database does not cover that case.

## 7. Mini-app updates

Platform and app updates share the same transaction principles but should not
share a generic framework merely for symmetry.

### Shared principles

- pin all remote bytes and Git identities during Review;
- prepare an immutable candidate without changing the served version;
- validate and compile the candidate in isolation;
- re-snapshot local state immediately before publication;
- commit the accepted source/runtime pointer, capabilities, metadata, and
  **desired** schedule/skill state as one logical transaction, then reconcile
  those external effects idempotently and show incomplete convergence;
- ask for an agent only for an unresolved content conflict;
- retain a durable resolver receipt so a restart cannot lose the reviewed
  candidate.

### App-specific differences

- Managed Store discovery uses the fetched Git identity/ancestry plus capability
  differences. A display version alone is not the update decision.
- Apps may add permissions, storage seeds, schedules, skills, icons, and static
  assets; Review must show those capability changes.
- Each app has a compiled runtime generation. The previous generation stays
  served until the new source and bundle are both ready.
- A multi-app Store update is a batch of independently prepared app candidates.
  One app's conflict does not require reverting already valid unrelated apps,
  but the UI must state clearly which apps applied, conflicted, or failed.
- Most app updates are live. A platform restart is needed only if an app update
  also requires a new shared platform runtime contract, which should be rare and
  explicit.

Apps currently preserve every local commit by replaying it onto the fetched
package commit in an isolated candidate. That is intentionally not changed by
the platform merge simplification: managed app origins, resolver receipts,
publication handoffs, capability review, and Store batching have different
constraints. The transferable lesson is immutable candidate → compile →
re-snapshot → publish last, not a mandatory shared Git-history shape. Synthetic
baselines remain only a finite adoption bridge for older installs, not a second
steady-state package model.

## 8. Acceptance gates

Before implementation of this target contract is called complete, realistic
tests must cover:

- clean live update;
- clean backend update plus one restart;
- true source conflict and one resolver;
- local edits arriving after Review and before Apply/restart;
- dependency-generation preparation success and failure;
- image scratch boot and exact-identity cutover;
- existing-database migration dry run;
- Railway clean-stop restart behavior;
- new source failing the pre-stop probe;
- new source passing pre-stop checks but failing early boot, followed by one
  successful rollback;
- failed rollback without a loop, with compatible baked repair or
  database-independent diagnostic navigation available;
- one broken optional schedule while the core platform becomes ready;
- linked Railway, both unlinked Railway modes, and self-hosted cutover;
- process death at every generation-publication and rollback boundary;
- a new frontend that needs its new backend while restart is postponed;
- a correct image with the wrong served source, and vice versa;
- source contribution merged upstream by squash, then edited again;
- WAL-active database copy, interrupted migration ledger write, and rollback
  compatibility against migrated data;
- external deployment accepted while its acknowledgement is lost;
- required supervisor failure after database readiness;
- app crash before/after its database commit, followed by schedule/skill
  convergence retry.

## 9. Deliberate simplifications

This design intentionally does **not** add:

- a generic platform/app update framework;
- a new update database table when one versioned filesystem record is sufficient;
- an automatic agent for predictable failures;
- a long-lived lock across Review and owner approval;
- per-commit replay or permanent patch-equivalence bookkeeping;
- automatic rollback loops;
- a Docker socket in the app container;
- Railway-specific health as a prerequisite for ordinary in-process readiness.

The durable core is smaller: one immutable candidate, one state record, one
final comparison, one complete-generation publish point, the smallest set of
required actions, exact post-activation proof, and at most one rollback.

## 10. Implementation order

1. Establish the complete served-generation pointer, one durable state record,
   last-ready identity, and crash-safe bounded rollback. A merge-engine change
   is not safe to ship before this publication boundary exists.
2. Make activation completion prove the exact generation, source/image,
   activation operation, boot, and required supervisors; bring the upstream
   restart preflight hardening onto this instance.
3. Add isolated dependency generations and the baked repair/diagnostic boundary.
4. Replace platform per-commit replay and its predictor/equivalence path with
   one prepared ordinary-merge candidate and publish-last Apply. Audit and retain
   contribution trust/provenance records that have duties beyond replay.
   `app_git.replay_overlay` and `retire_landed_equivalent_changes` remain because
   apps use them; this slice removes only the platform caller, predictor, and
   platform replay state.
5. Make required supervisor and per-app schedule readiness observable without
   letting one optional app suppress every scheduler.
6. Add production-state migration preflight and end-to-end deployment journeys.
7. Keep the app updater's commit-preserving replay, while tightening its
   accepted-runtime/desired-effects publication and convergence reporting.

Each slice must remove the platform-only machinery it replaces. Do not leave
platform replay and merge as parallel permanent paths.

## 11. Investigation status — 2026-09-22

Three recent Railway failure classes are now distinguished:

1. **Clean planned stop stayed down.** A planned restart exits cleanly. Railway's
   former `ON_FAILURE` policy did not restart it. Merged PR `#1195` changes the
   service to `ALWAYS` with a bounded retry count; that setting is present in
   this checkout's `railway.toml`. It affects a Railway service only after its
   active deployment configuration is updated, and `ALWAYS` is unavailable on
   Railway Free/Trial plans.
2. **Unsafe source was allowed to restart.** Merged PR `#1295` adds a
   production-equivalent import/router probe before restart, repeats it at the
   last possible boundary, keeps failed restart cards retryable, and limits
   completion to a recent ready boot. It is upstream but not yet in this
   machine's current branch (which is 13 upstream commits behind at this review).
   Its ready receipt is still time-bounded rather than fully generation-bound;
   the target contract above closes that remaining gap.
3. **Existing database state failed after restart.** Renamed migration identities
   passed on a fresh database but failed against a long-lived Railway database.
   This is why source boot checks and deployed-state migration checks are both
   release gates.

The first two fixes are real and useful; neither proves the whole restart path
on a particular live Railway service. Effective restart policy, plan limits,
served source, migration state, and post-boot readiness must still be observed.

## References

- Railway Dockerfile builds: <https://docs.railway.com/builds/dockerfiles>
- Railway deployment lifecycle: <https://docs.railway.com/deployments/reference>
- Railway restart policy: <https://docs.railway.com/deployments/restart-policy>
- Railway deployment actions and rollback: <https://docs.railway.com/deployments/deployment-actions>
- Current implementation map: `ARCHITECTURE.md`
- Activation classifier: `backend/app/platform_activation.py`
- Platform updater: `backend/app/platform_update.py`
- App update Git engine: `backend/app/app_git.py`
- App publisher: `backend/app/install.py`
- Self-hosted preflight/cutover: `scripts/deploy-prod.sh`
- External self-hosted image helper: `scripts/mobius-rebuild-host.py`
