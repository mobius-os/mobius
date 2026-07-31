/* ConnectorsSection — Settings surface for external MCP connectors.
 *
 * Owns its own data (list, add, toggle, refresh, remove) against
 * /api/connectors so SettingsView stays a composer, not an owner. Add
 * runs the server-side handshake before anything is saved: the owner
 * sees the tool count and a per-message token estimate (or a concrete
 * failure) at the moment of adding — cost-before-commit is the design's
 * rule. A 404/503 from the API means the backend behind this feature
 * hasn't been loaded yet (restart pending); render that as guidance,
 * not an error.
 */
import { useCallback, useEffect, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import Ellipsis from 'lucide-react/dist/esm/icons/ellipsis.mjs'
import RefreshCw from 'lucide-react/dist/esm/icons/refresh-cw.mjs'
import Trash2 from 'lucide-react/dist/esm/icons/trash-2.mjs'
import { apiFetch, jsonOrThrow } from '../../api/client.js'
import StatusDot from '../ui/StatusDot.jsx'

function tokensLabel(estTokens) {
  if (!estTokens) return null
  if (estTokens >= 1000) return `~${(estTokens / 1000).toFixed(estTokens >= 10000 ? 0 : 1)}k tokens/msg`
  return `~${estTokens} tokens/msg`
}

function connectorStatus(connector) {
  if (connector.status === 'error') return { color: '--danger', label: 'Unreachable' }
  if (!connector.enabled) return { color: '--border', label: 'Off' }
  return { color: '--green', label: 'On' }
}

export default function ConnectorsSection() {
  const [connectors, setConnectors] = useState(null) // null = loading
  const [unavailable, setUnavailable] = useState(false)
  const [listError, setListError] = useState('')

  const [addOpen, setAddOpen] = useState(false)
  const [url, setUrl] = useState('')
  const [usesKey, setUsesKey] = useState(false)
  const [authValue, setAuthValue] = useState('')
  const [authHeader, setAuthHeader] = useState('Authorization')
  const [addPhase, setAddPhase] = useState('idle') // idle | adding
  const [addError, setAddError] = useState('')
  const [justAdded, setJustAdded] = useState(null)

  const [confirmRemove, setConfirmRemove] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [menuOpen, setMenuOpen] = useState(null)

  // The kebab menu closes on outside pointer or Escape — standard popover
  // hygiene so an open menu never lingers over unrelated Settings rows.
  useEffect(() => {
    if (menuOpen === null) return undefined
    const onPointer = (event) => {
      if (!event.target.closest('.settings-connectors__menu-wrap')) {
        setMenuOpen(null)
      }
    }
    const onKey = (event) => {
      if (event.key === 'Escape') setMenuOpen(null)
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  const load = useCallback(async () => {
    setListError('')
    try {
      const res = await apiFetch('/connectors')
      if (res.status === 404 || res.status === 503) {
        setUnavailable(true)
        setConnectors([])
        return
      }
      const data = await jsonOrThrow(res, 'Could not load connectors')
      setUnavailable(false)
      setConnectors(data.connectors || [])
    } catch (err) {
      setListError(err.message || 'Could not load connectors')
      setConnectors([])
    }
  }, [])

  useEffect(() => { load() }, [load])

  // Availability is transient state — a view mounted mid-restart must not
  // latch "restart pending" forever. Re-check whenever the app regains
  // focus/visibility (covers PWA resume and post-restart reloads alike).
  useEffect(() => {
    const recheck = () => {
      if (document.visibilityState === 'visible') load()
    }
    window.addEventListener('focus', recheck)
    document.addEventListener('visibilitychange', recheck)
    return () => {
      window.removeEventListener('focus', recheck)
      document.removeEventListener('visibilitychange', recheck)
    }
  }, [load])

  async function addConnector(event) {
    event.preventDefault()
    if (!url.trim() || addPhase === 'adding') return
    setAddPhase('adding')
    setAddError('')
    try {
      const res = await apiFetch('/connectors', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url: url.trim(),
          auth_header: usesKey ? authHeader.trim() : '',
          auth_value: usesKey ? authValue.trim() : '',
        }),
      })
      const row = await jsonOrThrow(res, 'Could not add connector')
      setConnectors((current) => [...(current || []), row])
      setJustAdded(row.id)
      setUrl('')
      setAuthValue('')
      setUsesKey(false)
      setAddOpen(false)
    } catch (err) {
      setAddError(err.message || 'Could not add connector')
    } finally {
      setAddPhase('idle')
    }
  }

  async function patchConnector(connector, body) {
    setBusyId(connector.id)
    try {
      const res = await apiFetch(`/connectors/${connector.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const row = await jsonOrThrow(res, 'Could not update connector')
      setConnectors((current) => current.map((c) => (c.id === row.id ? row : c)))
    } catch (err) {
      setListError(err.message || 'Could not update connector')
    } finally {
      setBusyId(null)
    }
  }

  async function refreshConnector(connector) {
    setBusyId(connector.id)
    try {
      const res = await apiFetch(`/connectors/${connector.id}/refresh`, {
        method: 'POST',
      })
      const row = await jsonOrThrow(res, 'Could not re-check connector')
      setConnectors((current) => current.map((c) => (c.id === row.id ? row : c)))
    } catch (err) {
      setListError(err.message || 'Could not re-check connector')
    } finally {
      setBusyId(null)
    }
  }

  async function removeConnector(connector) {
    setBusyId(connector.id)
    setConfirmRemove(null)
    try {
      const res = await apiFetch(`/connectors/${connector.id}`, {
        method: 'DELETE',
      })
      await jsonOrThrow(res, 'Could not remove connector')
      setConnectors((current) => current.filter((c) => c.id !== connector.id))
    } catch (err) {
      setListError(err.message || 'Could not remove connector')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <section className="settings__section" id="settings-connectors">
      <h2 className="settings__section-title">Connectors</h2>
      <p className="settings__subtext settings-connectors__intro">
        Connected services give every chat extra abilities. Each connector
        lists what it adds — and roughly what it costs per message — before
        you enable it.
      </p>

      {unavailable && (
        <div className="settings__notice" role="status">
          Connectors finish setting up after the next server restart
          (Server → Restart below).{' '}
          <button
            type="button"
            className="settings__btn settings__btn--outline settings__btn--sm"
            onClick={load}
          >
            Check again
          </button>
        </div>
      )}

      {connectors === null ? (
        <div className="settings__notice" role="status">Loading connectors…</div>
      ) : connectors.length === 0 && !unavailable ? (
        <div className="settings__notice" role="status">
          No connectors yet. Add a service by its connector address.
        </div>
      ) : (
        <div className="settings-connectors__list">
          {connectors.map((connector) => {
            const status = connectorStatus(connector)
            const tokens = tokensLabel(connector.est_tokens)
            return (
              <div className="settings-connectors__row" key={connector.id}>
                <div className="settings-connectors__main">
                  <div className="settings-connectors__title">
                    <StatusDot color={status.color}>{connector.name}</StatusDot>
                    <button
                      type="button"
                      className="settings-connectors__recheck"
                      aria-label={`Re-check ${connector.name}`}
                      title="Probe the service again: refresh its tool list, cost estimate, and reachability"
                      disabled={busyId === connector.id}
                      onClick={() => refreshConnector(connector)}
                    >
                      <RefreshCw size={13} strokeWidth={2} />
                    </button>
                  </div>
                  <span className="settings-connectors__meta">
                    {connector.tool_count} tool{connector.tool_count === 1 ? '' : 's'}
                    {tokens ? ` · ${tokens}` : ''}
                    {connector.has_auth ? ' · uses a key' : ''}
                  </span>
                  {connector.status === 'error' && connector.status_detail && (
                    <span className="settings-connectors__error">
                      {connector.status_detail}
                    </span>
                  )}
                  {justAdded === connector.id && connector.tools.length > 0 && (
                    <span className="settings-connectors__meta">
                      Adds: {connector.tools.slice(0, 6).map((t) => t.name).join(', ')}
                      {connector.tool_count > 6 ? '…' : ''}
                    </span>
                  )}
                </div>
                <div className="settings-connectors__actions">
                  {confirmRemove === connector.id ? (
                    <>
                      <button
                        type="button"
                        className="settings__btn settings__btn--sm settings-connectors__danger"
                        disabled={busyId === connector.id}
                        onClick={() => removeConnector(connector)}
                      >
                        Really remove
                      </button>
                      <button
                        type="button"
                        className="settings__btn settings__btn--outline settings__btn--sm"
                        onClick={() => setConfirmRemove(null)}
                      >
                        Keep
                      </button>
                    </>
                  ) : (
                    <div className="settings-connectors__menu-wrap">
                      <button
                        type="button"
                        className="settings-connectors__icon-btn"
                        aria-label={`More options for ${connector.name}`}
                        aria-haspopup="menu"
                        aria-expanded={menuOpen === connector.id}
                        disabled={busyId === connector.id}
                        onClick={() => setMenuOpen(
                          menuOpen === connector.id ? null : connector.id
                        )}
                      >
                        <Ellipsis size={16} strokeWidth={2} />
                      </button>
                      {menuOpen === connector.id && (
                        <div className="settings-connectors__menu" role="menu">
                          {/* Future per-connector actions (change address,
                              manage sign-in/OAuth, rename) slot in here as
                              additional menu items. */}
                          <button
                            type="button"
                            role="menuitem"
                            className="settings-connectors__menu-item settings-connectors__menu-item--danger"
                            onClick={() => {
                              setMenuOpen(null)
                              setConfirmRemove(connector.id)
                            }}
                          >
                            <Trash2 size={14} strokeWidth={2} />
                            Delete
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                  <button
                    type="button"
                    role="switch"
                    aria-checked={connector.enabled}
                    aria-label={`${connector.name} enabled`}
                    className={`settings-connectors__switch${connector.enabled ? ' settings-connectors__switch--on' : ''}`}
                    disabled={busyId === connector.id}
                    onClick={() => patchConnector(connector, { enabled: !connector.enabled })}
                  >
                    <span className="settings-connectors__switch-knob" aria-hidden="true" />
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {!unavailable && (addOpen ? (
        <form className="settings-connectors__add" onSubmit={addConnector}>
          <input
            className="settings-connectors__input"
            type="url"
            placeholder="Connector address (https://…)"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            autoFocus
          />
          <button
            type="button"
            role="switch"
            aria-checked={usesKey}
            className="settings-connectors__key-row"
            onClick={() => setUsesKey(!usesKey)}
          >
            <span className="settings-connectors__key-label">
              This service uses an API key
            </span>
            <span
              className={`settings-connectors__switch${usesKey ? ' settings-connectors__switch--on' : ''}`}
              aria-hidden="true"
            >
              <span className="settings-connectors__switch-knob" />
            </span>
          </button>
          {usesKey && (
            <div className="settings-connectors__key-fields">
              <input
                className="settings-connectors__input"
                type="password"
                placeholder="API key"
                value={authValue}
                onChange={(e) => setAuthValue(e.target.value)}
              />
              <input
                className="settings-connectors__input settings-connectors__input--header"
                type="text"
                placeholder="Header (Authorization)"
                value={authHeader}
                onChange={(e) => setAuthHeader(e.target.value)}
              />
            </div>
          )}
          <div className="settings-connectors__actions">
            <button
              type="submit"
              className="settings__btn settings__btn--sm"
              disabled={addPhase === 'adding' || !url.trim()}
            >
              {addPhase === 'adding' ? 'Checking service…' : 'Add connector'}
            </button>
            <button
              type="button"
              className="settings__btn settings__btn--outline settings__btn--sm"
              onClick={() => { setAddOpen(false); setAddError('') }}
            >
              Cancel
            </button>
          </div>
          {addError && (
            <Alert color="danger" variant="soft" description={addError} />
          )}
        </form>
      ) : (
        <div className="settings__row">
          <button
            type="button"
            className="settings__btn settings__btn--outline settings__btn--sm"
            onClick={() => setAddOpen(true)}
          >
            Add connector
          </button>
        </div>
      ))}

      {listError && (
        <Alert color="danger" variant="soft" description={listError} />
      )}
    </section>
  )
}
