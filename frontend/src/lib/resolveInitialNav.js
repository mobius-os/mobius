// Resolve the shell's initial navigation on a fresh page load: the destination
// view AND whether HOME must be seeded beneath it as the back-stack root.
//
// **Why this exists (the invariant it enforces): HOME is always the root of the
// shell back-stack.** A page can boot straight into a deep destination — a
// notification deep-link (`/shell/?app=<id>`), a cold-restore of the last-viewed
// app, or a shell-reload snapshot — WITHOUT the user ever having navigated there
// in-shell. Those entries used to leave `navStackRef` empty, so once inside (and
// after a mini-app unwound its own nested-view sentinels) Back had nothing to pop
// and the handler let the browser exit the PWA; combined with the persisted
// `moebius_active_*` restore keys, the next launch/reload re-landed on the same
// app — the "keeps returning to the reflection report, can't get out" trap.
//
// Seeding HOME beneath any non-home initial destination makes Back always able to
// reach the chat surface. Reaching chat clears the canvas restore key (the
// persistence effect in useNavigation), so the loop dissolves on its own. This
// does NOT touch the drawer or an app's own back-stack: in handleBack the
// drawer-first and app-sentinel branches still run first; the seeded HOME is only
// the final fall-to once those are exhausted.
//
// Pure and side-effect-free (no globals, no React) so it is unit-testable in
// isolation; the caller parses storage/URL into the four source objects.

/**
 * @param {object}  sources
 * @param {?object} sources.shellReload  Parsed `shell-reload` snapshot: {activeView, activeAppId, activeChatId} | null
 * @param {?object} sources.deepLink     Parsed deep-link URL: {view, appId?, chatId?} | null
 * @param {?object} sources.returnView   Parsed return-view: {view:'settings'} | null
 * @param {?object} sources.restored     Parsed cold-restore: {view:'canvas', appId} | null
 * @param {?string} sources.storedChatId Last active chat id from localStorage | null
 * @returns {{view:string, appId:(number|null), chatId:(string|null), seedHome:boolean}}
 */
export function resolveInitialNav({
  shellReload = null,
  deepLink = null,
  returnView = null,
  restored = null,
  storedChatId = null,
} = {}) {
  const homeChatId = storedChatId ?? null

  // Single precedence chain — an explicit destination for THIS load wins over a
  // cold restore. (Previously activeView/activeAppId/activeChatId were resolved
  // by three separate `||` chains that could cross-contaminate sources.)
  let dest
  if (shellReload?.activeView) {
    dest = {
      view: shellReload.activeView,
      appId: shellReload.activeAppId ?? null,
      chatId: shellReload.activeChatId ?? null,
    }
  } else if (deepLink?.view) {
    dest = { view: deepLink.view, appId: deepLink.appId ?? null, chatId: deepLink.chatId ?? null }
  } else if (returnView?.view) {
    dest = { view: returnView.view, appId: null, chatId: null }
  } else if (restored?.view) {
    dest = { view: restored.view, appId: restored.appId ?? null, chatId: null }
  } else {
    dest = { view: 'chat', appId: null, chatId: null }
  }

  // Seed HOME beneath an initial destination whose view is NOT chat — i.e. a
  // canvas (mini-app: the trap case, has back-sentinels + the restore-key loop)
  // or settings. Both leave activeView !== 'chat', so popping the seeded HOME is
  // a genuine transition back to the chat surface.
  //
  // We deliberately do NOT seed under any 'chat' destination — not plain home,
  // not a shell-reload into chat, and not a deep-linked SPECIFIC chat. A chat
  // view is already the home surface (its own drawer reaches everything, it has
  // no back-sentinel, and it sets no canvas restore key, so it's never trapped),
  // and the seed would resolve to the chat you're already on — a dead Back press
  // that just delays the PWA exit. Backing out of a root chat exits the PWA,
  // which is the standard, pre-existing behavior.
  const seedHome = dest.view === 'canvas' || dest.view === 'settings'

  return {
    view: dest.view,
    appId: dest.appId,
    // Keep a chat loaded in the background (so switching to chat is instant and
    // Back-to-home has a target): the destination's own chat, else the stored one.
    chatId: dest.chatId ?? homeChatId,
    seedHome,
  }
}
