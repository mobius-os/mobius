/**
 * StatusDot — leading colored dot + plain label, the unified
 * "this is set up / connected / configured / on" status indicator
 * used across the shell.
 *
 * Why a shared component:
 *   - The pattern was duplicated as `::before` rules in two CSS
 *     files (SettingsView's status badges + ProviderAuth's
 *     "Connected" / "Not connected" status for each provider row).
 *     The two `::before` blocks were
 *     literally near-identical — easy to drift apart when one is
 *     tweaked without the other.
 *   - The pattern likely keeps showing up (new provider, new
 *     service, push notification status, etc.). Centralising it
 *     means every new use inherits the dot size, gap, and
 *     typography from one source.
 *
 * Color resolution:
 *   - `color` accepts a CSS color literal OR a CSS variable name
 *     (without `var()`). The label is small text, so pass a text-safe
 *     role: `--green-text` for connected / configured, `--danger-text`
 *     for failures, `--muted` for disconnected, `--accent-text` for
 *     accent-colored states.
 *   - When `color` is omitted, falls back to `currentColor` so a
 *     parent that already sets text color cascades to the dot.
 *
 * The label is `children` (any ReactNode) so callers can use
 * plain strings, formatted text, or icons + text combinations.
 */

// A text role (`--green-text`) falls back to its base token (`--green`) so a
// shell served before the theme provides the role keeps its status colour.
function themeVar(name) {
  return name.endsWith('-text') ? `var(${name}, var(${name.slice(0, -5)}))` : `var(${name})`
}

export default function StatusDot({ color, children, className }) {
  // Wrap a literal CSS variable name in var() so callers can
  // write `color="--green-text"` rather than `color="var(--green)"`.
  // Anything else (literal hex, named color, currentColor) passes
  // through unchanged.
  const resolved = color
    ? (color.startsWith('--') ? themeVar(color) : color)
    : 'currentColor'
  return (
    <span
      className={`status-dot${className ? ' ' + className : ''}`}
      style={{ color: resolved }}
    >
      <span className="status-dot__mark" aria-hidden="true" />
      {children}
    </span>
  )
}
