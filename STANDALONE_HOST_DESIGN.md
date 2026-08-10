# Standalone mini-app host

## Decision

An installed mini-app keeps its stable `/apps/<slug>/` URL, web-app manifest,
icon, PWA scope, offline navigation cache, and full-window presentation. The
URL is a **trusted platform host**, not an alternate mini-app runtime.

Every mini-app executes through the same `AppCanvas` opaque-frame boundary,
whether it was opened inside the workspace or from its own home-screen icon.

## Threat model and invariant

Mini-app source is editable and may be generated or installed. It must be
treated as less trusted than the owner-authenticated platform document.

The owner JWT and owner-origin browser state may be available to signed Möbius
frontend code. They must never be available to app-authored JavaScript.

The enforceable invariant is:

> No response under `/apps/<slug>/` imports, evaluates, or embeds app-authored
> JavaScript. It may contain non-secret app identity as inert JSON. The app
> module executes only inside `/api/apps/<id>/frame`, whose response CSP applies
> a sandbox without `allow-same-origin`.

The frame receives a short-lived app-scoped token through AppCanvas's
exact-window-attributed handshake. It does not receive the owner JWT.

The runtime deliberately has no top-level capability provider or standalone
navigation fallback. If app code is ever executed without AppCanvas, those
operations report `unavailable` rather than silently recreating a privileged
second host.

## Request path

1. `routes/standalone.py` resolves a live app row by slug.
2. It reads the same complete frontend build selected by the main shell.
3. It rewrites PWA identity metadata and injects
   `#__mobius-standalone-app__`, an `application/json` slot containing only
   non-secret app metadata.
4. `App.jsx` validates that slot and selects `StandaloneApp` after the ordinary
   setup/login boundary.
5. `StandaloneApp` renders `AppCanvas` with the app id, executable revision,
   offline flag, and reviewed capability contract.
6. `AppCanvas` mints/refreshes the app token, loads the opaque frame, brokers
   executable bytes, storage and capabilities, and attributes every frame
   message to an exact mounted `contentWindow`.

## Host requests

`AppCanvas` owns source attribution. It narrows the four reviewed requests
which may leave a mini-app (`new-chat`, `open-chat`, `open-app`, and
`open-settings`) before delivering them through `onHostRequest`.

The workspace retains its richer navigation behavior. The standalone host
uses the same request contract to hand off to `/shell/`. It also owns a bounded
top-level history bridge for nested app views, so device Back/Forward continues
to drive the frame's `mobius:nav-*` protocol.

## Preserved standalone behavior

- stable manifest identity, scope, icon and display mode;
- install prompt plus platform-specific manual instructions;
- optional icon customization before installation;
- app-specific loading identity;
- offline navigation caching only when `offline_capable` is true;
- app-scoped storage and capability behavior through AppCanvas;
- update notification with an explicit user-applied live swap;
- selectable, credential-redacted failures and report-to-owning-chat recovery;
- a quiet handoff back to the full Möbius workspace.

## Cache migration

The old `mobius-standalone-v2` cache may contain HTML that directly imported an
app module at owner origin. The secure host uses `mobius-standalone-v3`.
Service-worker activation classifies v2 as stale and deletes it. This is a
security migration, not a routine cache-name bump.

## Failure and rollback boundaries

- If the editable frontend build is incomplete, both shell and standalone
  routes select the baked recovery floor through one resolver.
- If the signed index template loses a required boot seam, standalone returns
  a retryable 503 rather than falling back to direct app execution.
- A missing/deleted app remains a 404 at the backend route; a deletion observed
  after launch becomes an explicit recovery handoff in `StandaloneApp`.
- Backend Python is syntax-checked before restart. Platform changes are
  committed as exact paths and remain repairable through an external root
  attachment.

## Verification contract

Completion requires evidence at each boundary:

1. Backend tests prove the PWA response contains validated boot JSON but no
   direct module bootstrap, while the selected frame CSP remains opaque.
2. Frontend unit tests prove boot validation, host-request narrowing,
   diagnostic redaction, and stale v2 cache eviction.
3. The frontend production build proves the lazy standalone host and AppCanvas
   graph compile together.
4. Rendered smoke tests inspect both `/apps/<slug>/` and the normal workspace
   route, including the actual iframe origin/sandbox behavior.
5. Device-only follow-up covers OS installation UI and cold offline relaunch,
   which headless browser testing cannot fully reproduce.
