// The memo gate for a chat pane. It lives in its own module because it is the
// load-bearing half of pane isolation: a chat pane holds a full transcript, so
// every prop Shell passes it must keep a stable identity across renders that
// have nothing to do with that chat. Anything that churns silently defeats the
// gate, and the failure is invisible — the app stays correct, just slower. As a
// plain module this is directly testable, which a comparator buried next to the
// component is not.
//
export function samePaneChatProps(previous, next) {
  for (const key of Object.keys(previous)) {
    if (!Object.is(previous[key], next[key])) return false
  }
  for (const key of Object.keys(next)) {
    if (!(key in previous)) return false
  }
  return true
}
