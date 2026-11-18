import { providerSetupTarget } from './providerRegistry.jsx'

/**
 * The setup link a provider's logo offers in the model picker, or null when
 * this host cannot open it (an embedded chat passes neither handler). A
 * built-in opens its Settings page; an app provider opens its app, where it
 * is connected.
 */
export function providerSetupLink(providerId, label, { onOpenProviderSettings, onOpenProviderApp } = {}) {
  const target = providerSetupTarget(providerId)
  if (target?.kind === 'settings' && onOpenProviderSettings) {
    return { label: `Open ${label} setup in Settings`, open: () => onOpenProviderSettings(target.page) }
  }
  if (target?.kind === 'app' && onOpenProviderApp) {
    return { label: `Open the ${label} app`, open: () => onOpenProviderApp(target.appId) }
  }
  return null
}

/**
 * The picker's link handlers for a host. An embedded chat cannot leave its
 * host, so it gets none and its logos stay plain. Otherwise each link closes
 * the picker first, then navigates.
 */
export function providerSetupHandlers({ embedded = false, onOpenProviderSettings, onOpenApp, close }) {
  if (embedded) return { onOpenProviderSettings: undefined, onOpenProviderApp: undefined }
  const closingFirst = open => open && (target => { close(); open(target) })
  return {
    onOpenProviderSettings: closingFirst(onOpenProviderSettings),
    onOpenProviderApp: closingFirst(onOpenApp),
  }
}

/**
 * One model row: the provider logo opens that provider's setup (when a link
 * exists) and the rest of the row selects the model. Two separate buttons, so
 * opening setup never changes the selection and a disconnected provider's
 * setup stays reachable while its model cannot be picked.
 */
export default function ModelPickerRow({
  Logo, modelLabel, subtitle, setup = null, selected = false, disabled = false, title, onSelect,
}) {
  const icon = <span className="csp-row__icon"><Logo /></span>
  return (
    <div className={`csp-row${selected ? ' csp-row--selected' : ''}`}>
      {setup ? (
        <button
          type="button"
          className="csp-row__icon-button"
          onClick={setup.open}
          aria-label={setup.label}
          title={setup.label}
        >
          {icon}
        </button>
      ) : (
        <span className="csp-row__icon-slot">{icon}</span>
      )}
      <button
        type="button"
        className="csp-row__model-button"
        onClick={onSelect}
        disabled={disabled}
        aria-pressed={selected}
        title={title}
      >
        <span className="csp-row__main">
          <span className="csp-row__title"><span>{modelLabel}</span></span>
          <span className="csp-row__sub">{subtitle}</span>
        </span>
        <span className="csp-row__dot" />
      </button>
    </div>
  )
}
