/* The system font stack has no reliable ✓ glyph, so checkmarks are drawn. */
export function CheckIcon({ size = 13 }) {
  return <svg viewBox="0 0 16 16" width={size} height={size} aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M3 8.5l3.2 3L13 4.5" /></svg>
}
