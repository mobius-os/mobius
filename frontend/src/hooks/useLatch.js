import { useState } from 'react'

/**
 * True from the first render where `value` is true, and on every render after.
 *
 * Mounts a surface the first time it is shown and keeps it mounted when it is
 * hidden again, so a lazily loaded surface is neither downloaded before it is
 * needed nor torn down (losing its state) between visits. Latching during
 * render is React's documented "adjust state while rendering" pattern: React
 * re-renders this component before committing, so no frame shows it unset.
 */
export default function useLatch(value) {
  const [latched, setLatched] = useState(Boolean(value))
  if (value && !latched) setLatched(true)
  return latched || Boolean(value)
}
