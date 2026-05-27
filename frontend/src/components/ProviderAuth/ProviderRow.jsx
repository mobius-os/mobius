import StatusDot from '../ui/StatusDot.jsx'
import '../ui/StatusDot.css'
import './ProviderAuth.css'

/**
 * Single unified row per provider. Shared by SettingsView (with a
 * default-radio, used to pick which provider new chats use) and
 * SetupWizard (no radio, used as a tap-to-connect list during
 * first-time setup).
 *
 * Props:
 *   id              — provider id ('claude' / 'codex'); used for onSelect
 *   name            — display name
 *   connected       — bool, drives status badge + action label
 *   expanded        — bool, whether the inline auth panel is open
 *   onToggleExpand  — fires when the user wants to open/close auth panel
 *   children        — auth flow component (ProviderAuth / CodexAuth)
 *
 *   showRadio       — bool, default true. False in setup wizard.
 *   isDefault       — bool, only meaningful when showRadio. Drives the
 *                     radio-dot + accent border.
 *   onSelect        — only used when showRadio. Click on the row main
 *                     area calls onSelect(id) — settings uses this to
 *                     switch the default. When showRadio is false, the
 *                     main area toggles expand instead.
 *   disabled        — disable the main area (used by settings during
 *                     /settings POST in-flight).
 *   badge           — optional small label rendered next to the name
 *                     (e.g. "Recommended for personal use" in setup).
 */
export default function ProviderRow({
  id, name, connected, expanded, onToggleExpand, children,
  showRadio = true, isDefault = false, onSelect, disabled = false,
  badge,
}) {
  // Settings page (showRadio=false): the main row is INFORMATIONAL
  // only — clicking it does nothing. The user must explicitly tap
  // Connect/Reconnect to open the auth panel. The radio variant
  // (setup wizard, showRadio=true) keeps row-tap = set-default.
  const rowIsClickable = showRadio
  const handleMainClick = rowIsClickable
    ? () => onSelect?.(id)
    : undefined

  const mainTitle = showRadio
    ? (connected
        ? (isDefault ? 'Default for new chats' : 'Set as default')
        : 'Tap to set up authentication')
    : undefined

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
          <span className="provider-row__info">
            <span className="provider-row__name">{name}</span>
            {badge && (
              <span className="provider-row__badge">{badge}</span>
            )}
            <StatusDot color={connected ? '--green' : '--muted'}>
              {connected ? 'Connected' : 'Not connected'}
            </StatusDot>
          </span>
        </button>
      ) : (
        <div className="provider-row__main provider-row__main--static">
          <span className="provider-row__info">
            <span className="provider-row__name">{name}</span>
            {badge && (
              <span className="provider-row__badge">{badge}</span>
            )}
            <StatusDot color={connected ? '--green' : '--muted'}>
              {connected ? 'Connected' : 'Not connected'}
            </StatusDot>
          </span>
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
          const verb = expanded ? 'Close' : (connected ? 'Reconnect' : 'Connect')
          return `${verb} ${name}`
        })()}
      >
        {connected
          ? (expanded ? 'Close' : 'Reconnect')
          : (expanded ? 'Close' : 'Connect')}
      </button>
      {expanded && (
        <div className="provider-row__auth">
          {children}
        </div>
      )}
    </div>
  )
}
