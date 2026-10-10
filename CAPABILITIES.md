# Mini-app capability architecture

Möbius mini-apps run in opaque-origin frames by default. Opacity removes
ambient shell authority; capabilities add back narrow, legible operations.
This document defines the app API, wire protocol, provider contract, review
model, lifecycle rules, and escape hatches.

## Design rules

1. **One broker, many providers.** Browser features do not invent their own
   `postMessage` dialects.
2. **Capabilities are general primitives.** The platform captures audio or
   returns a chosen file; the app decides how a sequencer or editor behaves.
3. **Declaration is not ambient authority.** A request must be declared in the
   installed contract, supported at the requested version, sent by the exact
   live frame, and satisfy the provider lifecycle.
4. **Every capability is independently versioned.** A camera change must not
   force unrelated apps onto a new global runtime version.
5. **Cancellation works before readiness.** Permission prompts and device
   setup are asynchronous; an app can disappear while either is pending.
6. **The contract is owner-readable.** Names, reasons, bounds, and capability
   increases are part of install/update review.
7. **No fake web platform.** Common, reusable host operations belong here.
   Full web services and unusual privileged integrations use an explicit trust
   tier rather than accumulating a large compatibility shim.

## Trust tiers

| Tier | Use | Authority |
|---|---|---|
| Ordinary mini-app | Most native Möbius apps | Opaque frame, scoped token, declared host capabilities |
| Reviewed app service | App-owned server policy, persistence, and public protocols | Accepted immutable Python entrypoint, short-lived app token, bounded JSON request/response process |
| Trusted web service | Existing full applications with cookies, origin storage, or their own backend | Separate service origin/gateway; never restored to the shell origin |
| Platform capability provider | Reusable privileged integration such as MIDI, Bluetooth, or specialist hardware | Owner-reviewed platform extension implementing this provider contract |
| Platform code | Shell behavior itself | Full shell authority; contributable and recoverable like other platform edits |

`allow-same-origin` is not a fourth app tier. On a shell-origin scripted frame
it collapses the boundary it is meant to protect. A deliberately trusted app
must receive its own origin or become an explicit platform extension.

## Credentials and authority

Opacity removes the shell's ambient **owner** authority; it does not make an
ordinary app powerless. In the current transport, the runtime receives a
refreshable app-scoped bearer and app code in that realm must be treated as able
to possess it. The bearer is narrower than the owner JWT: its app id,
installation nonce, owner epoch and installed permissions are rechecked by the
server.

Credential placement and granted authority are separate decisions. A future
host-mediated request transport could keep the raw app bearer in the exact
parent while still letting the live frame invoke every server operation its
installed permissions allow. That would reduce theft and replay outside the
frame; it would not reduce the app's approved authority while the frame is
running. A generic request transport also need not become one bespoke wrapper
per API route — server permissions remain the authorization contract.

This does not replace lifecycle-aware browser capabilities or the trust tiers
above. Origin-bound facilities such as cookies, service workers and durable
origin storage still require a host provider or a separate service origin; a
raw general shell-origin bridge would recreate the authority opacity removed.

### Named secret reads for supervised jobs

An app may declare `permissions.job_secret_read`, up to 16 unique names from
its own encrypted secret store. The accepted capability contract includes the
names in owner review. Omitted or empty means no job read access; ordinary app
frames still cannot read secret values. Only the owner-authorized supervised-job
mint includes a signed `job_secrets` claim, bounded by the accepted names at
mint time. Every read also checks the current accepted names, app installation
nonce, owner epoch and token expiry. Removing a grant denies future reads;
adding names does not expand an already-running job's token. Applying a manifest
without the grant revokes it. The existing owner/service behavior is unchanged.

This is not a process sandbox: reviewed jobs already run as trusted local code.
They must not log, persist unencrypted, or expose these values through browser
responses. A compromised permitted job could leak its own keys; denial cannot
retract a key already read. Keep provider transport and secret handling in the
app, not in new platform proxy routes. No grant implies permission to activate
bots, send messages or make paid calls.

