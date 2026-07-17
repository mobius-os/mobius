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
  onDeactivate: 'finish',
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
shape, event names, exclusivity, and feature-specific cleanup.

Provider methods must be idempotent under repeated finish/cancel, release every
browser resource, and tolerate cancellation after the app frame has gone away.
They must clamp requests to reviewed declaration limits rather than trusting
app input.

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
| `provider_error` | Unexpected provider failure |

Errors must say what the user can do next when there is an action. They must not
expose shell tokens, paths, browser internals, or another app's activity.

## Capability taxonomy

Add primitives only after a real app needs them. Likely families are:

- `media.microphone.capture`, `media.camera.capture`
- `files.open`, `files.save`
- `clipboard.read`, `clipboard.write`
- `location.current`, `location.watch`
- `notifications.request`, `share.open`
- `device.midi`, `device.serial`, `device.bluetooth`
- `display.fullscreen`, `display.wake_lock`

External HTTP remains an app-token authenticated server surface rather than a
host session. Its reviewable permission should describe destinations and
methods; wildcard access can remain possible through explicit owner approval.
Likewise, app storage and cross-app access are durable server capabilities, not
browser-session providers.

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
