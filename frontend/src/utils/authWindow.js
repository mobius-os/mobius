// Safari and Firefox only honour window.open() inside the user gesture that
// triggered it, so an auth URL fetched asynchronously can never open its own
// tab. Reserve the tab synchronously on the click, then point it at the URL
// once it arrives. The caller owns the handle and must close it on every path
// that never navigates, or the owner is left with a blank tab.

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
      authWindow.document.body.style.margin = '0'
      authWindow.document.body.style.fontFamily = 'system-ui, sans-serif'
      authWindow.document.body.style.background = '#0d0d0d'
      authWindow.document.body.style.color = '#d4d4d8'
      authWindow.document.body.innerHTML = '<main style="min-height:100vh;display:grid;place-items:center;padding:24px;box-sizing:border-box;text-align:center"><div><h1 style="font-size:20px;margin:0 0 8px">Opening sign-in...</h1><p style="margin:0;color:#a1a1aa">You can come back to Möbius when this is done.</p></div></main>'
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
