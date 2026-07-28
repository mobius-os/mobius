# Server-side app-job authority

Möbius has two server-side app-job authority profiles:

- **Platform-authority jobs** run reviewed, owner-installed app scripts with
  the historical authority of the Möbius process.
- **Scoped jobs** receive a narrower, owner-reviewed data contract enforced by
  a process sandbox. These jobs declare
  `permissions.job_authority: scoped`.

These names describe operating-system authority, not whether a script happens
to use AI. Platform-authority jobs may run agents, while a scoped job may run
ordinary deterministic code. Scheduled versus on-demand execution and
`embeds_agent` are separate choices.

This document defines the scoped contract, why its implementation is portable,
and the verification required to change it. Browser iframe isolation is a
separate boundary; see [`CAPABILITIES.md`](CAPABILITIES.md) for browser-side
apps.

## Decision: one contract, two private executors

The reviewed access contract is portable; Linux enforcement mechanisms are
not. Some supported hosts deny the namespace and mount operations Bubblewrap
requires but provide Landlock ABI 6+. Others allow Bubblewrap but lack a usable
Landlock implementation. Möbius therefore keeps one filesystem policy with two
private launch adapters, selects by probing required behavior, and fails closed
if neither works.

This is deliberately not an executor framework. Apps cannot select a mechanism,
and executor details do not enter manifests or capability policy. Remove
Bubblewrap when every supported deployment passes the Landlock probe; remove
Landlock when every supported deployment permits Bubblewrap.

## The stable scoped design

The stable part of the system is a small semantic contract:

```text
JobAccess
  source_read
  storage_write
  extra_read
  extra_write
```

For scoped authority this means:

- app source is readable and not writable;
- the app's numeric storage is readable and writable;
- shared Memory data is absent, read-only, or writable exactly as reviewed;
- supported provider credential directories are writable because their CLIs
  may refresh credentials;
- the app gets a minimal environment, a short-lived app token, and a unique
  writable home/temp directory;
- the editable platform checkout, database, service token, other app data, and
  undeclared shared data are denied (read-only image runtime under `/app`
  remains visible);
- sibling signalling and direct memory/file-descriptor inspection are denied;
- outbound IP networking remains available.

`JobAccess` is deliberately a filesystem contract. The runner derives it once;
executors consume it without interpreting manifests or adding Memory-specific
rules. Process and socket isolation are executor properties described below,
not fields that this four-path value pretends to make identical.

Two executors implement the contract:

1. **Bubblewrap** is preferred when a real namespace probe succeeds. It
   supplies private mount, PID, IPC, and UTS namespaces and hides masked paths.
2. **Landlock** is the fallback when the kernel exposes ABI 6 or newer and its
   required-primitives probe succeeds. `setpriv` installs filesystem rules; a
   small helper adds Landlock signal/abstract-socket scopes and seccomp denials
   for pathname UNIX sockets and direct sibling-process inspection.

If neither probe succeeds, the job does not run. There is no platform-authority
fallback for a reviewed scoped job.

### Startup and scheduled execution

Executor portability is only useful if every launch reaches it consistently:

- bootstrap initialization waits on the existing `/api/ready` contract before
  requesting its scoped token and job context;
- interactive and scheduled launches receive the backend's configured local
  address rather than assuming port 8000;
- Run now and cron prefer the served checkout's runner;
- startup schedule reconciliation prefers the served checkout's scaffold and
  rewrites older persisted entries through the current runner.

The baked runner and scaffold remain degraded-boot floors. They are not the
normal path after a platform update: preferring them would preserve old launch
behavior until the next image rebuild even though the served backend had
already advanced.

## Design philosophy

### Contract over mechanism

An app asks for access, not for Bubblewrap or Landlock. Deployment mechanics
must not leak into the manifest. This keeps app review stable when kernels,
container runtimes, and hosting platforms change.

### Keep authority small; express nuance as access

Authority is intentionally a small choice: either a job is confined to its
reviewed resources or it is trusted with platform process authority. The
scoped resource contract carries the useful nuance—source, storage, declared
shared data, provider credentials, and future specific capabilities.

Do not add an authority profile merely to express a new resource permission.
Add a narrow field to the scoped contract instead. A genuinely new profile is
justified only when a demonstrated requirement needs a materially different
enforcement boundary, such as hostile-tenant or resource-quota isolation.

### Name authority directly

An app with a server-side job may declare
`permissions.job_authority: scoped|platform`. Omitting the field preserves the
historical platform authority for existing ordinary jobs. The public
declaration therefore describes the operating-system boundary directly rather
than implying that sandboxing depends on whether a script uses AI.

The earlier `background_agent` boolean has been removed rather than retained as
an alias. Silently ignoring that spelling would grant its sole official
consumer platform authority. Current receipts record the declared authority
directly; coherent older receipts remain readable for existing volumes.
Missing, malformed, contradictory, or unknown receipt data fails closed so a
future schema cannot silently change an installed job's authority.

### Probe behavior, not host names

Möbius does not branch on Railway, Docker, Kubernetes, architecture, or an
environment variable claiming a feature exists. Bubblewrap is selected only
after the namespace operation needed by a real job succeeds. Landlock is
selected only after its ABI, a real filesystem denial, signal scope, and socket
filter all work together.

The probes run at job launch. They are cheap compared with an agent job and
avoid a capability cache that can become stale after a container or host
change. They answer “can this host provide the required primitives?”, not “has
every adversarial behavior test just been rerun?” The latter belongs in the
test suite and deployment smoke checks.

