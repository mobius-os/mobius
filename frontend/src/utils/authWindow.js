// Safari and Firefox only honour window.open() inside the user gesture that
// triggered it, so an auth URL fetched asynchronously can never open its own
// tab. Reserve the tab synchronously on the click, then point it at the URL
// once it arrives. The caller owns the handle and must close it on every path
// that never navigates, or the owner is left with a blank tab.

/** The live value of a Möbius theme token on this page, for painting a blank
 *  window Möbius opens (which has no theme of its own); '' when unavailable. */
export function openerThemeToken(name) {
  try {
    return window.getComputedStyle(window.document.documentElement).getPropertyValue(name).trim()
  } catch {
    return ''
  }
}

export function reserveAuthWindow(title = 'Opening sign-in...') {
  if (typeof window === 'undefined' || typeof window.open !== 'function') {
    return null
  }
  let authWindow = null
  try {
    authWindow = window.open('', '_blank')
    if (!authWindow) return null
    try {
      authWindow.document.title = title
      // The reserved tab is a blank page outside Möbius, so it borrows the
      // opener's live theme tokens instead of fixed colours.
      authWindow.document.body.style.margin = '0'
      authWindow.document.body.style.fontFamily = openerThemeToken('--font')
      authWindow.document.body.style.background = openerThemeToken('--bg')
      authWindow.document.body.style.color = openerThemeToken('--text')
      authWindow.document.body.innerHTML = '<main style="min-height:100vh;display:grid;place-items:center;padding:24px;box-sizing:border-box;text-align:center"><div><h1 style="font-size:20px;margin:0 0 8px">Opening sign-in...</h1><p style="margin:0;opacity:.72">You can come back to Möbius when this is done.</p></div></main>'
    } catch {
      // Some browsers block writes to the temporary page; the window itself is enough.
    }
  } catch {
    return null
  }
  return authWindow
}

export function navigateAuthWindow(authWindow, url) {
  if (!authWindow || authWindow.closed || !url) return false
  try {
    // Sever the opener only now, as the tab is handed to a cross-origin
    // sign-in page that could otherwise navigate us (reverse tabnabbing).
    // Doing it at reserve time instead would risk making the tab
    // non-script-closable while it is still ours to clean up.
    authWindow.opener = null
    authWindow.location.replace(url)
    return true
  } catch {
    return false
  }
}

export function closeAuthWindow(authWindow) {
  if (!authWindow || authWindow.closed) return
  try {
    authWindow.close()
  } catch {
    // Best effort only.
  }
}
