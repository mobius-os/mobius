/**
 * Shell side of the generic mini-app capability session protocol.
 *
 * The host is intentionally ignorant of microphone/camera/files/etc. A
 * provider supplies those semantics; this module owns the invariants every
 * privileged operation needs: exact source binding, reviewed declaration,
 * independent version negotiation, session correlation, cancellation, and
 * lifecycle cleanup.
 */

function errorFields(error, fallbackCode = 'provider_error') {
  const inferredCode = {
    AbortError: 'aborted',
    NotAllowedError: 'denied',
    SecurityError: 'denied',
    NotFoundError: 'unavailable',
    NotSupportedError: 'unavailable',
    InvalidStateError: 'busy',
    NotReadableError: 'busy',
    TypeError: 'invalid_request',
  }[error?.name]
  return {
    code: typeof error?.code === 'string'
      ? error.code
      : inferredCode || fallbackCode,
    name: typeof error?.name === 'string' ? error.name : 'CapabilityError',
    message: typeof error?.message === 'string'
      ? error.message
      : 'Capability request failed.',
  }
}

function providerContention(provider, input, activeInput) {
  if (typeof provider?.contention === 'function') {
    const decision = provider.contention({ input, activeInput })
    if (decision === 'share' || decision === 'replace' || decision === 'reject') {
      return decision
    }
  }
  return provider?.exclusive ? 'reject' : 'share'
}

export function createCapabilityHost({
  providers,
  getDeclaration,
  isActive,
  send,
}) {
  const sessions = new Map()

  function post(session, type, fields = {}, transfer = []) {
    const message = {
      type,
      requestId: session.requestId,
      capability: session.capability,
      ...fields,
    }
    send(session.source, message, transfer)
  }

  function current(session) {
    return sessions.get(session.requestId) === session && !session.settled
  }

  function settle(session, type, fields, transfer) {
    if (!current(session)) return
    session.settled = true
    sessions.delete(session.requestId)
    post(session, type, fields, transfer)
  }

  function fail(session, error, fallbackCode) {
    settle(
      session,
      'moebius:capability-error',
      errorFields(error, fallbackCode),
    )
  }

  function runControl(session, action, value) {
    if (!current(session)) return
    if (!session.control) {
      session.pendingControls.push({ action, value })
      return
    }
    try {
      session.control.control?.(action, value)
    } catch (error) {
      fail(session, error)
    }
  }

  function open(source, msg) {
    // Never truncate a correlation id: the runtime could not match any reply
    // carrying the shortened value, leaving its session pending forever.
    const requestId = typeof msg.requestId === 'string' && msg.requestId.length <= 120
      ? msg.requestId
      : ''
    const capability = typeof msg.capability === 'string' && msg.capability.length <= 160
      ? msg.capability
      : ''
    if (!requestId || !capability) return true

    const shellSession = {
      requestId,
      capability,
      input: msg.input,
      source,
      settled: false,
      control: null,
      pendingControls: [],
    }
    const rejectOpen = (code, message, name = 'CapabilityError') => {
      post(shellSession, 'moebius:capability-error', { code, name, message })
    }

    if (!isActive()) {
      rejectOpen('not_active', 'Capabilities are available only to the visible app.')
      return true
    }
    if (sessions.has(requestId)) {
      rejectOpen('duplicate_request', 'This capability request id is already active.')
      return true
    }

    const declaration = getDeclaration(capability)
    if (!declaration) {
      rejectOpen('undeclared', `Capability \`${capability}\` is not declared by this app.`)
      return true
    }
    const provider = providers[capability]
    if (!provider) {
      rejectOpen('unavailable', `Capability \`${capability}\` is unavailable in this host.`)
      return true
    }
    if (msg.version !== declaration.version || msg.version !== provider.version) {
      rejectOpen('version_mismatch', `Capability \`${capability}\` version is not supported.`)
      return true
    }
    if (!msg.input || typeof msg.input !== 'object' || Array.isArray(msg.input)) {
      rejectOpen('invalid_request', 'Capability input must be an object.', 'TypeError')
      return true
    }
    const contentions = [...sessions.values()]
      .filter((candidate) => candidate.capability === capability)
      .map((candidate) => ({
        candidate,
        decision: providerContention(provider, msg.input, candidate.input),
      }))
    if (contentions.some(({ decision }) => decision === 'reject')) {
      rejectOpen('busy', `Capability \`${capability}\` is already in use.`, 'InvalidStateError')
      return true
    }
    for (const { candidate, decision } of contentions) {
      if (decision === 'replace') {
        abortSession(
          candidate,
          'Another request took over this capability. You can resume the earlier action when ready.',
          'superseded',
        )
      }
    }
    sessions.set(requestId, shellSession)
    const channel = {
      ready(value) {
        if (current(shellSession)) {
          post(shellSession, 'moebius:capability-ready', { value })
        }
      },
      event(event, value, transfer = []) {
        if (current(shellSession) && typeof event === 'string') {
          post(shellSession, 'moebius:capability-event', { event, value }, transfer)
        }
      },
      result(value, transfer = []) {
        settle(shellSession, 'moebius:capability-result', { value }, transfer)
      },
      error(error) { fail(shellSession, error) },
    }

    Promise.resolve().then(() => provider.open({
      input: msg.input,
      declaration,
      channel,
    })).then((control) => {
      if (!current(shellSession)) {
        try { control?.control?.('cancel') } catch {}
        return
      }
      shellSession.control = control || {}
      const queued = shellSession.pendingControls.splice(0)
      for (const item of queued) {
        if (!current(shellSession)) break
        runControl(shellSession, item.action, item.value)
      }
    }).catch((error) => fail(shellSession, error))
    return true
  }

  function control(source, msg) {
    const session = sessions.get(msg.requestId)
    if (!session || session.source !== source || session.capability !== msg.capability) {
      return true
    }
    const action = typeof msg.action === 'string' ? msg.action.slice(0, 80) : ''
    if (!action) return true
    runControl(session, action, msg.value)
    return true
  }

  function abortSession(session, message, code = 'aborted') {
    if (!current(session)) return
    runControl(session, 'cancel')
    fail(session, {
      code,
      name: 'AbortError',
      message,
    })
  }

  return {
    handle(source, msg) {
      if (!msg || typeof msg !== 'object') return false
      if (msg.type === 'moebius:capability-open') return open(source, msg)
      if (msg.type === 'moebius:capability-control') return control(source, msg)
      return false
    },
    deactivate() {
      for (const session of [...sessions.values()]) {
        const declaration = getDeclaration(session.capability)
        const provider = providers[session.capability]
        const action = provider?.onDeactivate
          || (declaration?.lifecycle === 'active_frame' ? 'cancel' : null)
        if (action) runControl(session, action)
      }
    },
    detachSource(source) {
      for (const session of [...sessions.values()]) {
        if (session.source === source) {
          abortSession(session, 'The app frame that opened this capability was detached.')
        }
      }
    },
    reconcile() {
      for (const session of [...sessions.values()]) {
        const declaration = getDeclaration(session.capability)
        const provider = providers[session.capability]
        if (!declaration || !provider || declaration.version !== provider.version) {
          abortSession(session, 'The app capability contract changed.')
        }
      }
    },
    destroy() {
      for (const session of [...sessions.values()]) {
        abortSession(session, 'Capability host was detached.')
      }
    },
    activeCount: () => sessions.size,
  }
}