## Manifest contract

Runtime capabilities live in the root `capabilities` object:

```json
{
  "capabilities": {
    "media.microphone.capture": {
      "version": 1,
      "reason": "Record a custom drum pad.",
      "limits": {
        "max_duration_ms": 8000
      }
    }
  }
}
```

A bounded rear-camera recording declares both time and in-memory byte ceilings:

```json
{
  "capabilities": {
    "media.camera.capture": {
      "version": 1,
      "reason": "Record a room walkthrough for a private 3D scene.",
      "limits": {
        "max_duration_ms": 180000,
        "max_bytes": 201326592
      }
    }
  }
}
```

- The key is the stable capability id.
- `version` is required and exact. The platform can host v1 and v2 together
  during a future migration without a global API-version flag.
- `reason` is concise owner-facing context, not executable policy.
- `limits` are capability-specific reviewed ceilings. A request may ask for
  less, never more.
- Unknown ids, versions, and limit fields fail installation. An install UI
  cannot truthfully review semantics its host does not know.
- Server-route permissions remain under `permissions`: they authorize HTTP
  surfaces such as filesystem or cross-app data. Both domains are normalized
  into the same install-review receipt because both increase app authority.

The installed, server-derived contract is passed to the frame. App input can
never enlarge it. Runtime revocation is therefore a contract update followed
by a frame refresh; future per-owner grant controls can narrow the installed
contract further without changing the manifest.

Apps that need server-side policy can declare one reviewed Python entrypoint:

```json
{
  "service": {
    "id": "weather-api",
    "entry": "service.py",
    "access": "self"
  },
  "source_files": ["index.jsx", "service.py"]
}
```

The platform starts a fresh process from the exact accepted app revision for
each request. It sends one JSON object on stdin and accepts one JSON response
on stdout: `{ "status": 200, "body": ..., "headers": {...} }`. The platform
owns authentication, immutable source selection, the short-lived app token,
8 MiB request/response ceilings, timeout, concurrency, and response-header
safety: only `Cache-Control`, `Content-Disposition`, `Content-Language`,
`ETag`, `Last-Modified`, and `Vary` pass, other headers are dropped, and an
authenticated response may not opt into shared caching (`public`, `s-maxage`).
Private and public requests use separate serialized lanes so a private
request can synchronously receive a public callback without deadlocking. An
agent-tool call (below) runs on a third lane that is not serialized per app, so
several can run at once alongside the two request lanes. When lanes that run at
the same time can touch the same state, the app must
provide its own file or database locking.
The app owns its paths, policy, storage format, and domain behavior. This is a
reviewed trusted process like an app job, not an operating-system sandbox.

Starting a fresh interpreter costs most services far more than their work
(roughly a second for a FastAPI entry). An entry can declare a top-level
`MOBIUS_PRELOAD = True` and end with its `if __name__ == "__main__":` block.
The platform then runs the module-level setup once per accepted revision and
forks a fresh process for each request that runs only that block, with the
request's own environment, stdio, token, deadline, and process group. The
request then exits as the interpreter would: it waits for non-daemon threads
and runs `atexit` handlers. The declaration is a promise about module-level
code. It reads no per-request value (such as `APP_TOKEN`) and no mutable app
state, which would stay frozen for the process's lifetime. It starts no
threads, opens no files, sockets, or connections that requests later use, and
sets no `os.environ` values, since each request's environment replaces them.
Do that work inside the main block or its callees. Every request shares the
setup's hash seed and any module-level random generator other than the global
`random`, which is reseeded per request. A request that no preloaded process
can take is spawned as usual.

By default a service, its preload host, and a Python job run on the platform's
interpreter and can import its libraries, which change with platform updates.
An app can instead declare its own dependencies:

```json
{
  "python": { "lock": "requirements.lock" },
  "source_files": ["index.jsx", "service.py", "requirements.lock"]
}
```

