// Safari's audio session is page-wide. Opaque app frames cannot configure it
// (WebKit gates that operation on microphone policy), so the trusted host owns
// the default. Selecting a category does not start audio or unlock autoplay.
// Capture temporarily takes priority; overlapping canvases/captures share it.
const owners = new WeakMap()
const noop = () => {}

export function acquireAudioSession(type, nav = globalThis.navigator) {
  let session
  try { session = nav?.audioSession } catch { return noop }
  if (!session) return noop

  let owner = owners.get(session)
  if (!owner) {
    try {
      owner = { original: session.type, applied: session.type, playback: 0, capture: 0 }
    } catch { return noop }
    owners.set(session, owner)
  }
  const key = type === 'play-and-record' ? 'capture' : 'playback'

  function apply() {
    try {
      // Do not overwrite an explicit choice made by another audio feature.
      if (session.type !== owner.applied) owner.original = session.type
      const next = owner.capture ? 'play-and-record'
        : owner.playback && owner.original === 'auto' ? 'playback'
          : owner.original
      if (session.type !== next) session.type = next
      owner.applied = session.type
    } catch {
      // Optional browser support must not prevent app launch or recording.
    }
  }

  owner[key] += 1
  apply()
  let released = false
  return () => {
    if (released) return
    released = true
    owner[key] -= 1
    apply()
    if (!owner.capture && !owner.playback) owners.delete(session)
  }
}
