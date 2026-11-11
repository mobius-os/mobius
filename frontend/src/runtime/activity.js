// window.mobius.onActivity — "something new happened in this app" from the shell.
//
// Möbius learns about an app's new activity (an app-attributed notification, or
// a change to the app's sidebar unread badge) the moment the server commits it,
// and the shell hears it on its live event stream. AppCanvas forwards that nudge
// to the app's frames as `moebius:app-activity`, so an open app can refresh at
// the same instant the sidebar does instead of waiting for its next poll.
//
// The nudge carries no content: it only says "check now". Nudges do not
// replay; the shell nudges every open app once when its stream reconnects.
// Apps keep their own polling as the fallback (older shells, standalone pages).
// Trust is the sender check `event.source === window.parent`, as in
// visibility.js.

export function makeActivity({ win } = {}) {
  const listeners = new Set()

  if (win && win.parent && win.parent !== win) {
    win.addEventListener('message', (e) => {
      if (e.source !== win.parent) return
      const msg = e.data
      if (!msg || msg.type !== 'moebius:app-activity') return
      for (const cb of [...listeners]) {
        try { cb() } catch (err) {}
      }
    })
  }

  return {
    // cb() fires on each nudge. Returns an unsubscribe function.
    onActivity(cb) {
      if (typeof cb !== 'function') return () => {}
      listeners.add(cb)
      return () => { listeners.delete(cb) }
    },
  }
}
