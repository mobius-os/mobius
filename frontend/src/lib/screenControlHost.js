import { api } from '../api/client.js'
import {
  createScreenControlClient,
  requestCurrentTabCapture,
} from './screenControlPage.js'

export const SCREEN_CONTROL = 'workspace.screen-control'

const GLOBAL_KEY = Symbol.for('mobius.screen-control-host.v1')
const IDLE_STATE = Object.freeze({ phase: 'idle' })

function hostStore() {
  if (!globalThis[GLOBAL_KEY]) {
    globalThis[GLOBAL_KEY] = {
      state: IDLE_STATE,
      current: null,
      listeners: new Set(),
    }
  }
  return globalThis[GLOBAL_KEY]
}

function publish(next) {
  const store = hostStore()
  store.state = next
  for (const listener of store.listeners) listener()
}

export function getScreenControlState() {
  return hostStore().state
}

export function subscribeScreenControlState(listener) {
  const store = hostStore()
  store.listeners.add(listener)
  return () => store.listeners.delete(listener)
}

export async function stopActiveScreenControl() {
  await hostStore().current?.finish?.('owner')
}

function capabilityError(name, message, code) {
  const error = new Error(message)
  error.name = name
  if (code) error.code = code
  return error
}

function validatedChatId(value) {
  const chatId = typeof value === 'string' ? value.trim() : ''
  if (!chatId || chatId.length > 128) {
    throw capabilityError('TypeError', 'Open the support chat before sharing.', 'invalid_request')
  }
  return chatId
}

export function createScreenControlProvider({
  appId,
  requestCapture = requestCurrentTabCapture,
  startSession = (payload) => api.screenControl.start(payload),
  stopSession = (sessionId) => api.screenControl.stop(sessionId),
  makeClient = createScreenControlClient,
} = {}) {
  return {
    version: 1,
    exclusive: true,
    // A shell Fast Refresh or app-frame replacement must not revoke an active
    // browser grant. The global owner-visible stop control survives, and the
    // app reattaches its same app-owned chat after remount.
    preserveOnDetach: true,
    async open({ input, channel }) {
      const store = hostStore()
      if (!Number.isInteger(appId) || appId < 1) {
        throw capabilityError('TypeError', 'The control app identity is unavailable.', 'invalid_request')
      }
      const chatId = validatedChatId(input?.chatId)
      if (store.current) {
        if (input?.resume === true
            && store.current.appId === appId
            && store.current.chatId === chatId) {
          store.current.channel = channel
          channel.ready({ expiresAt: store.current.expiresAt })
          return {
            control(action) {
              if (action === 'detach') {
                if (store.current?.channel === channel) store.current.channel = null
              } else if (action === 'finish' || action === 'cancel') {
                void store.current?.finish?.('owner')
              }
            },
          }
        }
        throw capabilityError(
          'InvalidStateError',
          'Another screen-control session is already active.',
          'busy',
        )
      }
      if (input?.resume === true) {
        throw capabilityError('NotFoundError', 'No active screen-control session.', 'unavailable')
      }

      // This must be the first awaited browser operation. A click inside the
      // opaque app frame activates every ancestor Window, but getDisplayMedia
      // still rejects after that transient activation is consumed.
      const capture = await requestCapture()
      let session
      let client
      let finishing = null
      const current = {
        appId,
        chatId,
        channel,
        client: null,
        expiresAt: null,
        finish: null,
      }

      const finish = (reason, error) => {
        if (finishing) return finishing
        finishing = (async () => {
          await current.client?.stop?.()
          if (hostStore().current === current) hostStore().current = null
          publish(IDLE_STATE)
          if (error) current.channel?.error(error)
          else current.channel?.result({ reason })
          current.channel = null
        })()
        return finishing
      }
      current.finish = finish

      try {
        const page = globalThis.location
        const view = globalThis.window
        session = await startSession({
          appId,
          chatId,
          route: page ? `${page.pathname}${page.search}${page.hash}` : '/',
          viewport: {
            width: Math.round(view?.innerWidth || 1),
            height: Math.round(view?.innerHeight || 1),
            pixelRatio: view?.devicePixelRatio || 1,
          },
        })
        current.expiresAt = session.expiresAt
        client = makeClient({
          sessionId: session.sessionId,
          capture,
          onConnected() {
            if (hostStore().current !== current) return
            publish({
              phase: 'active',
              appId,
              chatId,
              expiresAt: session.expiresAt,
            })
            current.channel?.ready({ expiresAt: session.expiresAt })
          },
          onEnded(reason, error) {
            if (hostStore().current !== current) return
            void finish(
              reason === 'disconnected' ? 'disconnected' : 'stopped',
              reason === 'disconnected'
                ? (error || capabilityError(
                  'NetworkError', 'Agent control disconnected. Start a new session to continue.',
                ))
                : null,
            )
          },
        })
        current.client = client
        store.current = current
        publish({ phase: 'starting', appId, chatId })
      } catch (error) {
        capture.stream.getTracks().forEach(track => track.stop())
        if (session?.sessionId) void stopSession(session.sessionId).catch(() => {})
        if (hostStore().current === current) hostStore().current = null
        publish(IDLE_STATE)
        throw error
      }

      return {
        control(action) {
          if (action === 'detach') {
            if (current.channel === channel) current.channel = null
          } else if (action === 'finish' || action === 'cancel') {
            void finish('owner')
          }
        },
      }
    },
  }
}
