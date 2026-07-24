import StatusDot from '../ui/StatusDot.jsx'
import '../ui/StatusDot.css'
import './ProviderAuth.css'

/**
 * Single unified row per provider. Shared by SettingsView and
 * SetupWizard as a compact, informational auth/status row.
 *
 * Props:
 *   name            — display name
 *   connected       — bool, drives status badge + action label
 *   expanded        — bool, whether the inline auth panel is open
 *   onToggleExpand  — fires when the user wants to open/close auth panel
 *   children        — auth flow component (ProviderAuth / CodexAuth)
 *
 *   badge           — optional small label rendered next to the name
 *                     (e.g. "Recommended for personal use" in setup).
 *   version         — optional installed CLI/SDK version, shown inline
 *                     next to the name when the provider is connected.
 *
 *   The next three make this the ONE row used for every Settings
 *   line — the two providers and "Chat model" — so they read as one
 *   family. All optional; the provider callers
 *   pass none and keep the connected/version behavior above.
 *   subtitle        — optional one-line muted description under the name.
 *   statusNode      — optional node replacing the default Connected/Not
 *                     connected StatusDot (e.g. "Configured", or the
 *                     chat model's "Last model: Opus 4.8").
 *   actionLabel     — optional override for the action button text
 *                     (e.g. "Reconfigure" / "Configure"); the default
 *                     is the Connect/Reconnect/Close verb below.
 *   disabled        — greys the informational row and disables its action.
 */
export default function ProviderRow({
  name, connected, expanded, onToggleExpand, children,
  badge, version, subtitle, statusNode, actionLabel, disabled = false,
}) {
  // Name + installed CLI/SDK version + status are informational. The explicit
  // action button is the only interactive target in the row.
  const info = (
    <span className="provider-row__info">
      <span className="provider-row__name-line">
        <span className="provider-row__name">{name}</span>
        {connected && version && (
          <span className="provider-row__version" title="Installed CLI version">
            {version}
          </span>
        )}
      </span>
      {badge && (
        <span className="provider-row__badge">{badge}</span>
      )}
      {subtitle && (
        <span className="provider-row__sub">{subtitle}</span>
      )}
      {statusNode !== undefined ? (
        statusNode
      ) : (
        <StatusDot color={connected ? '--green' : '--muted'}>
          {connected ? 'Connected' : 'Not connected'}
        </StatusDot>
      )}
    </span>
  )

  return (
    <div className={`provider-row${disabled ? ' provider-row--disabled' : ''}`}>
      <div className="provider-row__main">{info}</div>
      <button
        type="button"
        className="provider-row__action"
        onClick={() => onToggleExpand?.()}
        disabled={disabled}
        aria-expanded={expanded}
        aria-label={(() => {
          const verb = expanded
            ? 'Close'
            : (actionLabel || (connected ? 'Reconnect' : 'Connect'))
          return `${verb} ${name}`
        })()}
      >
        {expanded
          ? 'Close'
          : (actionLabel || (connected ? 'Reconnect' : 'Connect'))}
      </button>
      {expanded && (
        <div className="provider-row__auth">
          {children}
        </div>
      )}
    </div>
  )
}
