import './Spinner.css'

/**
 * Spinner — the shell's one "working, duration unknown" indicator.
 *
 * Every loading ring in the shell renders this, so its look, speed,
 * reduced-motion and forced-colors behaviour change in Spinner.css alone.
 * It is a native indeterminate <progress> (no `value`), so assistive
 * technology knows what it is without extra ARIA.
 *
 * - `size`: a pixel number. Omitted, the ring is 1em and matches the text
 *   beside it, which is right for buttons, pills and status lines.
 * - `tone="accent"`: draw the arc in the theme accent instead of the
 *   surrounding text colour, for standalone loading surfaces.
 * - `label`: announce the spinner by itself. Without one it is hidden from
 *   assistive technology, because nearly every caller already shows text
 *   such as "Loading models…" next to it.
 * - `className`: the caller's own placement (margins, absolute position).
 */
export default function Spinner({ size, tone, label, className }) {
  const classes = ['spinner', tone === 'accent' && 'spinner--accent', className]
    .filter(Boolean)
    .join(' ')
  return (
    <progress
      className={classes}
      style={size ? { '--spinner-size': `${size}px` } : undefined}
      aria-label={label || undefined}
      aria-hidden={label ? undefined : 'true'}
    />
  )
}
