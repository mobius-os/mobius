/* Exact rendered-font readiness shared by the shell and isolated app frames. */
(function (scope) {
  const OWNED_FAMILIES = new Set(['Inter', 'JetBrains Mono'])
  const FRAME_CHECK_TIMEOUT_MS = 4000

  function renderedFontRequirements(doc, ownedOnly) {
    const specs = new Set()
    const requiredFamilies = new Set()
    if (!doc?.fonts || !doc.body) return { specs, requiredFamilies }

    const elements = new Set([doc.body])
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT)
    let node
    while ((node = walker.nextNode())) {
      if (node.textContent.trim() && node.parentElement) elements.add(node.parentElement)
    }

    for (const element of elements) {
      if (!element.getClientRects().length) continue
      const style = doc.defaultView.getComputedStyle(element)
      const family = style.fontFamily.split(',')[0].trim().replace(/^['"]|['"]$/g, '')
      const owned = OWNED_FAMILIES.has(family)
      if (ownedOnly && !owned) continue
      specs.add([
        style.fontStyle, style.fontWeight, style.fontSize, JSON.stringify(family),
      ].join(' '))
      if (owned) requiredFamilies.add(family)
    }
    return { specs, requiredFamilies }
  }

  async function settleRenderedFonts(doc, ownedOnly) {
    if (!doc?.fonts || !doc.body) return true
    const { specs, requiredFamilies } = renderedFontRequirements(doc, ownedOnly)

    try {
      const groups = await Promise.all([...specs].map((font) => doc.fonts.load(font)))
      await doc.fonts.ready
      const faces = groups.flat()
      return [...requiredFamilies].every((family) =>
        faces.some((face) => face.family === family && face.status === 'loaded')
      ) && faces.every((face) => face.status === 'loaded')
    } catch {
      return false
    }
  }

  function settleDocument(doc) {
    return settleRenderedFonts(doc, false)
  }

  // Production launch covers only wait on faces Möbius ships itself. External
  // theme fonts remain an owner-controlled network dependency and must never
  // hold the shell hostage; exact screenshots still use settleDocument above.
  function settleOwnedDocument(doc) {
    return settleRenderedFonts(doc, true)
  }

  function isPainted(element, view) {
    const rect = element.getBoundingClientRect()
    if (!rect.width || !rect.height
        || rect.right <= 0 || rect.bottom <= 0
        || rect.left >= view.innerWidth || rect.top >= view.innerHeight) return false
    for (let node = element; node; node = node.parentElement) {
      const style = view.getComputedStyle(node)
      if (style.display === 'none'
          || style.visibility === 'hidden'
          || Number(style.opacity) <= 0) return false
    }
    return true
  }

  function settleVisibleFrames(doc) {
    const view = doc.defaultView
    const frames = [...doc.querySelectorAll('iframe[data-app-id]')]
      .filter((frame) => isPainted(frame, view))
    if (!frames.length) return Promise.resolve(true)

    const requestId = view.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`
    return new Promise((resolve) => {
      const pending = new Set(frames.map((frame) => frame.contentWindow))
      let settled = false
      let timeout
      function finish(ready) {
        if (settled) return
        settled = true
        view.clearTimeout(timeout)
        view.removeEventListener('message', onMessage)
        resolve(ready)
      }
      function onMessage(event) {
        const message = event.data
        if (message?.type !== 'moebius:frame-font-check-result'
            || message.requestId !== requestId
            || !pending.has(event.source)) return
        if (message.ready !== true) return finish(false)
        pending.delete(event.source)
        if (!pending.size) finish(true)
      }
      view.addEventListener('message', onMessage)
      timeout = view.setTimeout(() => finish(false), FRAME_CHECK_TIMEOUT_MS)
      try {
        for (const frame of frames) {
          frame.contentWindow.postMessage({
            type: 'moebius:frame-font-check', requestId,
          }, '*')
        }
      } catch {
        finish(false)
      }
    })
  }

  async function settleCapture(doc) {
    const results = await Promise.all([settleDocument(doc), settleVisibleFrames(doc)])
    if (!results.every(Boolean)) return false
    const view = doc.defaultView
    await new Promise((resolve) => {
      view.requestAnimationFrame(() => view.requestAnimationFrame(resolve))
    })
    return true
  }

  scope.__mobiusFontReadiness = {
    settleDocument, settleOwnedDocument, settleCapture,
  }
})(globalThis)
