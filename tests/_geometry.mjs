/** Shared layout-geometry helpers for browser specs. */

/** Measure an element only once its geometry has stopped moving.
 *
 *  Tab strips reflow after the panes are up: a tab is laid out at an empty
 *  ~36px width and grows to its full ~120px once the chat title resolves,
 *  shifting every tab after it. A box read during that window is stale by the
 *  time the gesture presses, so the press lands on a neighbouring tab or on
 *  bare strip background. Neither starts a drag session, and the failure
 *  surfaces far away as a drag chip that never mounts -- on whichever case
 *  happened to measure mid-reflow, which is why the victim moved run to run.
 *
 *  Frames rather than a sleep: this is a layout settle, not a duration. The
 *  cap keeps a genuinely animating element from hanging the case; it returns
 *  the last reading so the caller still fails on its own assertion. */
export async function settledBox(locator, { frames = 3, maxFrames = 180 } = {}) {
  await locator.scrollIntoViewIfNeeded()
  const box = await locator.evaluate((element, settings) => (
    new Promise((resolve) => {
      let previous = null
      let stable = 0
      let seen = 0
      const read = () => {
        const rect = element.getBoundingClientRect()
        const now = { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
        const same = previous
          && now.x === previous.x && now.y === previous.y
          && now.width === previous.width && now.height === previous.height
        stable = same ? stable + 1 : 0
        previous = now
        seen += 1
        if (stable >= settings.frames || seen >= settings.maxFrames) resolve(now)
        else requestAnimationFrame(read)
      }
      requestAnimationFrame(read)
    })
  ), { frames, maxFrames })
  return box
}
