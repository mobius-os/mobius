const SPEECH_CAPABILITY = 'media.speech'

export class CapabilityError extends Error {
  constructor(code, message, fields = {}) {
    super(message || 'Capability request failed.')
    this.name = fields.name || 'CapabilityError'
    this.code = code || 'provider_error'
    this.capability = fields.capability
  }
}

function capabilityRequestId() {
  return globalThis.crypto?.randomUUID?.()
    || `cap-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function asCapabilityError(msg, capability) {
  return new CapabilityError(
    typeof msg?.code === 'string' ? msg.code : 'provider_error',
    typeof msg?.message === 'string' ? msg.message : 'Capability request failed.',
    { name: typeof msg?.name === 'string' ? msg.name : 'CapabilityError', capability },
  )
}

export function makeCapabilities({ declarations = {}, hostWindow, selfWindow } = {}) {
  const ownWindow = selfWindow || globalThis.window
  const parentWindow = hostWindow || ownWindow?.parent
  const embedded = !!ownWindow && parentWindow && parentWindow !== ownWindow
  const sessions = new Map()
  let currentDeclarations = declarations && typeof declarations === 'object' ? declarations : {}

  function describe(name) {
    const value = currentDeclarations?.[name]
    return value && typeof value === 'object' ? value : null
  }

  function available(name, version) {
    const declaration = describe(name)
    if (!declaration) return false
    if (version !== undefined && declaration.version !== version) return false
    return embedded
  }

  function settle(session, mode, value) {
    if (!session || session.settled) return
    session.settled = true
    sessions.delete(session.requestId)
    if (mode === 'result') {
      if (!session.readySettled) {
        session.readySettled = true
        session.readyResolve(undefined)
      }
      session.resultResolve(value)
    } else {
      const error = value instanceof Error
        ? value
        : asCapabilityError(value, session.capability)
      if (!session.readySettled) {
        session.readySettled = true
        session.readyReject(error)
      }
      session.resultReject(error)
    }
  }

  function onMessage(event) {
    if (!embedded || event.source !== parentWindow || event.origin !== ownWindow.location.origin) return
    const msg = event.data
    if (!msg || typeof msg !== 'object' || typeof msg.requestId !== 'string') return
    const session = sessions.get(msg.requestId)
    if (!session || msg.capability !== session.capability) return
    if (msg.type === 'moebius:capability-ready') {
      if (session.readySettled || session.settled) return
      session.readySettled = true
      session.readyResolve(msg.value)
    } else if (msg.type === 'moebius:capability-event') {
      if (session.settled || typeof msg.event !== 'string') return
      for (const listener of session.listeners.get(msg.event) || []) {
        try { listener(msg.value) } catch (e) {}
      }
    } else if (msg.type === 'moebius:capability-result') {
      settle(session, 'result', msg.value)
    } else if (msg.type === 'moebius:capability-error') {
      settle(session, 'error', msg)
    }
  }
  ownWindow?.addEventListener?.('message', onMessage)

  function open(capability, input = {}) {
    const declaration = describe(capability)
    if (!declaration) {
      throw new CapabilityError('undeclared', `Capability \`${capability}\` is not declared by this app.`, { capability })
    }
    if (!available(capability, declaration.version)) {
      throw new CapabilityError('unavailable', `Capability \`${capability}\` is unavailable in this host.`, { capability })
    }
    if (!input || typeof input !== 'object' || Array.isArray(input)) {
      throw new CapabilityError('invalid_request', 'Capability input must be an object.', { capability })
    }

    const requestId = capabilityRequestId()
    let readyResolve
    let readyReject
    let resultResolve
    let resultReject
    const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject })
    const result = new Promise((resolve, reject) => { resultResolve = resolve; resultReject = reject })
    ready.catch(() => {})
    result.catch(() => {})
    const internal = {
      requestId, capability, declaration, listeners: new Map(), settled: false,
      readySettled: false, readyResolve, readyReject, resultResolve, resultReject,
      localControl: null,
    }
    sessions.set(requestId, internal)

    const session = {
      capability,
      ready,
      result,
      on(event, listener) {
        if (typeof event !== 'string' || typeof listener !== 'function') return () => {}
        let group = internal.listeners.get(event)
        if (!group) { group = new Set(); internal.listeners.set(event, group) }
        group.add(listener)
        return () => group.delete(listener)
      },
      control(action, value) {
        if (internal.settled || typeof action !== 'string') return result
        if (internal.localControl) return result
        parentWindow.postMessage({
          type: 'moebius:capability-control', requestId, capability, action, value,
        }, ownWindow.location.origin)
        return result
      },
      finish() { return this.control('finish') },
      cancel() {
        if (internal.settled) return
        if (internal.localControl) {
          internal.localControl.cancel()
          return
        }
        parentWindow.postMessage({
          type: 'moebius:capability-control', requestId, capability, action: 'cancel',
        }, ownWindow.location.origin)
        settle(internal, 'error', new CapabilityError('aborted', 'Capability request cancelled.', {
          name: 'AbortError', capability,
        }))
      },
    }

    // `media.speech` synthesis is answered here rather than by the shell: a
    // worker runs under the policy of the document that spawned it, and only
    // this frame's policy reliably permits WebAssembly. The shell still owns
    // the model and streams it over a `model-stream` request on the same
    // capability, so this needs no extra permission and no app-side engine.
    if (capability === SPEECH_CAPABILITY && (input.operation || 'synthesize') === 'synthesize') {
      runSpeechInFrame({ internal, input, declaration, open })
      return session
    }

    parentWindow.postMessage({
      type: 'moebius:capability-open', requestId, capability,
      version: declaration.version, input,
    }, ownWindow.location.origin)
    return session
  }

  function runSpeechInFrame({ internal, input, declaration, open: openBridge }) {
    // A shell older than this runtime does not serve `model-stream`; it still
    // synthesises itself. The two update on different channels, so fall back to
    // asking it rather than failing on a protocol the host has not learned yet.
    let usedFallback = false
    const fallbackToShell = () => {
      if (usedFallback || internal.settled) return
      usedFallback = true
      internal.localControl?.cancel?.()
      internal.localControl = null
      parentWindow.postMessage({
        type: 'moebius:capability-open',
        requestId: internal.requestId,
        capability: internal.capability,
        version: declaration.version,
        input,
      }, ownWindow.location.origin)
    }

    const emit = (event, value) => {
      if (internal.settled) return
      for (const listener of internal.listeners.get(event) || []) {
        try { listener(value) } catch { /* an app listener must not break speech */ }
      }
    }
    import('./speech.js').then(({ synthesizeInFrame }) => {
      if (internal.settled) return
      const engine = synthesizeInFrame({
        input,
        maxTextChars: Number(declaration?.limits?.max_text_chars) || 50_000,
        channel: { event: emit },
        openModelStream({ modelId, engineId, onManifest, onChunk, onProgress, onComplete, onError }) {
          const stream = openBridge(SPEECH_CAPABILITY, { operation: 'model-stream', modelId, engineId })
          stream.on('manifest', onManifest)
          stream.on('chunk', onChunk)
          stream.on('progress', onProgress)
          stream.result.then(onComplete).catch((error) => {
            if (error?.code === 'invalid_request') fallbackToShell()
            else onError(error)
          })
          return stream
        },
      })
      internal.localControl = engine
      if (!internal.readySettled) {
        internal.readySettled = true
        internal.readyResolve({ state: 'starting' })
      }
      engine.result.then(
        (value) => settle(internal, 'result', value),
        (error) => { if (!usedFallback) settle(internal, 'error', error) },
      )
    }).catch((error) => settle(internal, 'error', error))
  }

  return {
    available,
    describe,
    list: () => Object.keys(currentDeclarations).sort(),
    open,
    async invoke(capability, input = {}, { signal } = {}) {
      const session = open(capability, input)
      if (signal) {
        if (signal.aborted) session.cancel()
        else signal.addEventListener('abort', () => session.cancel(), { once: true })
      }
      return session.result
    },
    _updateDeclarations(next) {
      currentDeclarations = next && typeof next === 'object' ? next : {}
    },
    _destroy() {
      ownWindow?.removeEventListener?.('message', onMessage)
      for (const session of sessions.values()) {
        settle(session, 'error', new CapabilityError('aborted', 'Capability host was detached.', {
          name: 'AbortError', capability: session.capability,
        }))
      }
    },
  }
}
