const HOST_COPY_TIMEOUT_MS = 1200

function execCopy(value) {
  try {
    const previousActive = document.activeElement
    const ta = document.createElement('textarea')
    ta.value = value
    ta.setAttribute('aria-hidden', 'true')
    // iOS/WebKit ignores execCommand('copy') from hidden, readonly, or
    // off-screen fields. Keep it editable and on-screen but visually inert:
    // 16px avoids the focus-zoom, opacity:0 keeps it invisible, 1px hides it.
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;'
      + 'padding:0;border:0;margin:0;opacity:0;font-size:16px;'
    document.body.appendChild(ta)
    let copied = false
    try {
      ta.focus()
      ta.select()
      if (typeof ta.setSelectionRange === 'function') {
        ta.setSelectionRange(0, value.length)
      }
      copied = Boolean(document.execCommand('copy'))
    } catch {
      copied = false
    }
    try { ta.remove() } catch { /* cleanup is best-effort */ }
    try {
      if (previousActive && typeof previousActive.focus === 'function') {
        previousActive.focus()
      }
    } catch { /* restoring focus must not change a successful copy result */ }
    return copied
  } catch {
    return false
  }
}

async function asyncCopy(value) {
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      await navigator.clipboard.writeText(value)
      return true
    }
  } catch { /* unavailable or denied */ }
  return false
}

// Used by the trusted AppCanvas host after it has attributed a request to the
// exact mounted frame. The top-level document can write on engines that deny
// clipboard access to an otherwise delegated opaque frame.
export async function writeClipboardText(value) {
  return execCopy(value) || asyncCopy(value)
}

function hostCopy(value) {
  if (typeof window === 'undefined' || !window.parent || window.parent === window) {
    return Promise.resolve(null)
  }
  const requestId = globalThis.crypto?.randomUUID?.()
    || `clipboard-${Date.now()}-${Math.random().toString(36).slice(2)}`
  return new Promise((resolve) => {
    let settled = false
    const finish = (result) => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      window.removeEventListener('message', onMessage)
      resolve(result)
    }
    const onMessage = (event) => {
      if (event.source !== window.parent || event.origin !== window.location.origin) return
      const message = event.data
      if (message?.type !== 'moebius:clipboard-write-result'
          || message.requestId !== requestId) return
      finish(message.ok === true)
    }
    const timeout = setTimeout(() => finish(null), HOST_COPY_TIMEOUT_MS)
    window.addEventListener('message', onMessage)
    try {
      window.parent.postMessage({
        type: 'moebius:clipboard-write', requestId, text: value,
      }, window.location.origin)
    } catch {
      finish(null)
    }
  })
}

export function makeClipboard() {
  async function writeText(text) {
    const value = text == null ? '' : String(text)
    if (!value) return false
    // Keep the synchronous attempt inside the original tap. If the opaque
    // frame cannot write, let its trusted host try before awaiting the frame's
    // async API—the latter can consume iOS's transient activation on denial.
    if (execCopy(value)) return true
    const hostResult = await hostCopy(value)
    if (hostResult === true) return true
    return asyncCopy(value)
  }
  return { writeText }
}