The lock is a complete `pip-compile --generate-hashes` output inside the app
source, listed in `source_files`. It must include everything the app imports,
since nothing is borrowed from the platform, and every package must have a
wheel. Apply, Store install, and Store update build a virtual environment
without system site-packages, published at `/data/app-envs/<app id>/<key>`,
where the key combines the interpreter/ABI and the lock's SHA-256. The build
installs hash-checked wheels only, so no package build code runs. pip reads no
configuration file and inherits only index, certificate, and proxy settings,
and URL credentials are removed from any diagnostics returned. The build then
runs `pip check` and a smoke run. The smoke run executes the service entry's
module-level setup (everything but its `__main__` block), or else a Python
job's top-level imports. It is the app's own code running as the backend user
with no sandbox, like the service itself, given inert `APP_*` values and
throwaway storage. Each build step runs in its own process group, killed when
the step ends or after its time limit (60 s for the smoke run). That cleanup
is best effort, since a process that calls `setsid` leaves the group, but a
build never waits past its limits. A matching environment is reused. A build
failure fails the Apply or install with pip's diagnostics, which name the
package, and the previous revision stays live. A Store install or update
builds from the fetched package before its database transaction, so it
smoke-tests the fetched service. If the owner's local edits merge into that
update, the merged service is not what was tested. A merge that changes the
lock is refused rather than built inside the transaction.

The declaring revision's service, preload host, and every job run with the
environment's `bin` first on `PATH`, so a spawned `python3` is the app's too.
The service and preload host start with the environment's interpreter, and so
does a job whose shebang is `#!/usr/bin/env [-S] pythonX[.Y]` or an absolute
Python path. A shebang that names Python in any other form is rejected when
the app is applied or installed. Other jobs keep their own interpreter.

Nothing falls back to the platform interpreter. After an image replacement
that changes the interpreter, the key no longer matches, and an accepted
revision whose manifest cannot be read is treated the same way. A revision
with no `mobius.json` at all is deliberately undeclared, because accepted
revisions may legitimately lack one. Service calls
answer 503 and the app's jobs log a failure until the background setup runner
rebuilds from the accepted lock after boot and update settlement. This may
need network; failures and explicit retries are available at `/api/setup`. Environments that no retained runtime
revision references are removed with those revisions.

Apps may also declare `"setup": {"steps": ["restore.sh"], "apt": ["foo (>= 2)"]}`.
Scripts ship through `source_files`, need a shebang, and support `check` (exit
0 ready, 1 needs apply, 2 conflict) and idempotent `apply`. The background
runner uses accepted source as cwd, runs as `mobius` after readiness/update
settlement, and combines all APT requirements. It does not gate app launches.
Instance declarations use `/data/customizations/mobius.json`. Owner-authenticated
`GET /api/setup` shows status/running steps; `POST /api/setup/rerun` cancels
the running step and starts a fresh pass. Instance scripts need no `source_files`. See the
platform-maintenance skill for setup guidance. No UI or ad-hoc install capture.

Same-app calls use `/api/apps/{app_id}/service/{path}`. An app can expose a
reviewed service to other installed apps at `/api/services/{service_id}/{path}`
by setting `access` to `apps`, or additionally expose anonymous calls at
`/api/app-services/{service_id}/{path}` by setting it to `public`. `service.id`
is the stable public contract and does not change when the app's display name,
manifest `id`, repository, or installed slug changes. These are explicit
install-time grants and do not widen the service app token's accepted
permissions. The generic routes are the whole contract: the platform does not
carry app-specific path aliases. Services receive the same `APP_ID`, `APP_SLUG`,
`APP_STORAGE_DIR`, `API_BASE_URL`, and short-lived `APP_TOKEN` environment as
other reviewed app-owned processes. The token's authority follows the caller.
An authenticated invocation's token carries the app's own authority. A public
invocation acts for an anonymous visitor, so its token has a narrow scope: it
may read the owner's identity (with the app's `identity_manage` grant) and app
list and send the owner a notification, and every other route refuses it. In
particular it cannot start or drive the owner's agents, so an anonymous visitor
cannot spend on the owner's provider accounts. A service reached only through
an authenticated caller (`self` or `apps` access) additionally receives the
provider-credential locations `DATA_DIR`, `CLAUDE_CONFIG_DIR`, and `CODEX_HOME`,
so it may run a provider CLI as its scheduled job can; a publicly reachable
(`public`) service never receives them. None of this is a filesystem sandbox:
a service is owner-installed reviewed code with the platform's file access.
Paths under `tools/` are reserved for agent tool calls; HTTP callers get 404
there.

