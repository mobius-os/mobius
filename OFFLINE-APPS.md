# Offline mini-apps

Offline support is an app-level product choice, not a requirement for every
Möbius app. This document defines the shared platform/app contract for apps
that choose to support it. The implementation map remains in
[`ARCHITECTURE.md`](ARCHITECTURE.md); practical instructions live in the seeded
[`building-apps`](backend/scripts/seed-skills/building-apps.md) and
[`building-apps-quickstart`](backend/scripts/seed-skills/building-apps-quickstart.md)
guides.

## Ownership

Möbius supplies generic transport and lifecycle primitives:

- an isolated read-through cache and durable write queue per installed app;
- read-your-writes behavior for ordinary reads;
- connectivity state and automatic queue drain after reconnect;
- complete/incomplete directory-listing status;
- authoritative online value/version pairs for conditional writes;
- bounded mutation-intent retention and conflict replay; and
- workspace and standalone loading/error boundaries.

The app owns its data model and product policy:

- which data must be warmed before offline use;
- whether partial data is safe to display or act on;
- how domain changes merge after concurrent edits;
- loading, skeleton, offline, conflict, and recovery UI; and
- whether offline use is valuable enough to promise at all.

Keep domain-specific merge rules in the app. Do not add app-specific sync logic
to the platform, and do not create a second app-owned cache or queue beside
`window.mobius.storage`.

## Surfaces and the manifest promise

Every ordinary mini-app runs in the same opaque `AppCanvas` frame in the
workspace and under `/apps/<slug>/`. After an online open, the shell can reuse
the app frame and self-contained module without the network. This universal
code warm-up is separate from the app's product-level offline promise.

Set `offline_capable: true` in `mobius.json` only when the app's required data,
reads, writes, and recovery behavior work through a cold offline reload. The
flag enables the real standalone app to open offline. An app may intentionally
remain online-only; the standalone host then shows branded offline fallback
chrome instead of claiming that the app works or dropping to the browser's
native network-error page.

Packaged nested documents under `/app-embeds/` do not inherit a recursive
static-asset offline guarantee. Their subresources need an explicit future
warm contract; until then, keep an app that depends on them online-only.

## Read models

Use `window.mobius.storage`, not raw storage `fetch` calls. The two read modes
serve different purposes:

- `get()`, `getText()`, `getBlob()`, and subscriptions are product reads. They
  may return the last cached server value with queued local writes overlaid.
  This provides fast reads and read-your-writes behavior.
- `getWithVersion()` is a merge-base read. While online, it returns the
  authoritative server value and its matching version, bypassing both queued
  overlays and a stale ordinary-read HTTP cache. While offline it returns the
  best cached value/version with `offline: true`, which is useful for queuing a
  conditional write but is not proof of current server state.

Conflict recovery that depends on the online guarantee must require
`window.mobius.runtimeFeatures.authoritativeVersionedReads === true`. On an
older runtime, leave the conflict unhandled rather than merging against an
ambiguous value/version pair.

Views that the agent, a job, or another frame can update must subscribe to the
relevant path instead of loading only on mount.

## Collection completeness

Use `storage.listWithStatus(prefix, { includeContent: true })` when behavior
depends on complete membership.

- `complete: false` means partial or unavailable knowledge. It is never proof
  that the collection is empty. Preserve the last authoritative UI state and
  do not seed defaults, delete records, or rewrite an index from it.
- `complete: true` proves membership, not body availability. Content can be
  omitted by byte limits or be absent from the offline cache. If an operation
  needs every body, fetch each missing body and stop safely if any remain
  unavailable.
- `list()` is an entries-only best-known view. Use it for non-authoritative
  display, not cleanup or reconciliation.

The runtime option is `includeContent: true`; the equivalent raw HTTP query is
`include_content=true`.

## Writes and conflicts

Prefer one file per independently edited record. Ordinary `set()` writes are
last-write-wins per path and are appropriate when that is the intended policy.
Use compare-and-swap only for a document that genuinely has multiple writers.

For a conditional write:

1. Read with `getWithVersion()`.
2. Apply the app-owned change to that value.
3. Call `durableWrite()` with `ifMatch: version`, or `ifNoneMatch: true` for a
   create-only write.
4. If the write conflicts, re-read the authoritative value, merge, and retry
   with a finite policy owned by the app.

An offline conditional write should include a small `conflictContext` that
describes mutation intent, not only the resulting whole document. When several
writes to one path coalesce, the runtime retains those opaque intents in order.
Recover with `storage.conflictContextItems()` so every retained intent is
applied.

```js
const { storage } = window.mobius
const canRecover =
  window.mobius.runtimeFeatures?.authoritativeVersionedReads === true

if (canRecover) {
  storage.onConflict(async (conflict) => {
    const current = await storage.getWithVersion(conflict.path)
    if (current.offline) return false

    const intents = storage.conflictContextItems(conflict.conflictContext)
    const merged = applyIntents(current.value, intents)
    const guard = current.version
      ? { ifMatch: current.version }
      : { ifNoneMatch: true }
    try {
      const result = await storage.durableWrite(conflict.path, merged, guard)
      return result?.durability === 'synced'
    } catch (error) {
      if (error?.code === 'conflict') return false
      throw error
    }
  })
}
```

The callback's result is a durability decision: truthy retires the original
conflict, while `false` preserves it for replay, including after an app-frame
remount. A result with `durability: "queued"` is not server acceptance. Do not
acknowledge it, and do not use a separate `pendingCount()` check as proof: a
different frame can enqueue between that check and a later read. The
authoritative versioned read is the merge boundary.

Keep conflict contexts bounded and deterministic. If applying the same intent
twice would duplicate or corrupt data, make the intent idempotent or record a
stable operation id in the app document.

## Loading and offline UI

The platform owns only the outer host state before the app frame is ready. Once
app code renders, the app owns meaningful loading and recovery states. Keep an
existing board, note, or collection visible while background refresh runs;
replace it only with a complete accepted result. Use one latest-request guard
when startup and reconnect refreshes can overlap so an older response cannot
replace newer state.

Show offline state only when it helps the owner understand availability or a
restricted action. A skeleton should match the final layout closely, respect
reduced motion, and remain mounted until the corresponding data state is ready.

## Verification matrix

Test behavior, not only the manifest flag or source text:

1. Open online and warm every required code and data path.
2. Go fully offline, including page, frame, and service-worker network paths.
3. Reload the whole workspace and the standalone route.
4. Verify complete cached views remain intact and incomplete reads do not
   erase prior state.
5. Create offline writes, reload again, and verify they remain visible and
   queued.
6. Reconnect and wait for actual server synchronization.
7. Introduce a disjoint remote edit and verify conflict recovery preserves
   both the remote change and every retained local intent.
8. Repeat conflict delivery after a frame remount and under reversed async
   completion order.

Run the matrix for each surface the app claims to support. Browser automation
can verify workspace and standalone documents, frames, and service-worker
behavior. Actual OS-installed-PWA launch and device installation UI remain a
separate device-only boundary and must not be implied by a browser pass.
