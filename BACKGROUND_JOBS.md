# Secure background jobs

Möbius has two server-side app-job tiers:

- **Ordinary jobs** run reviewed, owner-installed app scripts with the
  historical authority of the Möbius process.
- **Background-agent jobs** declare `permissions.background_agent: true`.
  They can run an AI agent without an owner watching, so they receive a
  narrower, owner-reviewed data contract enforced by a process sandbox.

This document defines that contract, why its implementation is portable, and
the verification required to change it. Browser iframe isolation is a separate
boundary; see [`CAPABILITIES.md`](CAPABILITIES.md) for browser-side apps.

## The stable design

The stable part of the system is a small semantic contract:

```text
JobAccess
  source_read
  storage_write
  extra_read
  extra_write
```

For the current manifest vocabulary this means:

- app source is readable and not writable;
- the app's numeric storage is readable and writable;
- shared Memory data is absent, read-only, or writable exactly as reviewed;
- supported provider credential directories are writable because their CLIs
  may refresh credentials;
- the app gets a minimal environment, a short-lived app token, and a unique
  writable home/temp directory;
- the editable platform checkout, database, service token, other app data,
  undeclared shared data, sibling-process control, and host UNIX sockets are
  denied (read-only image runtime under `/app` remains visible);
- outbound IP networking remains available.

The runner derives this contract once. Executors consume it; they do not
interpret manifests or contain Memory-specific rules.

Two executors implement the contract:

1. **Bubblewrap** is preferred when a real namespace probe succeeds. It
   supplies private mount, PID, IPC, and UTS namespaces and hides masked paths.
2. **Landlock** is the fallback when the kernel exposes ABI 6 or newer and a
   complete enforcement probe succeeds. `setpriv` installs filesystem rules;
   a small helper adds Landlock process/abstract-socket scopes and seccomp
   denials for pathname UNIX sockets and direct sibling-process inspection.

If neither probe succeeds, the job does not run. There is no unsandboxed
fallback for a reviewed background agent.

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

### Probe behavior, not host names

Möbius does not branch on Railway, Docker, Kubernetes, architecture, or an
environment variable claiming a feature exists. Bubblewrap is selected only
after the namespace operation needed by a real job succeeds. Landlock is
selected only after its ABI, filesystem restriction, process scoping, and
socket filter all work together.

The probes run at job launch. They are cheap compared with an agent job and
avoid a capability cache that can become stale after a container or host
change.

### Prefer the strongest working executor; fail closed

Bubblewrap remains first because its private namespaces provide a stronger and
easier-to-explain boundary. Landlock is not presented as identical: protected
path metadata and process IDs may remain visible even though contents,
mutation, signalling, inspection, and local socket access are denied.

The shared contract is therefore expressed as allowed and denied operations,
not as an identical filesystem view. A future job that genuinely requires a
private PID or mount namespace must become a new explicit requirement; it must
not silently receive the Landlock executor.

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

## Why the system reached this point

Background-agent isolation was introduced when Memory moved from
platform-owned code into a modular system app. Bubblewrap was a sound initial
executor: it could make the container filesystem read-only, mask owner data,
mount only reviewed paths, and isolate processes with familiar namespace
semantics.

Nested Bubblewrap is not only an image property, though. The outer container
runtime must permit namespace and mount setup. The bundled Docker Compose
deployment was later given the required capabilities and security profile, so
that deployment worked. Managed runtimes that do not expose equivalent outer
container controls can reject Bubblewrap before app code starts. Memory made
the gap visible because it was the first Store app to combine
`background_agent` with install-time initialization.

That initialization exposed three independent integration assumptions in
sequence: the backend was not ready to mint a scoped token, scheduled jobs
assumed the old local port and baked runner, and the managed host rejected
Bubblewrap's namespace setup. Each correction belongs to its owning layer:
readiness in bootstrap launch, address/runner selection in the shared job
handoff, and host portability in secure executor selection. None belongs in
Memory itself.

The resulting lesson is narrower than “build a sandbox framework”:
Bubblewrap was coupled to one deployment topology, while the reviewed access
contract was not. Landlock provides a second enforcement path on modern
restricted hosts without requiring namespace creation. Keeping both small
preserves stronger isolation where available and portability where it is not.

Related architecture already documented elsewhere:

- [`ARCHITECTURE.md`](ARCHITECTURE.md) defines “solve at the core,” “design for
  the next change,” and “keep the shared foundation lean.”
- [`CAPABILITIES.md`](CAPABILITIES.md) establishes the broader pattern that
  declarations are owner-readable contracts and mechanisms are narrow
  providers.
- [`SECURITY.md`](SECURITY.md) distinguishes hardened technical boundaries
  from accepted trade-offs.
- The original runner comments explain the narrower background-agent data
  contract and why jobs write as the `mobius` data owner.
- The Compose security settings explain why nested Bubblewrap needs explicit
  outer-runtime support.

## Alternatives considered

| Alternative | Why it is not the current design |
|---|---|
| Bubblewrap only | Excludes demonstrated managed hosts that deny nested namespaces. |
| Landlock only | Discards stronger private namespaces and excludes older kernels where Bubblewrap already works. |
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
- Landlock: ABI 6+, filesystem enforcement, denied sibling signals and process
  inspection, denied `AF_UNIX` sockets, parent-death termination, and temp
  cleanup.

Selection tests cover Bubblewrap preference, Landlock fallback, and the
fail-closed case with both diagnostics. CI may skip a real executor only when
the host cannot provide it; each supported deployment topology must therefore
run one end-to-end secure-job smoke test rather than treating a skip as proof.

A release-level startup smoke should install a trivial `background_agent` app
or Memory on a fresh volume, wait for its ready marker, and fail with the
executor diagnostics if initialization cannot start. This catches image,
kernel, outer-runtime, callback-address, and startup-order integration failures
that unit tests cannot.

## Change checklist

When this boundary changes:

1. Keep manifest interpretation in the runner and enforcement in executors.
2. State any executor asymmetry explicitly; do not weaken the common contract.
3. Run the shared adversarial suite against every available executor.
4. Run one real secure job in each supported deployment topology.
5. Verify job-group termination, parent-death behavior, and temp cleanup.
6. Check both AMD64 and ARM64 images because syscall numbers, system packages,
   and seccomp resolution are architecture-sensitive.
7. Keep failures actionable and never silently run a background agent as an
   ordinary process.
