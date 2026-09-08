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