### Prefer the strongest working executor; fail closed

Bubblewrap remains first because its private namespaces provide a stronger and
easier-to-explain boundary. Landlock is not presented as identical: protected
path metadata and process IDs may remain visible even though contents,
mutation, signalling, inspection, and local socket access are denied.

The shared contract is therefore expressed as allowed and denied operations,
not as an identical filesystem view. A future job that genuinely requires a
private PID or mount namespace must become a new explicit requirement; it must
not silently receive the Landlock executor.

### Deliberate limits

Scoped app jobs are reviewed internal code in a single-owner system. This
boundary reduces the data exposed to a job; it is not a hostile-tenant
container or a CPU, memory, and process-count quota.

Landlock does not create a private PID namespace. Same-owner scheduling and
resource-limit controls may therefore remain possible even though sibling
signals, process memory, file descriptors, and protected `/proc` contents are
denied. Its parent-death signal covers the directly launched process, not an
arbitrary descendant that deliberately creates an independent lifetime. A job
that creates a separate session owns that session's cleanup, as it owns its
other application-level resources.

Socket behavior also differs. Landlock blocks `socket(AF_UNIX, ...)`, so a job
cannot open pathname or abstract UNIX endpoints; private
`socketpair(AF_UNIX, ...)` IPC remains available. Bubblewrap masks the pathname
socket locations used by the host, but keeps the network namespace so jobs
retain outbound IP networking; it does not promise a separate abstract UNIX
namespace. Apps must not depend on addressable private UNIX sockets unless that
becomes an explicit reviewed requirement with shared tests.

### One policy, small adapters

There is one path policy and two launch builders. There is deliberately no
executor plugin registry, host capability database, background probe daemon,
deployment matrix in production code, or general guarantee algebra. Add such
machinery only after a real second policy requires it.

Use maintained system interfaces where possible. In particular, util-linux
`setpriv` owns Landlock filesystem rule construction. Möbius keeps only the
small helper needed for protections that tool does not expose.

### Make the decision inspectable

While a job runs, its existing lease records `executor: process|bubblewrap|
landlock`. A Landlock fallback records why Bubblewrap was rejected. If no
executor qualifies, the durable app-job log records both probe diagnostics.
This reuses the lease and failure log rather than adding a database or health
service.

## Origin

Scoped authority was introduced when Memory moved from platform-owned code into
a modular app. Bubblewrap was a sound first executor, but its namespace setup
depends on privileges granted by the outer container runtime; installing the
binary inside an image cannot recover privileges the host withholds. Landlock
made the same reviewed data contract enforceable on such hosts without
namespace creation.

Memory's install-time initialization also exposed readiness, callback-address,
and stale-runner assumptions. Those corrections live in the shared launch path,
not in Memory or the sandbox adapters. The durable lesson is to keep app policy
independent of both application identity and deployment mechanism.

## Alternatives considered

| Alternative | Why it is not the current design |
|---|---|
| Bubblewrap only | Excludes supported hosts whose outer runtime denies nested namespaces. |
| Landlock only | Discards stronger private namespaces and excludes supported hosts where Landlock is disabled or too old. |
| A different namespace launcher | Cannot recover namespace or mount privileges withheld by the outer runtime. |
| A container or VM per job | Moves isolation to a host orchestrator and adds deployment-specific images, mounts, credentials, and lifecycle. |
| Run unsandboxed if probing fails | Silently violates the permission the owner reviewed. |
| Branch on deployment name | Brittle: the relevant property is kernel/runtime behavior, not branding. |
| Add retries or a durable job queue | Does not fix an executor that can never start; solves a different problem. |
| Cache host capabilities or run a probe daemon | Adds invalidation and lifecycle machinery to avoid millisecond launch probes. |
| General executor/plugin framework | No demonstrated third executor or second policy justifies the abstraction. |

## Verification contract

Every executor must pass the same adversarial data test:

- read app source and declared shared data;
- write app storage and its unique temp directory;
- fail to read the service token and database;
- fail to write outside declared writable paths;
- run durable writes as the `mobius` data owner.

Each executor also verifies its mechanism-specific boundary:

- Bubblewrap: real namespace creation, masked owner data, and process-group
  revocation.
- Landlock: ABI 6+, filesystem enforcement, denied sibling signals and direct
  process inspection, denied addressable `AF_UNIX` endpoints, direct-launcher
  parent-death behavior, and temp cleanup.

Selection tests cover Bubblewrap preference, Landlock fallback, and the
fail-closed case with both diagnostics. CI may skip a real executor only when
the host cannot provide it; each supported deployment topology must therefore
run one end-to-end secure-job smoke test rather than treating a skip as proof.

A release-level startup smoke should install a trivial app declaring
`job_authority: scoped`, or Memory, on a fresh volume; wait for its ready
marker; and fail with the executor diagnostics if initialization cannot start.
This catches image, kernel, outer-runtime, callback-address, and startup-order
integration failures that unit tests cannot.

## Change checklist

When this boundary changes:

1. Keep manifest interpretation in the runner and enforcement in executors.
2. State any executor asymmetry explicitly; do not weaken the common contract.
3. Run the shared adversarial suite against every available executor.
4. Run one real secure job in each supported deployment topology.
5. Verify job-group revocation, direct-launcher parent-death behavior, and temp
   cleanup; separately test any sessions an app intentionally creates.
6. Check both AMD64 and ARM64 images because syscall numbers, system packages,
   and seccomp resolution are architecture-sensitive.
7. Keep failures actionable and never silently run a scoped job with platform
   authority.
