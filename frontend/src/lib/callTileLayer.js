/**
 * Host-painted video tiles for `media.call`.
 *
 * Tiles are painted over the live app frame at rectangles the app gives in its
 * own CSS pixels, inside a non-interactive layer that covers exactly the frame,
 * so they move and clip with it. The app never receives these elements or
 * their streams. Updates restyle elements in place, so tiles can move every
 * animation frame. `fit` is `cover` for a camera and `contain` for a screen.
 */
export function createCallTileLayer({ getContainer }) {
  let painted = new Map()

  function release(entry) {
    entry.video.srcObject = null
    entry.element.remove()
  }

  function create(container) {
    const document = container.ownerDocument
    const element = document.createElement('div')
    element.className = 'canvas-call-tile'
    Object.assign(element.style, { position: 'absolute', overflow: 'hidden', pointerEvents: 'none' })
    const video = document.createElement('video')
    // Audio plays through the call's Web Audio graph, never the tile.
    video.muted = true
    video.autoplay = true
    video.playsInline = true
    video.setAttribute('playsinline', '')
    Object.assign(video.style, { display: 'block', width: '100%', height: '100%' })
    element.appendChild(video)
    container.appendChild(element)
    return { element, video, stream: null }
  }

  return {
    // `tiles` is the complete list, each with the shell-owned `stream` to show.
    paint(tiles) {
      const container = getContainer?.() || null
      const next = new Map()
      const seen = new Map()
      for (const [order, tile] of (container ? tiles : []).entries()) {
        // The same person may be painted twice (say, a strip and a stage).
        const count = seen.get(tile.peer) || 0
        seen.set(tile.peer, count + 1)
        const key = `${tile.peer}#${count}`
        let entry = painted.get(key)
        if (entry?.element.parentNode !== container) entry = create(container)
        Object.assign(entry.element.style, {
          left: `${tile.x}px`,
          top: `${tile.y}px`,
          width: `${tile.width}px`,
          height: `${tile.height}px`,
          borderRadius: `${tile.radius}px`,
          opacity: String(tile.opacity),
          // Later tiles in the app's list paint above earlier ones.
          zIndex: String(order + 1),
        })
        entry.video.style.objectFit = tile.fit
        entry.video.style.transform = tile.mirror ? 'scaleX(-1)' : ''
        if (entry.stream !== tile.stream) {
          entry.stream = tile.stream
          entry.video.srcObject = tile.stream
          entry.video.play?.()?.catch?.(() => {})
        }
        next.set(key, entry)
      }
      for (const [key, entry] of painted) if (next.get(key) !== entry) release(entry)
      painted = next
    },
    destroy() {
      for (const entry of painted.values()) release(entry)
      painted = new Map()
    },
  }
}
