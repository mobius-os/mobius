// Workbox precaches whole RESPONSES and busts entries on a content hash, so
// headers go stale silently: identical bytes mean no refetch, and an entry
// keeps the headers it was installed with indefinitely. That is invisible for
// most assets and load-bearing for two kinds of file, both of which have
// already broken on-device speech once:
//
//   1. A worker runs under the Content-Security-Policy of its OWN response, so
//      a precached worker keeps executing under whatever policy shipped first.
//   2. A document's policy also governs the workers it spawns, so a stale
//      precached document can forbid a capability the live server allows.
//
// Workers are small and same-origin, so they are never precached. Documents
// must stay precached for offline start, so their revision carries a per-build
// stamp instead and they revalidate on every deploy.

import { SPEECH_WORKER_PATH } from '../src/lib/speech/speechWorkerAsset.js'
import { SPEECH_PITCH_WORKLET_PATH } from '../src/lib/speech/speechPitchAsset.js'

export const UNPRECACHED_WORKERS = Object.freeze([
  'sw-push.js',
  SPEECH_WORKER_PATH,
  SPEECH_PITCH_WORKLET_PATH,
])

// Marks a stamped revision. `check-offline-build.mjs` asserts documents carry
// one, so a future manifest change cannot silently re-freeze document headers.
export const DOCUMENT_REVISION_MARK = '~b'

export function buildStamp(now = Date.now()) {
  return `${DOCUMENT_REVISION_MARK}${now.toString(36)}`
}

/** Give every precached document a revision that changes on every build. */
export function stampDocumentRevisions(entries, stamp) {
  return entries.map((entry) => (entry.url.endsWith('.html')
    ? { ...entry, revision: `${entry.revision || ''}${stamp}` }
    : entry))
}