Project output formats are app-owned too. A `project_templates[].artifact_types`
declaration names the source extensions, preview kind, output path, and reviewed
builder script. Core confines execution and publishes the resulting artifact;
it does not contain Website or LaTeX builders. The sole built-in builder is the
platform's inert app-source preview because it must use the platform compiler.

## App API

The public surface is deliberately small:

```js
const caps = window.mobius.capabilities

caps.available('media.microphone.capture', 1) // boolean
caps.describe('media.microphone.capture')     // reviewed declaration | null
caps.list()                                    // sorted declared names

const session = caps.open('media.microphone.capture', {
  maxDurationMs: 8000,
})

session.on('level', updateMeter)
await session.ready
const result = await session.finish()
// result === await session.result

session.cancel()
```

Camera recording uses the same session API:

```js
const session = caps.open('media.camera.capture', {
  facingMode: 'environment',
  maxDurationMs: 180000,
  audio: false,
})

session.on('progress', ({ durationMs, bytes }) => updateCaptureProgress({
  durationMs,
  bytes,
}))

const ready = await session.ready
// ready: { mimeType, width, height, audio }

// Optional: ask the trusted shell to paint its live preview over this app's
// viewfinder. Send again after the viewfinder moves or resizes.
const rect = viewfinder.getBoundingClientRect()
session.control('preview-rect', {
  x: rect.left, y: rect.top, width: rect.width, height: rect.height,
})

const recording = await session.finish()
// recording: { mimeType, durationMs, width, height, bytes: ArrayBuffer }
const video = new Blob([recording.bytes], { type: recording.mimeType })
```

Camera v1 accepts `environment` (the default) or `user` facing mode, a positive
`maxDurationMs`, and an `audio` boolean that defaults to `false`. The requested
duration is clamped to the reviewed manifest ceiling. The provider requests the
preferred facing camera from the browser, records through `MediaRecorder`, and
returns only the final bytes and metadata—not a `MediaStream`, track, or DOM
handle. `progress` reports the current `{durationMs, bytes}` retained in memory.
An app can send `control('preview-rect', {x, y, width, height})` to position an
optional host-owned live preview inside its canvas. The shell validates and
clips that rectangle, paints a non-interactive video layer, and removes it when
capture releases the camera. The stream itself never crosses into the opaque
app frame.
The reviewed `max_bytes` ceiling is always enforced, including the recorder's
final chunk. Manifests default to 60 seconds and 128 MiB; v1 accepts reviewed
ceilings from 100 milliseconds to 5 minutes and from 64 KiB to 256 MiB.

Every `open()` returns the same `CapabilitySession` shape:

| Member | Contract |
|---|---|
| `capability` | Stable name used to open it |
| `ready` | Resolves when the provider is usable; rejects if setup fails |
| `result` | Resolves once with the final value; rejects on failure/cancel |
| `on(event, fn)` | Subscribes to provider-defined progress events; returns unsubscribe |
| `control(action, value?)` | Sends a provider-defined control and returns `result` |
| `finish()` | Generic `control('finish')` shorthand |
| `cancel()` | Idempotently aborts and locally rejects pending promises |

One-shot providers can use:

```js
const files = await caps.invoke('files.open', { accept: ['image/*'] }, {
  signal: abortController.signal,
})
```

