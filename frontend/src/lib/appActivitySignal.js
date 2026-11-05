// In-shell fan-out of "this app has new activity" from the live system event
// stream to that app's mounted canvases, which forward it into the frame as
// `moebius:app-activity` (window.mobius.onActivity). Keyed by app id; a nudge
// for an app with no mounted canvas is simply dropped (the app loads fresh
// state when it opens).
const listeners = new Map() // appId(string) -> Set<cb>

export function publishAppActivity(appId) {
  const set = listeners.get(String(appId))
  if (!set) return
  for (const cb of [...set]) {
    try { cb() } catch {}
  }
}

export function subscribeAppActivity(appId, cb) {
  const key = String(appId)
  let set = listeners.get(key)
  if (!set) { set = new Set(); listeners.set(key, set) }
  set.add(cb)
  return () => {
    set.delete(cb)
    if (set.size === 0 && listeners.get(key) === set) listeners.delete(key)
  }
}
