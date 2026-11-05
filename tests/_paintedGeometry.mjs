/**
 * Chat scroll geometry as the browser actually paints it.
 *
 * Streamed output commits in a task between frames, and the scroll owner
 * repairs FOLLOW/PIN positions in the next frame's ResizeObserver pass, before
 * paint. A plain `page.evaluate` read can land between those two points and
 * force a layout of geometry that never paints (for example, a live follower
 * several lines short of the tail). This samples right after a painted frame
 * and skips any sample whose transcript DOM changed since that frame, the
 * same rule settled-transcript-handoff.spec.mjs uses for its frame sampler.
 */
export async function paintedScrollGeometry(page, { timeout = 5000 } = {}) {
  return page.evaluate(timeoutMs => new Promise((resolve, reject) => {
    const surface = document.querySelector('[data-chat-surface="painted"]')
    const scroll = surface?.querySelector('.chat__scroll')
    if (!scroll) {
      reject(new Error('painted chat scroll element is missing'))
      return
    }
    let committedSinceFrame = false
    const observer = new MutationObserver(() => { committedSinceFrame = true })
    observer.observe(scroll, { childList: true, characterData: true, subtree: true })
    const deadline = setTimeout(() => {
      observer.disconnect()
      reject(new Error(`no unchanged painted frame within ${timeoutMs}ms`))
    }, timeoutMs)
    const sample = () => {
      if (committedSinceFrame) {
        schedule()
        return
      }
      clearTimeout(deadline)
      observer.disconnect()
      const users = surface.querySelectorAll('.chat__msg--user')
      const lastUser = users[users.length - 1]
      const spacerH = surface.querySelector('.spacer-dynamic')?.offsetHeight || 0
      const sr = scroll.getBoundingClientRect()
      const ur = lastUser?.getBoundingClientRect()
      resolve({
        scrollTop: Math.round(scroll.scrollTop),
        scrollH: scroll.scrollHeight,
        clientH: scroll.clientHeight,
        spacerH,
        mode: scroll.dataset.scrollMode || null,
        lastUserVisualTop: ur ? Math.round(ur.top - sr.top) : null,
        // Distance from the real-content tail to the viewport bottom.
        realContentGap: Math.round(
          scroll.scrollHeight - spacerH - scroll.scrollTop - scroll.clientHeight,
        ),
      })
    }
    const schedule = () => requestAnimationFrame(() => {
      committedSinceFrame = false
      setTimeout(sample, 0)
    })
    schedule()
  }), timeout)
}