`invoke()` is `open()` plus `result`; it does not define a second transport.
Callbacks and functions never cross the frame boundary. Streaming/progress is
represented by named events, and binary results use structured cloning with
transferable buffers where supported.

## Wire protocol

There are five messages:

```text
frame -> host  moebius:capability-open
frame -> host  moebius:capability-control
host  -> frame moebius:capability-ready
host  -> frame moebius:capability-event
host  -> frame moebius:capability-result | moebius:capability-error
```

All carry `requestId` and `capability`. Open also carries `version` and `input`.
Control carries `action` and optional `value`. Event carries `event` and
`value`. Result carries `value`. Error carries stable `code`, DOM-style `name`,
and owner/app-readable `message`.

The host binds each request to the exact `contentWindow` that opened it. Payload
app ids are never identity. Only the visible frame may open sessions. Controls
from other frames, duplicate request ids, undeclared names, version mismatches,
and stale results are ignored or rejected without changing another session.

## Provider contract

The host registry maps a capability id to:

```js
{
  version: 1,
  exclusive: true,
  // Optional per-request policy for a capability with both read-only and
  // resource-owning operations. Receives {input, activeInput} and returns
  // 'share', 'replace', or 'reject'. It takes precedence over `exclusive`.
  contention({ input, activeInput }) { return 'reject' },
  onDeactivate: 'finish',
  preserveOnDetach: false,
  async open({ input, declaration, channel }) {
    channel.ready(metadata)
    channel.event('progress', value)
    channel.result(value, transferables)
    // or channel.error(error)
    return {
      control(action, value) { /* finish/cancel/provider controls */ }
    }
  }
}
```

The generic host owns correlation, declaration/version checks, exact-source
binding, active-frame checks, queued controls before readiness, terminal
settlement, and teardown. Providers own input validation, browser APIs, result
shape, event names, contention/exclusivity, and feature-specific cleanup. A
`replace` decision cancels and settles the matching older session before the
new request opens; use it only when the new user intent honestly supersedes the
old work. The replaced consumer receives an `AbortError` with code
`superseded`, so it can preserve a resumable checkpoint and explain the handoff
instead of presenting a generic failure.

Provider methods must be idempotent under repeated finish/cancel, release every
browser resource, and tolerate cancellation after the app frame has gone away.
They must clamp requests to reviewed declaration limits rather than trusting
app input.

`preserveOnDetach` is exceptional and platform-reviewed. It is reserved for a
background grant whose independent, owner-visible stop surface survives frame
replacement. A preserving provider receives `control('detach')`, must drop the
old frame channel without ending the grant, and must offer a narrow reattach
operation bound to the same app-owned identity. Ordinary providers keep the
default `false` and are cancelled on frame or host teardown.

## Lifecycle and grants

The initial lifecycle vocabulary is deliberately small:

- `active_frame`: the capability may run only while its app is visible. The
  provider chooses whether deactivation finishes useful partial work or
  cancels it.
- A future `background` lifecycle must be a separate reviewed capability and
  must not emerge accidentally by leaving a cached iframe alive.

Browser permission prompts remain authoritative for camera/microphone/location.
Möbius review answers a different question: *may this installed app ask?* A
future grant store may support `once`, `while installed`, and `deny` without
changing the app API. The effective grant is always the intersection of:

```text
manifest request ∩ installed review ∩ owner grant ∩ host support ∩ live context
```

For `media.camera.capture` v1, cancellation or frame teardown settles the app
session immediately even while a browser permission prompt is pending. Browsers
do not expose a way to dismiss that prompt programmatically; if it later grants
access, the provider stops the newly returned tracks without starting a
recorder. Finish before readiness fails rather than manufacturing an empty or
invalid video. Finish after readiness returns the useful partial recording.

## Stable error codes

Providers may use DOM-style names for familiar browser handling, but app logic
should branch on stable codes:

