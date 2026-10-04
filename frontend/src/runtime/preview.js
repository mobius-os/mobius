// Authored HTML previews share the app's shell shortcuts, not its credentials.
// Keep this installer self-contained: it also runs in the opaque child document.
export function installPreviewShortcuts(target) {
  let shortcuts = []
  target.addEventListener('message', (event) => {
    if (event.source !== target.parent || event.data?.type !== 'moebius:frame-shortcuts') return
    shortcuts = Array.isArray(event.data.shortcuts) ? event.data.shortcuts : []
  })
  target.document.addEventListener('keydown', (event) => {
    if (event.isComposing || event.repeat) return
    const shortcut = shortcuts.find((item) => {
      const binding = item?.binding
      return binding && String(event.key || '').toLowerCase() === String(binding.key || '').toLowerCase()
        && Boolean(event.metaKey || event.ctrlKey) === Boolean(binding.mod)
        && Boolean(event.shiftKey) === Boolean(binding.shift)
        && Boolean(event.altKey) === Boolean(binding.alt)
    })
    if (!shortcut) return
    event.preventDefault()
    event.stopImmediatePropagation()
    target.parent.postMessage({ type: 'moebius:shell-shortcut', actionId: shortcut.actionId }, '*')
  }, true)
  target.parent.postMessage({ type: 'moebius:frame-shortcuts-ready' }, '*')
}

export function preparePreviewDocument(html) {
  const source = `(${installPreviewShortcuts.toString()})(window)`.replace(/<\/script/gi, '<\\/script')
  const script = `<script>${source}</script>`
  const document = String(html ?? '')
  // Keep standards mode, but install before authored scripts can claim keys.
  const doctype = /^((?:\s|<!--[\s\S]*?-->)*<!doctype[^>]*>)/i.exec(document)
  return doctype
    ? `${doctype[1]}${script}${document.slice(doctype[1].length)}`
    : `${script}${document}`
}

// Bind once with the app's React, just like createUseDocument. The runtime
// stays React-free. A stable srcDoc avoids hidden joint-history navigations;
// live catalog changes travel through the existing bridge instead.
export function createPreviewFrame(shortcuts, React) {
  return React.forwardRef(function PreviewFrame({ srcDoc, sandbox = 'allow-scripts', ...props }, ref) {
    const html = React.useMemo(() => preparePreviewDocument(srcDoc), [srcDoc])
    const disconnect = React.useRef(null)
    const register = React.useCallback((frame) => {
      disconnect.current?.()
      disconnect.current = frame ? shortcuts.connect(frame) : null
      if (typeof ref === 'function') ref(frame)
      else if (ref) ref.current = frame
    }, [ref])
    return React.createElement('iframe', { ...props, sandbox, srcDoc: html, ref: register })
  })
}
