// Route a Web Audio graph through a real HTMLAudioElement.
//
// A bare `AudioContext` → `destination` graph is not reliably placed on the OS
// "media" stream on mobile: iOS/Android then leave the hardware volume buttons
// controlling the ringer instead of the speech, so users cannot change its
// volume, and lock-screen / Media Session controls never appear. Streaming the
// output through a MediaStreamDestination attached to an autoplaying <audio>
// element gives the browser a genuine media element to manage, which fixes all
// three. Mirrors the per-app bridge the News app already ships.
//
// Returns null (caller falls back to `context.destination`) when the browser
// lacks the APIs or no document is available.
export function createSpeechMediaBridge({ context, doc = globalThis.document } = {}) {
  if (!context?.createMediaStreamDestination || !doc?.createElement) return null
  const element = doc.createElement('audio')
  if (!('srcObject' in element)) return null

  const destination = context.createMediaStreamDestination()
  element.autoplay = true
  element.playsInline = true
  element.preload = 'none'
  element.setAttribute('aria-hidden', 'true')
  element.style.display = 'none'
  element.srcObject = destination.stream
  ;(doc.body || doc.documentElement)?.appendChild(element)

  // Safari 17+ can classify Web Audio as long-form playback; other engines omit
  // AudioSession or may reject an unknown type. Feature-detect and ignore.
  try { if (globalThis.navigator?.audioSession) globalThis.navigator.audioSession.type = 'playback' } catch {}

  let disposed = false
  return {
    destination,
    async start() {
      if (disposed) return
      try { await element.play() } catch { /* autoplay may be deferred; audio still routes */ }
    },
    dispose() {
      if (disposed) return
      disposed = true
      try { element.pause() } catch {}
      element.srcObject = null
      for (const track of destination.stream?.getTracks?.() || []) {
        try { track.stop() } catch {}
      }
      try { element.remove() } catch {}
      try { if (globalThis.navigator?.audioSession) globalThis.navigator.audioSession.type = 'auto' } catch {}
    },
  }
}