| Code | Meaning |
|---|---|
| `undeclared` | Missing from the installed contract |
| `unavailable` | Host/browser/provider cannot supply it |
| `version_mismatch` | Requested and installed/provider versions differ |
| `not_active` | Request came from a non-visible app |
| `busy` | An exclusive provider already has a live session |
| `invalid_request` | Input failed provider or transport validation |
| `denied` | Owner or browser denied access |
| `aborted` | App, host lifecycle, or teardown cancelled it |
| `limit_exceeded` | A reviewed byte ceiling was exceeded |
| `provider_error` | Unexpected provider failure |

Errors must say what the user can do next when there is an action. They must not
expose shell tokens, paths, browser internals, or another app's activity.

## Capability taxonomy

Add primitives only after a real app needs them. Likely families are:

- `media.microphone.capture`, `media.camera.capture`
- `device.storage`, `device.asset-cache`
- `files.open`, `files.save`
- `clipboard.read`, `clipboard.write`
- `location.current`, `location.watch`
- `notifications.request`, `share.open`
- `device.midi`, `device.serial`, `device.bluetooth`
- `display.fullscreen`, `display.wake_lock`

The current `workspace.screen-control` provider is intentionally narrower than
a general DOM or browser-debugging bridge. Only an app-owned support chat can
be bound to a session. The owner must start current-tab capture from a visible
app gesture, an active-only shell pill keeps Stop available after navigation,
and the provider exposes no arbitrary script execution. The session may remain
active while its app is behind another workspace view so the support agent can
investigate that view; frame teardown, capture stop, relay disconnect, expiry,
or the owner/agent Stop path releases it.

Today external HTTP remains an app-token-authenticated server surface rather
than a host session. Its reviewable permission should describe destinations and
methods; wildcard access can remain possible through explicit owner approval.
Moving credential possession into a generic host request transport would not
change that authorization model. Likewise, app storage and cross-app access are
durable server capabilities, not browser-session providers.

### Small device-local app state

`device.storage` lets an opaque mini-app remember a small JSON value in the
current browser profile without inventing an app-specific `postMessage`
protocol. The shell partitions values by both app id and installation nonce,
so uninstall/reinstall or id reuse cannot expose a previous installation's
state. A public hosted app uses the same capability messages and the same
partition identity as its signed-in view.

Declare a reviewed `max_bytes` ceiling, then use one-shot operations:

```js
await window.mobius.capabilities.invoke('device.storage', {
  operation: 'set', key: 'visitor-bookings', value: bookings,
})
const bookings = await window.mobius.capabilities.invoke('device.storage', {
  operation: 'get', key: 'visitor-bookings',
})
```

Supported operations are `get`, `set`, `remove`, and `list`. Keys are short
app-owned identifiers; values must be JSON. This storage is best-effort and
device-local: clearing site data removes it, it is not synchronized, and it is
not a substitute for `window.mobius.storage` when data belongs to the owner or
must be shared between visitors.

### Large public assets stored on one device

`device.asset-cache` lets an app install checksum-pinned public assets into
the current browser without granting the opaque app frame ambient origin
storage. It is intended for large, regenerable inputs such as an on-device ML
model, not owner-authored data or a server-side application database.

The app declares reviewed byte ceilings and supplies an exact package manifest
for each operation: a package key, public HTTPS asset URLs, total sizes, and a
SHA-256 for every bounded chunk. The shell:

- partitions Cache Storage by installed app id;
- asks for persistent browser storage only during an explicit install gesture;
- downloads bounded byte ranges through an owner-only, SSRF-safe transient
  relay when cross-origin range requests are unavailable;
- verifies every chunk before retaining it and resumes only verified chunks;
- leaves a previous complete package intact until its replacement is complete;
- prunes abandoned partial versions and enforces the reviewed total app partition;
- streams cached chunks back through transferable capability events; and
- purges the app partition on explicit app-data removal and every Möbius cache
  partition on logout.

