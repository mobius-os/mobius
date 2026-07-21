const INIT = 'moebius:chat-embed:init'

let queuedInit = null
let listening = false

function captureInit(event) {
  if (event.origin !== 'null' && event.origin !== window.location.origin) return
  if (event.source !== window.parent) return
  if (event.data?.type !== INIT) return
  queuedInit = event
}

// Install the tiny receiver from the shared entry bundle, before React asks
// for the lazy ChatEmbed chunk. This preserves compatibility with older app
// runtimes that send INIT on document load.
export function beginEmbedBootstrap() {
  if (listening || typeof window === 'undefined' || window.parent === window) return
  window.addEventListener('message', captureInit)
  listening = true
}

// ChatEmbed installs its full receiver first, then atomically takes over and
// drains the one pre-mount INIT (if an older runtime already sent it).
export function handoffEmbedBootstrap(receiver) {
  if (listening) window.removeEventListener('message', captureInit)
  listening = false
  const event = queuedInit
  queuedInit = null
  if (event && typeof receiver === 'function') receiver(event)
}
