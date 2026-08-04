/* Owner-managed remote MCP connections shared by both agent providers. */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import {
  ApiKey,
  ArrowRotateCw,
  Delete,
} from '@openai/apps-sdk-ui/components/Icon'
import { api, jsonOrThrow } from '../../api/client.js'
import { connectorQueries } from '../../hooks/queries.js'
import StatusDot from '../ui/StatusDot.jsx'

const EMPTY_CONNECTIONS = []

function displayEndpoint(value) {
  try {
    const endpoint = new URL(value)
    return `${endpoint.origin}${endpoint.pathname}`
  } catch {
    return String(value || '').split(/[?#]/, 1)[0]
  }
}

function focusConnectionTarget(root, connectorId = null) {
  const preferred = connectorId === null
    ? null
    : root?.querySelector(`[data-connector-remove="${connectorId}"]`)
  const fallback = root?.querySelector(
    '#settings-connections-add, #settings-connections-endpoint:not(:disabled)',
  )
  const target = preferred || fallback
  target?.focus()
}

// Where focus belongs once the settled DOM exists. `restore` puts the owner
// back on the control they ran (the element is remembered before the mutation
// disables it); `target` picks the connection row — or the add affordance — the
// section wants next.
const restoreFocusTo = node => ({ kind: 'restore', node })
const focusConnection = connectorId => ({ kind: 'target', connectorId })

function ConnectorForm({ disabled, onAdd, onCancel }) {
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [usesKey, setUsesKey] = useState(false)
  const [authValue, setAuthValue] = useState('')
  const [authHeader, setAuthHeader] = useState('Authorization')
  const [error, setError] = useState('')

  async function submit(event) {
    event.preventDefault()
    if (disabled || !url.trim()) return
    setError('')
    try {
      await onAdd({
        url: url.trim(),
        name: name.trim(),
        auth_header: usesKey ? authHeader.trim() : '',
        auth_value: usesKey ? authValue.trim() : '',
      })
      onCancel()
    } catch (submitError) {
      setError(submitError.message || 'Could not add connection')
    }
  }

  function toggleKey() {
    setUsesKey(current => {
      if (current) {
        setAuthValue('')
        setAuthHeader('Authorization')
      }
      return !current
    })
  }

  return (
    <form
      className="settings-connections__form"
      aria-busy={disabled}
      onSubmit={submit}
    >
      <fieldset className="settings-connections__fieldset" disabled={disabled}>
        <div className="settings-connections__field-grid">
          <label className="settings-connections__field settings-connections__field--wide">
            <span>Streamable HTTP endpoint</span>
            <input
              id="settings-connections-endpoint"
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
          onClick={toggleKey}
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
          Supports public HTTPS endpoints with no authentication or one static key.
        </p>
        <div className="settings-connections__form-actions">
          <button
            type="submit"
            className="settings__btn settings__btn--sm"
            disabled={!url.trim() || (usesKey && !authValue.trim())}
          >
            {disabled ? 'Checking…' : 'Check and add'}
          </button>
          <button
            type="button"
            className="settings__btn settings__btn--outline settings__btn--sm"
            onClick={onCancel}
          >
            Cancel
          </button>
        </div>
      </fieldset>
      {error && <Alert color="danger" variant="soft" description={error} />}
    </form>
  )
}

export default function ConnectorsSection({ active = true }) {
  const queryClient = useQueryClient()
  const listQuery = connectorQueries.list.useQuery({ enabled: active })
  const connections = listQuery.data || EMPTY_CONNECTIONS
  const [addOpen, setAddOpen] = useState(false)
  const [pending, setPending] = useState(false)
  const [actionError, setActionError] = useState('')
  const [confirmRemove, setConfirmRemove] = useState(null)
  const [focusIntent, setFocusIntent] = useState(null)
  const mutationPending = useRef(false)
  const section = useRef(null)
  const confirmButton = useRef(null)

  useEffect(() => {
    if (confirmRemove !== null) confirmButton.current?.focus()
  }, [confirmRemove])

  useEffect(() => {
    if (confirmRemove === null) return
    const current = connections.find(
      connection => connection.id === confirmRemove.id,
    )
    if (current?.generation === confirmRemove.generation) return
    setConfirmRemove(null)
    setFocusIntent(focusConnection(current?.id ?? null))
  }, [confirmRemove, connections])

  // Restore focus in the COMMIT that produces the settled DOM, never on the
  // next animation frame.
  //
  // Every action here disables its own control while the mutation is in flight
  // (`disabled={busy}`), and disabling the focused element drops focus to
  // <body>. The re-enabling render is a React state update scheduled through
  // the scheduler (a task), while requestAnimationFrame is a rendering-phase
  // callback: the two have no guaranteed order, and under load the frame
  // regularly runs FIRST. The old code then found the control still `:disabled`
  // — or, for the add form, found `#settings-connections-add` not rendered yet
  // and `#settings-connections-endpoint` still inside a disabled fieldset — and
  // its guards silently skipped the focus. Nothing retried, so the owner's
  // keyboard position was lost to <body> for the rest of the session: every
  // subsequent Tab restarted at the top of the document. Queuing the intent and
  // applying it from a layout effect that runs only once `pending` is false
  // makes "the mutation has settled and the list has re-rendered" the actual
  // trigger, which is what the restore always meant.
  useLayoutEffect(() => {
    if (focusIntent === null || pending) return
    setFocusIntent(null)
    if (focusIntent.kind === 'restore') {
      const previous = focusIntent.node
      if (
        previous?.isConnected
        && document.activeElement === document.body
        && !previous.matches(':disabled')
      ) previous.focus()
      return
    }
    focusConnectionTarget(section.current, focusIntent.connectorId ?? null)
  }, [focusIntent, pending])

  async function perform(request, label) {
    if (mutationPending.current) {
      throw new Error('Another connection change is still finishing.')
    }
    const focusTarget = section.current?.contains(document.activeElement)
      ? document.activeElement
      : null
    mutationPending.current = true
    setPending(true)
    setActionError('')
    try {
      const response = await request()
      const data = await jsonOrThrow(response, label)
      await connectorQueries.list.invalidate(queryClient)
      return data
    } finally {
      mutationPending.current = false
      setPending(false)
      setFocusIntent(restoreFocusTo(focusTarget))
    }
  }

  function report(action) {
    action.catch(error => {
      setActionError(error.message || 'Could not update connection')
    })
  }

  function removeConnection(connection) {
    const index = connections.findIndex(item => item.id === connection.id)
    const nextConnection = connections[index + 1] || connections[index - 1]
    report(perform(
      () => api.connectors.remove(connection.id, connection.generation),
      'Could not remove connection',
    ).then(() => {
      setConfirmRemove(null)
      // Queued after perform()'s own restore intent and in the same batch, so
      // the row-walk wins: the control the owner ran has just been removed.
      setFocusIntent(focusConnection(nextConnection?.id ?? null))
    }))
  }

  function closeAddForm() {
    setAddOpen(false)
    setFocusIntent(focusConnection(null))
  }

  const busy = pending
  const hasData = listQuery.data !== undefined

  return (
    <section
      ref={section}
      className="settings__section settings-connections"
      id="settings-connections"
    >
      <h2 className="settings__section-title">Connections</h2>
      <p className="settings__subtext settings__subtext--tight">
        Add a custom MCP endpoint once and make it available to both Claude Code and Codex.
      </p>

      {listQuery.isLoading && !hasData ? (
        <div className="settings__notice" role="status">Loading connections…</div>
      ) : listQuery.isError && !hasData ? (
        <div className="settings__notice" role="alert">
          Could not load connections.{' '}
          <button
            type="button"
            className="settings-connections__text-button"
            disabled={listQuery.isFetching}
            onClick={() => listQuery.refetch()}
          >
            {listQuery.isFetching ? 'Retrying…' : 'Retry'}
          </button>
        </div>
      ) : connections.length === 0 ? (
        <div className="settings__notice" role="status">
          No custom MCP connections yet.
        </div>
      ) : (
        <div className="settings-connections__list">
          {connections.map(connection => {
            const removing = (
              confirmRemove?.id === connection.id
              && confirmRemove.generation === connection.generation
            )
            const unhealthy = connection.status === 'error'
            const endpoint = displayEndpoint(connection.url)
            const statusColor = unhealthy
              ? '--danger'
              : (connection.enabled ? '--green' : '--muted')
            return (
              <article className="settings-connections__item" key={connection.id}>
                <div className="settings-connections__main">
                  <StatusDot color={statusColor}>
                    {connection.name}
                  </StatusDot>
                  <span className="settings-connections__endpoint" title={endpoint}>
                    {endpoint}
                  </span>
                  <span className="settings-connections__meta">
                    {connection.enabled ? 'On' : 'Off'}
                    {' · '}{unhealthy ? 'Needs attention' : 'Reachable'}
                    {' · '}{connection.tool_count} tool{connection.tool_count === 1 ? '' : 's'}
                    {connection.has_auth ? ' · API key saved' : ''}
                  </span>
                  {unhealthy && connection.status_detail && (
                    <span className="settings-connections__error">
                      {connection.status_detail}
                    </span>
                  )}
                </div>

                <div className="settings-connections__actions">
                  {removing ? (
                    <div
                      className="settings__confirm"
                      role="group"
                      aria-label={`Remove ${connection.name}?`}
                    >
                      <button
                        type="button"
                        ref={confirmButton}
                        className="settings__btn settings__btn--sm settings-connections__danger-button"
                        disabled={busy}
                        onClick={() => removeConnection(connection)}
                      >
                        Remove
                      </button>
                      <button
                        type="button"
                        className="settings__btn settings__btn--outline settings__btn--sm"
                        disabled={busy}
                        onClick={() => {
                          setConfirmRemove(null)
                          setFocusIntent(focusConnection(connection.id))
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
                        title="Re-check connection"
                        disabled={busy}
                        onClick={() => report(perform(
                          () => api.connectors.refresh(
                            connection.id,
                            connection.generation,
                          ),
                          'Could not re-check connection',
                        ))}
                      >
                        <ArrowRotateCw aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        className="settings-connections__icon-button settings-connections__icon-button--danger"
                        data-connector-remove={connection.id}
                        aria-label={`Remove ${connection.name}`}
                        title="Remove connection"
                        disabled={busy}
                        onClick={() => {
                          setConfirmRemove({
                            id: connection.id,
                            generation: connection.generation,
                          })
                        }}
                      >
                        <Delete aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={connection.enabled}
                        aria-label={`${connection.name} available to agents`}
                        title={
                          !connection.enabled && unhealthy
                            ? 'Re-check successfully before enabling'
                            : undefined
                        }
                        className={`settings-connections__switch${connection.enabled ? ' settings-connections__switch--on' : ''}`}
                        disabled={busy || (!connection.enabled && unhealthy)}
                        onClick={() => report(perform(
                          () => api.connectors.update(
                            connection.id,
                            connection.generation,
                            { enabled: !connection.enabled },
                          ),
                          'Could not update connection',
                        ))}
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

      {listQuery.isError && hasData && (
        <div className="settings__notice" role="alert">
          The saved list could not be refreshed.{' '}
          <button
            type="button"
            className="settings-connections__text-button"
            disabled={listQuery.isFetching}
            onClick={() => listQuery.refetch()}
          >
            {listQuery.isFetching ? 'Retrying…' : 'Retry'}
          </button>
        </div>
      )}

      {addOpen ? (
        <ConnectorForm
          disabled={busy}
          onAdd={payload => perform(
            () => api.connectors.add(payload, { timeoutMs: 25000 }),
            'Could not add connection',
          )}
          onCancel={closeAddForm}
        />
      ) : (
        <button
          type="button"
          id="settings-connections-add"
          className="settings__btn settings__btn--outline settings__btn--sm settings-connections__add-button"
          disabled={busy}
          onClick={() => setAddOpen(true)}
        >
          Add connection
        </button>
      )}

      {actionError && <Alert color="danger" variant="soft" description={actionError} />}
    </section>
  )
}
