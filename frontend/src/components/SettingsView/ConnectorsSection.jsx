/* Owner-managed remote MCP connections, deliberately separate from each
 * provider's own installed apps/plugins. The backend probes a connection and
 * returns its tool catalog before this surface adds anything to agent turns. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import {
  ApiKey,
  ArrowRotateCw,
  ConnectorsConnectedApps,
  Delete,
} from '@openai/apps-sdk-ui/components/Icon'
import { api, jsonOrThrow } from '../../api/client.js'
import {
  connectorSchemaCostLabel,
  connectorStatus,
} from '../../lib/connectorViewModel.js'
import StatusDot from '../ui/StatusDot.jsx'

export default function ConnectorsSection({ active = true }) {
  const [connections, setConnections] = useState(null)
  const [unavailable, setUnavailable] = useState(false)
  const [listError, setListError] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [usesKey, setUsesKey] = useState(false)
  const [authValue, setAuthValue] = useState('')
  const [authHeader, setAuthHeader] = useState('Authorization')
  const [addPhase, setAddPhase] = useState('idle')
  const [addError, setAddError] = useState('')
  const [busyIds, setBusyIds] = useState(() => new Set())
  const [confirmRemove, setConfirmRemove] = useState(null)
  const listRequest = useRef(0)
  const confirmRemoveButton = useRef(null)
  const removeButtons = useRef(new Map())
  const addButton = useRef(null)
  const focusAfterRemove = useRef(null)

  const setConnectionBusy = useCallback((connectionId, busy) => {
    setBusyIds(current => {
      const next = new Set(current)
      if (busy) next.add(connectionId)
      else next.delete(connectionId)
      return next
    })
  }, [])

  const load = useCallback(async () => {
    if (!active) return
    const requestId = ++listRequest.current
    setListError('')
    try {
      const response = await api.connectors.list({ timeoutMs: 10000 })
      if (requestId !== listRequest.current) return
      if (response.status === 404 || response.status === 503) {
        setUnavailable(true)
        setConnections([])
        return
      }
      const data = await jsonOrThrow(response, 'Could not load connections')
      if (requestId !== listRequest.current) return
      setUnavailable(false)
      setConnections(data.connectors || [])
    } catch (error) {
      if (requestId !== listRequest.current) return
      setListError(error.message || 'Could not load connections')
      setConnections([])
    }
  }, [active])

  useEffect(() => {
    load()
    return () => { listRequest.current += 1 }
  }, [load])

  useEffect(() => {
    if (!active) return undefined
    const recheck = () => {
      if (document.visibilityState === 'visible') load()
    }
    window.addEventListener('focus', recheck)
    document.addEventListener('visibilitychange', recheck)
    return () => {
      window.removeEventListener('focus', recheck)
      document.removeEventListener('visibilitychange', recheck)
    }
  }, [active, load])

  useEffect(() => {
    if (confirmRemove !== null) confirmRemoveButton.current?.focus()
  }, [confirmRemove])

  useEffect(() => {
    const connectorId = focusAfterRemove.current
    if (connectorId === null) return
    focusAfterRemove.current = null
    ;(removeButtons.current.get(connectorId) || addButton.current)?.focus()
  }, [connections])

  async function addConnection(event) {
    event.preventDefault()
    if (!url.trim() || addPhase === 'adding') return
    setAddPhase('adding')
    setAddError('')
    try {
      const response = await api.connectors.add({
        url: url.trim(),
        name: name.trim(),
        auth_header: usesKey ? authHeader.trim() : '',
        auth_value: usesKey ? authValue.trim() : '',
      }, { timeoutMs: 25000 })
      const row = await jsonOrThrow(response, 'Could not add connection')
      listRequest.current += 1
      setConnections(current => [...(current || []), row])
      setUrl('')
      setName('')
      setAuthValue('')
      setAuthHeader('Authorization')
      setUsesKey(false)
      setAddOpen(false)
    } catch (error) {
      setAddError(error.message || 'Could not add connection')
    } finally {
      setAddPhase('idle')
    }
  }

  async function updateConnection(connection, patch) {
    setConnectionBusy(connection.id, true)
    setListError('')
    try {
      const response = await api.connectors.update(connection.id, patch)
      const row = await jsonOrThrow(response, 'Could not update connection')
      listRequest.current += 1
      setConnections(current => current.map(item => (
        item.id === row.id ? row : item
      )))
    } catch (error) {
      setListError(error.message || 'Could not update connection')
    } finally {
      setConnectionBusy(connection.id, false)
    }
  }

  async function refreshConnection(connection) {
    setConnectionBusy(connection.id, true)
    setListError('')
    try {
      const response = await api.connectors.refresh(connection.id)
      const row = await jsonOrThrow(response, 'Could not re-check connection')
      listRequest.current += 1
      setConnections(current => current.map(item => (
        item.id === row.id ? row : item
      )))
    } catch (error) {
      setListError(error.message || 'Could not re-check connection')
    } finally {
      setConnectionBusy(connection.id, false)
    }
  }

  async function removeConnection(connection) {
    setConnectionBusy(connection.id, true)
    setListError('')
    try {
      const response = await api.connectors.remove(connection.id)
      await jsonOrThrow(response, 'Could not remove connection')
      listRequest.current += 1
      const index = connections.findIndex(item => item.id === connection.id)
      const survivor = connections[index + 1] || connections[index - 1]
      focusAfterRemove.current = survivor?.id ?? -1
      setConnections(current => current.filter(item => item.id !== connection.id))
      setConfirmRemove(null)
    } catch (error) {
      setListError(error.message || 'Could not remove connection')
    } finally {
      setConnectionBusy(connection.id, false)
    }
  }

  return (
    <section className="settings__section settings-connections" id="settings-connections">
      <h2 className="settings__section-title">Connections</h2>
      <div className="settings-connections__heading">
        <span className="settings-connections__heading-icon" aria-hidden="true">
          <ConnectorsConnectedApps />
        </span>
        <div>
          <span className="settings__label">Custom MCP</span>
          <p className="settings__subtext settings__subtext--tight">
            Shared with Claude Code and Codex. Provider-installed apps stay
            with their provider; these endpoints become available on the next turn.
          </p>
        </div>
      </div>

      {unavailable && (
        <div className="settings__notice" role="status">
          Connections will finish setting up after the server restarts.{' '}
          <button
            type="button"
            className="settings-connections__text-button"
            onClick={load}
          >
            Check again
          </button>
        </div>
      )}

      {connections === null ? (
        <div className="settings__notice" role="status">Loading connections…</div>
      ) : connections.length === 0 && !unavailable ? (
        <div className="settings__notice" role="status">
          No custom MCP connections yet.
        </div>
      ) : (
        <div className="settings-connections__list">
          {connections.map(connection => {
            const status = connectorStatus(connection)
            const schemaCost = connectorSchemaCostLabel(connection.est_tokens)
            const removing = confirmRemove === connection.id
            const busy = busyIds.has(connection.id)
            return (
              <article className="settings-connections__item" key={connection.id}>
                <div className="settings-connections__main">
                  <div className="settings-connections__title">
                    <StatusDot color={status.color}>{connection.name}</StatusDot>
                    <span className="settings-connections__state">{status.text}</span>
                  </div>
                  <span className="settings-connections__meta">
                    {connection.tool_count} tool{connection.tool_count === 1 ? '' : 's'}
                    {schemaCost ? ` · ${schemaCost}` : ''}
                    {connection.has_auth ? ' · protected by key' : ''}
                  </span>
                  {connection.tools?.length > 0 && (
                    <span className="settings-connections__tools">
                      {connection.tools.slice(0, 6).map(tool => tool.name).join(', ')}
                      {connection.tool_count > 6 ? '…' : ''}
                    </span>
                  )}
                  {connection.status === 'error' && connection.status_detail && (
                    <span className="settings-connections__error">
                      {connection.status_detail}
                    </span>
                  )}
                </div>

                <div className="settings-connections__actions">
                  {removing ? (
                    <div className="settings-connections__confirm" role="group" aria-label={`Remove ${connection.name}?`}>
                      <button
                        type="button"
                        ref={confirmRemoveButton}
                        className="settings-connections__danger-button"
                        disabled={busy}
                        onClick={() => removeConnection(connection)}
                      >
                        Remove
                      </button>
                      <button
                        type="button"
                        className="settings-connections__quiet-button"
                        onClick={() => {
                          setConfirmRemove(null)
                          requestAnimationFrame(() => {
                            removeButtons.current.get(connection.id)?.focus()
                          })
                        }}
                      >
                        Keep
                      </button>
                    </div>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="settings-connections__icon-button"
                        aria-label={`Re-check ${connection.name}`}
                        title="Re-check endpoint and tool catalog"
                        disabled={busy}
                        onClick={() => refreshConnection(connection)}
                      >
                        <ArrowRotateCw aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        className="settings-connections__icon-button settings-connections__icon-button--danger"
                        ref={element => {
                          if (element) removeButtons.current.set(connection.id, element)
                          else removeButtons.current.delete(connection.id)
                        }}
                        aria-label={`Remove ${connection.name}`}
                        title="Remove connection"
                        disabled={busy}
                        onClick={() => setConfirmRemove(connection.id)}
                      >
                        <Delete aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={connection.enabled}
                        aria-label={`${connection.name} available to agents`}
                        className={`settings-connections__switch${connection.enabled ? ' settings-connections__switch--on' : ''}`}
                        disabled={busy}
                        onClick={() => updateConnection(connection, {
                          enabled: !connection.enabled,
                        })}
                      >
                        <span aria-hidden="true" />
                      </button>
                    </>
                  )}
                </div>
              </article>
            )
          })}
        </div>
      )}

      {!unavailable && (addOpen ? (
        <form className="settings-connections__form" onSubmit={addConnection}>
          <div className="settings-connections__field-grid">
            <label className="settings-connections__field settings-connections__field--wide">
              <span>Streamable HTTP endpoint</span>
              <input
                type="url"
                inputMode="url"
                placeholder="https://example.com/mcp"
                value={url}
                onChange={event => setUrl(event.target.value)}
                autoFocus
                required
              />
            </label>
            <label className="settings-connections__field">
              <span>Name <small>optional</small></span>
              <input
                type="text"
                placeholder="Use server name"
                value={name}
                onChange={event => setName(event.target.value)}
                maxLength={128}
              />
            </label>
          </div>

          <button
            type="button"
            className="settings-connections__key-toggle"
            aria-expanded={usesKey}
            onClick={() => setUsesKey(current => {
              if (current) {
                setAuthValue('')
                setAuthHeader('Authorization')
              }
              return !current
            })}
          >
            <ApiKey aria-hidden="true" />
            <span>{usesKey ? 'Remove API key' : 'Add API key'}</span>
          </button>

          {usesKey && (
            <div className="settings-connections__field-grid">
              <label className="settings-connections__field settings-connections__field--wide">
                <span>API key</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={authValue}
                  onChange={event => setAuthValue(event.target.value)}
                  required
                />
              </label>
              <label className="settings-connections__field">
                <span>Header</span>
                <input
                  type="text"
                  value={authHeader}
                  onChange={event => setAuthHeader(event.target.value)}
                  placeholder="Authorization"
                  required
                />
              </label>
            </div>
          )}

          <p className="settings-connections__support-note">
            Public HTTPS endpoints with no auth or a static key are supported.
            OAuth sign-in, including Google, needs the next authorization layer.
          </p>
          <div className="settings-connections__form-actions">
            <button
              type="submit"
              className="settings__btn settings__btn--sm"
              disabled={addPhase === 'adding' || !url.trim() || (usesKey && !authValue.trim())}
            >
              {addPhase === 'adding' ? 'Checking…' : 'Check and add'}
            </button>
            <button
              type="button"
              className="settings__btn settings__btn--outline settings__btn--sm"
              disabled={addPhase === 'adding'}
              onClick={() => {
                setAddOpen(false)
                setAddError('')
                setUrl('')
                setName('')
                setAuthValue('')
                setAuthHeader('Authorization')
                setUsesKey(false)
              }}
            >
              Cancel
            </button>
          </div>
          {addError && <Alert color="danger" variant="soft" description={addError} />}
        </form>
      ) : (
        <button
          type="button"
          ref={addButton}
          className="settings__btn settings__btn--outline settings__btn--sm settings-connections__add-button"
          onClick={() => setAddOpen(true)}
        >
          Add connection
        </button>
      ))}

      {listError && <Alert color="danger" variant="soft" description={listError} />}
    </section>
  )
}
