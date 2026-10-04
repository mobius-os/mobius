/* The one finger every guide demo uses, as the mobius.you demos draw it.

   It appears at the centre of its container and glides to the target, presses (a ring expands from
   it), and, if the target changes, glides on to the next one. Every glide runs at the same speed
   (TOUCH_SPEED px per second, with a short floor so a nudge never looks like a jump) and every press looks the same.
   `still` is for a tap on a spot it already visited: it fades in right there with no glide. */
import { useRef } from 'react'

const TOUCH_SPEED = 420
const glideSeconds = distance => Math.max(0.3, distance / TOUCH_SPEED)

export function Touch({ x, y, from, second, out, still = false }) {
  const press = second ? 'b' : 'a'
  const last = useRef(null)
  const origin = from || { x: x + 90, y: y - 64 }
  const arrive = glideSeconds(Math.hypot(x - origin.x, y - origin.y))
  const move = last.current ? glideSeconds(Math.hypot(x - last.current.x, y - last.current.y)) : arrive
  last.current = { x, y }
  const style = { '--x': `${x}px`, '--y': `${y}px`, '--fx': `${origin.x - x}px`, '--fy': `${origin.y - y}px`, '--arrive': `${arrive}s`, '--move': `${move}s` }
  return <span className={`wt-touch${out ? ' is-out' : ''}${still ? ' wt-touch--still' : ''}`} style={style} aria-hidden="true">
    <i key={`ring-${press}`} className={`wt-touch__ring is-${press}`} />
    <span className="wt-touch__arrive"><i key={`disc-${press}`} className={`wt-touch__disc is-${press}`} /></span>
  </span>
}

/* Where a finger goes: the centre of `target`, and the centre of `root`, which is where it starts.
   Both are in `root`'s own CSS pixels. They come from layout offsets, not bounding boxes, so the
   shell's zoom does not skew them and a transform still in flight does not move them. */
export function fingerPath(target, root) {
  let x = 0
  let y = 0
  for (let node = target; node && node !== root; node = node.offsetParent) { x += node.offsetLeft; y += node.offsetTop }
  return {
    x: x + target.offsetWidth / 2,
    y: y + target.offsetHeight / 2,
    from: { x: root.offsetWidth / 2, y: root.offsetHeight / 2 },
  }
}
