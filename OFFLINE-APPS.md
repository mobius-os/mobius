# Offline mini-apps

Offline support is an app-level product choice, not a requirement for every
Möbius app. For apps that choose it, this is the canonical platform/app
contract. [`ARCHITECTURE.md`](ARCHITECTURE.md) explains the underlying platform
implementation; the seeded
[`building-apps`](backend/scripts/seed-skills/building-apps.md) and
[`building-apps-quickstart`](backend/scripts/seed-skills/building-apps-quickstart.md)
guides turn the contract into build steps.

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

## Storage invariants

The exact storage API recipes live in the seeded
[`building-apps`](backend/scripts/seed-skills/building-apps.md) guide. An app
that promises offline behavior must preserve these boundaries:

- use `window.mobius.storage`, not raw storage `fetch` calls, so ordinary reads
  can overlay queued writes and retain read-your-writes behavior;
- use `getWithVersion()` only as an authoritative merge base when
  `runtimeFeatures.authoritativeVersionedReads === true` and its result is not
  marked `offline`;
- treat `listWithStatus().complete === false` as partial or unavailable
  knowledge, never proof that a collection is empty; and
- subscribe to paths that the agent, a job, or another frame can update.

A complete listing proves membership, not that every record body is available.
Preserve the last authoritative UI state and stop safely when a required body
cannot be loaded.

## Conflict recovery

Prefer one file per independently edited record; use compare-and-swap only for
a document that genuinely has multiple writers. The general CAS mechanics are
covered in `building-apps.md`. An offline conditional write adds one requirement:
include a small `conflictContext` that describes mutation intent, not only the
resulting whole document. When several writes to one path coalesce, the runtime
retains those opaque intents in order.

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
      const result = await storage.durableWrite(conflict.path, merged, {
        ...guard,
        conflictContext: conflict.conflictContext,
      })
      return result?.durability === 'synced'
    } catch (error) {
      if (error?.code === 'conflict') return false
      throw error
    }
  })
}
```

The callback's result is a durability decision: truthy retires the original
conflict, while `false` preserves it. A preserved conflict is replayed when an
`onConflict` listener registers again, normally on the next app-frame load or
remount; reconnect alone does not redispatch it. A result with
`durability: "queued"` is not server acceptance. Do not acknowledge it, and do
not use a separate `pendingCount()` check as proof: a different frame can
enqueue between that check and a later read. The authoritative versioned read
is the merge boundary.

Carry the original `conflictContext` onto the recovery write. Connectivity can
drop between the authoritative read and that write; if the queued recovery
later conflicts, the app still needs the retained intent to recover safely.

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
