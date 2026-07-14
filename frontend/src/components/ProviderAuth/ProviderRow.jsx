import StatusDot from '../ui/StatusDot.jsx'
import '../ui/StatusDot.css'
import './ProviderAuth.css'

/**
 * Single unified row per provider. Shared by SettingsView and
 * SetupWizard as a compact auth/status row; the optional radio
 * variant is kept for legacy provider-pick surfaces.
 *
 * Props:
 *   id              — provider id ('claude' / 'codex'); used for onSelect
 *   name            — display name
 *   connected       — bool, drives status badge + action label
 *   expanded        — bool, whether the inline auth panel is open
 *   onToggleExpand  — fires when the user wants to open/close auth panel
 *   children        — auth flow component (ProviderAuth / CodexAuth)
 *
 *   showRadio       — bool, default true. False in setup/settings.
 *   isDefault       — bool, only meaningful when showRadio. Drives the
 *                     radio-dot + accent border.
 *   onSelect        — only used when showRadio. Click on the row main
 *                     area calls onSelect(id). When showRadio is false,
 *                     the main area is static.
 *   disabled        — disable the main area (used by settings during
 *                     /settings POST in-flight).
 *   badge           — optional small label rendered next to the name
 *                     (e.g. "Recommended for personal use" in setup).
 *   version         — optional installed CLI/SDK version, shown inline
 *                     next to the name when the provider is connected.
 *
 *   The next three make this the ONE row used for every Settings
 *   line — the two providers, "Chat model", and "Gemini API key" —
 *   so they read as one family. All optional; the provider callers
 *   pass none and keep the connected/version behavior above.
 *   subtitle        — optional one-line muted description under the name.
 *   statusNode      — optional node replacing the default Connected/Not
 *                     connected StatusDot (e.g. "Configured", or the
 *                     chat model's "Last model: Opus 4.8").
 *   actionLabel     — optional override for the action button text
 *                     (e.g. "Reconfigure" / "Configure"); the default
 *                     is the Connect/Reconnect/Close verb below.
 */
export default function ProviderRow({
  id, name, connected, expanded, onToggleExpand, children,
  showRadio = true, isDefault = false, onSelect, disabled = false,
  badge, version, subtitle, statusNode, actionLabel,
}) {
  // Settings page (showRadio=false): the main row is informational
  // only — clicking it does nothing. The user must explicitly tap
  // Connect/Reconnect to open the auth panel. The radio variant
  // keeps row-tap = select provider for legacy surfaces.
  const rowIsClickable = showRadio
  const handleMainClick = rowIsClickable
    ? () => onSelect?.(id)
    : undefined

  const mainTitle = showRadio
    ? (connected
        ? (isDefault ? 'Selected provider' : 'Select provider')
        : 'Tap to set up authentication')
    : undefined

  // Name + (when connected) the installed CLI/SDK version inline +
  // status badge. Extracted so the clickable (setup/radio) and static
  // (settings) row variants render it identically.
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
    <div className={`provider-row${showRadio && isDefault ? ' provider-row--default' : ''}`}>
      {rowIsClickable ? (
        <button
          type="button"
          className="provider-row__main"
          onClick={handleMainClick}
          disabled={disabled}
          title={mainTitle}
        >
          {showRadio && (
            <span className={`provider-row__radio${isDefault ? ' provider-row__radio--on' : ''}`}>
              {isDefault && <span className="provider-row__radio-dot" />}
            </span>
          )}
          {info}
        </button>
      ) : (
        <div className="provider-row__main provider-row__main--static">
          {info}
        </div>
      )}
      <button
        type="button"
        className="provider-row__action"
        onClick={(e) => {
          // The action button is a sibling of the main area but it
          // historically lived inside a row that itself was clickable
          // — stopPropagation is defensive against future restructures
          // that re-introduce row-level click handling.
          e.stopPropagation()
          onToggleExpand?.()
        }}
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
