let active = null
let state = { key: '', phase: 'idle', error: '' }
let stateOwnerChatId = ''
const listeners = new Set()

function abortError() {
  const error = new Error('Speech stopped.')
  error.name = 'AbortError'
  return error
}

function abortableDelay(milliseconds, signal) {
  if (signal.aborted) return Promise.reject(signal.reason || abortError())
  return new Promise((resolve, reject) => {
    const timer = setTimeout(finish, milliseconds)
    function finish() {
      signal.removeEventListener('abort', cancel)
      resolve()
    }
    function cancel() {
      clearTimeout(timer)
      signal.removeEventListener('abort', cancel)
      reject(signal.reason || abortError())
    }
    signal.addEventListener('abort', cancel, { once: true })
  })
}

function publish(next, ownerChatId = '') {
  state = next
  stateOwnerChatId = String(ownerChatId || '')
  for (const listener of listeners) listener()
}

export function subscribeChatSpeech(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function chatSpeechSnapshot() {
  return state
}

export function chatSpeechKey(chatId, messageKey) {
  return JSON.stringify([String(chatId || ''), String(messageKey || '')])
}

export function messageSpeechText(value) {
  return String(value || '')
    .replace(/```[\s\S]*?```/g, ' Code example omitted. ')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s{0,3}(?:#{1,6}|>|[-+*]|\d+[.)])\s+/gm, '')
    .replace(/[*_~]{1,3}/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 50_000)
}

export function stopChatSpeech({ chatId = null, key = null } = {}) {
  const session = active
  if (session && key != null && session.key !== key) return false
  if (session && chatId != null && session.chatId !== String(chatId)) return false
  if (!session && key != null && state.key !== key) return false
  if (!session && chatId != null && stateOwnerChatId !== String(chatId)) return false
  active = null
  if (session && !session.controller.signal.aborted) {
    session.controller.abort(abortError())
  }
  session?.synthesis?.cancel?.()
  for (const source of session?.sources || []) {
    try { source.stop() } catch {}
  }
  session?.sources?.clear()
  if (session?.context && session.context.state !== 'closed') {
    session.context.close().catch(() => {})
  }
  publish({ key: '', phase: 'idle', error: '' })
  return true
}

async function defaultLoadSpeech() {
  const [{ speechModelCatalog }, { synthesizeSpeech }] = await Promise.all([
    import('../../lib/speech/speechModelStore.js'),
    import('../../lib/speech/speechProviderRuntime.js'),
  ])
  return { speechModelCatalog, synthesizeSpeech }
}

export async function toggleChatSpeech(
  { chatId, messageKey, text },
  {
    AudioContext = globalThis.AudioContext || globalThis.webkitAudioContext,
    loadSpeech = defaultLoadSpeech,
  } = {},
) {
  const ownerChatId = String(chatId || '')
  const key = chatSpeechKey(ownerChatId, messageKey)
  if (active?.key === key) {
    stopChatSpeech({ key })
    return
  }
  stopChatSpeech()
  const spoken = messageSpeechText(text)
  if (!spoken) return

  if (!AudioContext) {
    publish(
      { key, phase: 'error', error: 'This browser cannot play generated speech.' },
      ownerChatId,
    )
    return
  }
  const context = new AudioContext()
  const controller = new AbortController()
  const session = {
    key,
    chatId: ownerChatId,
    context,
    controller,
    sources: new Set(),
    synthesis: null,
    nextAt: 0,
  }
  active = session
  publish({ key, phase: 'loading', error: '' }, ownerChatId)

  try {
    const requireOwnership = () => {
      if (active !== session || controller.signal.aborted) {
        throw controller.signal.reason || abortError()
      }
    }
    // Preserve the mobile autoplay gesture before lazy-loading the speech
    // runtime or checking the device model library.
    await context.resume()
    requireOwnership()
    const { speechModelCatalog, synthesizeSpeech } = await loadSpeech()
    requireOwnership()
    const catalog = await speechModelCatalog({ signal: controller.signal })
    requireOwnership()
    const model = catalog.models.find(
      (candidate) => candidate.id === catalog.activeModelId && candidate.state === 'ready',
    )
    if (!model) throw new Error('Open Voice and download a speech model on this device first.')
    session.nextAt = context.currentTime + 0.12
    session.synthesis = synthesizeSpeech({
      text: spoken,
      modelId: model.id,
      signal: controller.signal,
      onAudio(samples) {
        if (active !== session || !(samples instanceof Float32Array) || !samples.length) return
        const buffer = context.createBuffer(1, samples.length, model.sampleRate || 24_000)
        buffer.copyToChannel(samples, 0)
        const source = context.createBufferSource()
        source.buffer = buffer
        source.connect(context.destination)
        session.sources.add(source)
        source.onended = () => session.sources.delete(source)
        const startAt = Math.max(session.nextAt, context.currentTime + 0.08)
        session.nextAt = startAt + buffer.duration
        source.start(startAt)
        publish({ key, phase: 'playing', error: '' }, ownerChatId)
      },
    })
    await session.synthesis.result
    requireOwnership()
    const remainingMs = Math.max(0, session.nextAt - context.currentTime) * 1000
    await abortableDelay(remainingMs + 40, controller.signal)
    if (active === session) stopChatSpeech()
  } catch (error) {
    if (active !== session || error?.name === 'AbortError') return
    active = null
    if (context.state !== 'closed') context.close().catch(() => {})
    publish({
      key,
      phase: 'error',
      error: error?.message || 'This message could not be spoken.',
    }, ownerChatId)
  }
}
