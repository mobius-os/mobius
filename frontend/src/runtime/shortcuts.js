/* Connect explicitly registered nested previews to the host's live shell command catalog. */
export function makeShortcuts({ getBindings = () => [], target = window, document = target.document } = {}) {
  const previews = new Map()
  const send = (frame, shortcuts = getBindings()) => {
    if (frame.contentWindow) frame.contentWindow.postMessage({
      type: 'moebius:frame-shortcuts', shortcuts,
    }, '*')
  }
  const onMessage = (event) => {
    // The host updates its catalog before this runtime listener runs. Its
    // original source/origin gate and the final AppCanvas gate stay intact.
    if (event.source === target.parent && event.origin === target.location.origin
        && event.data?.type === 'moebius:frame-shortcuts') {
      previews.forEach((_, frame) => { if (frame.isConnected) send(frame) })
      return
    }
    const frame = [...previews.keys()].find(candidate => (
      candidate.isConnected && candidate.contentWindow === event.source
    ))
    if (!frame) return
    if (event.data?.type === 'moebius:frame-shortcuts-ready') send(frame)
    if (event.data?.type !== 'moebius:shell-shortcut') return
    const actionId = event.data.actionId
    if (!getBindings().some(item => item.actionId === actionId)) return
    target.parent.postMessage({ type: 'moebius:shell-shortcut', actionId }, target.location.origin)
  }
  target.addEventListener('message', onMessage)
  return Object.freeze({
    connect(frame) {
      if (!frame || frame.tagName !== 'IFRAME' || frame.ownerDocument !== document) return () => {}
      if (previews.has(frame)) return previews.get(frame)
      const onLoad = () => { if (frame.isConnected) send(frame) }
      const disconnect = () => {
        if (previews.get(frame) !== disconnect) return
        send(frame, [])
        previews.delete(frame)
        frame.removeEventListener('load', onLoad)
      }
      previews.set(frame, disconnect)
      frame.addEventListener('load', onLoad)
      if (frame.isConnected) send(frame)
      return disconnect
    },
    _destroy() {
      target.removeEventListener('message', onMessage)
      for (const disconnect of [...previews.values()]) disconnect()
    },
  })
}