No asset is retained on the server. Browser persistence is best-effort: an app
must state that installation is per device and browser profile, report whether
the browser granted persistent storage, and tolerate user-initiated site-data
removal. `status`, `install`, `read`, and `remove` use the ordinary capability
session API; the app remains responsible for presentation, interpretation, and
licensing of the bytes. A `read` consumer sends `control('next')` after taking
ownership of every `chunk` event, so neither message port accumulates a whole
large package in memory.

## Adding a capability

1. Add one definition (name, version, kind, copy, lifecycle, limits) to the
   canonical backend registry.
2. Implement one shell provider using the generic channel.
3. Add a standalone provider only while the standalone route remains a trusted
   top-level host. The intended end state is an installable outer shell hosting
   the same opaque frame, eliminating split semantics.
4. Add contract validation/digest tests, hostile-source broker tests, lifecycle
   cleanup tests, provider tests, and one real app journey.
5. Declare the capability in the app manifest and use
   `window.mobius.capabilities`; never probe a blocked browser API first.
6. Surface the reviewed row in install/update UI.

## Removed patterns

- No feature-specific `moebius:microphone-*` or future camera/file message
  families.
- No `window.mobius.microphone`, `window.mobius.camera`, etc. top-level sprawl.
- No browser-API probe followed by a private fallback bridge.
- No capability inferred from an app name, current screen, or payload app id.
- No raw shell JWT, cookies, DOM handles, `MediaStream`, or general shell-origin
  access passed into an ordinary app.

## Passive transcript sessions

`capabilities["chat.blocks.passive"] = {"version": 1, "reason": "Read receipts in chat."}`
opts an app into evaluation while a transcript block approaches the viewport.
Omission is no grant: unknown and legacy apps remain click-open. This is an
owner-reviewable execution capability, not a new HTTP permission. Its v1
contract requires **read-only module startup and passive session hydration**;
public actions still require explicit owner input and the existing app guards.
It is not a read-only sandbox for arbitrary code with an app-scoped bearer.

The host's `passiveAppBlockAllowed(app)` checks the accepted capability and
`AppOut.passive_block_module_digest`. That server projection requires the same
opt-in in the immutable applied runtime manifest, never editable `mobius.json`,
and a content-addressed compiled bundle. Accepting a new local Store runtime
capability does not alone admit an older served revision; apply an opted-in
package through the ordinary accepted-source path. Existing owners are never
auto-granted this capability. The frame independently requires the server's
admission and checks transferred module bytes against that exact SHA-256 before
import. Cached-frame/module mismatches fail closed, including offline; absence
of secure digest support also fails closed. An admitted module that omits
`appBlockSessions = true` is not mounted as an ordinary default component on a
passive path.

AppCanvas retains negotiation per attributed document. Its callback is
`onBlockCapability(supported, {version, reset: true})`, with `supported: null`
on a same-version document reload, and a boolean on initial mount/promotion.
Hosts invalidate stale idle/confirmation views on reset, not active or unknown
publication ownership. `blockSession.retain` pins the outgoing document while
it owns an active/uncertain operation; a ready incoming frame waits without a
loading timeout until retention clears. Promotion initializes a read session,
never replays an already delivered Confirm nonce, and ignores old-frame state.

A live `moebius:app-block-state` may include `ackNonce` for the last accepted
owner event and `retain: true` while its document owns confirmation, active
publication or an uncertain receipt. The host pins unacknowledged events and
retained owners even offscreen; idle offscreen sessions are released. Apps
acknowledge without replaying an event and release only after cancellation or
canonical settlement. These fields convey lifecycle, not publication authority.

An action with `confirming: true` supplies the complete frozen presentation as
`confirmation: [{title, facts: [{label, value}]}]`. It describes every member of
the current publication phase, not the historical transcript snapshot. The
host displays all members and rejects an incomplete/oversized presentation
without truncation: Confirm stays disabled and ordinary app review remains
available. Current limits are 256 members, 8 facts per member and 512 characters
per text field. Document reset clears stale confirmation controls, preserving
active/uncertain ownership until fresh observation releases it.
