// In-repo fixture for tests/app-blocks.spec.mjs. It is a minimal passive
// block-session app: it listens on window directly (as a real app may),
// records every block message that reaches it, and answers with bounded state.
import { useEffect, useLayoutEffect, useRef, useState } from 'react'

export const appBlockSessions = true

// Twelve targets, so the list runs well below Confirm on a phone-sized screen.
const CONFIRMATION = Array.from({ length: 12 }, (_, i) => ({
  title: i ? `fixture/repo: Change ${i + 1}` : 'fixture/repo: Change one', facts: [{ label: 'Version', value: `abc12${i}` }] }))

// For the `swap:` action: one target first, then 256 that push the list end
// far below the fold, as an app that refreshes its list after asking may.
const ONE = [{ title: 'fixture/repo: Only change', facts: [{ label: 'Version', value: 'abc' }] }]
const MANY = Array.from({ length: 256 }, (_, i) => ({ title: `fixture/repo: Swapped ${i + 1}`, facts: [] }))
const listFor = (key, swapped) => !key.startsWith('swap:') ? CONFIRMATION : swapped ? MANY : ONE

function Session({ blockSession }) {
  const [log, setLog] = useState([])
  const owner = useRef({ confirming: null, sent: 0, ack: null, swapped: false })
  useLayoutEffect(() => {
    const publish = () => {
      const state = owner.current
      window.parent.postMessage({
        // Every report names this document's generation; the host drops others.
        type: 'moebius:app-block-state', sessionId: blockSession.sessionId, generation: blockSession.generation,
        ackNonce: state.ack,
        retain: Boolean(state.confirming), summary: `Sent ${state.sent}`,
        actions: blockSession.actions.map(action => ({
          key: action.key, label: action.label, status: state.sent ? 'Sent' : 'Ready',
          confirming: state.confirming === action.key,
          ...(state.confirming === action.key ? { confirmation: listFor(action.key, state.swapped) } : {}),
        })),
      }, window.location.origin)
    }
    const onMessage = event => {
      if (event.source !== window.parent) return
      const data = event.data
      if (data?.type !== 'moebius:app-block-init' && data?.type !== 'moebius:app-block-action') return
      setLog(lines => [...lines, [data.type, data.sessionId, data.generation, data.event || ''].join(' ')])
      if (data.type === 'moebius:app-block-action') {
        const state = owner.current
        if (data.nonce === state.ack) return
        state.ack = data.nonce
        if (data.event === 'activate') {
          state.confirming = data.key
          state.swapped = false
          // The swap arrives after the one-item list has had time to arm.
          if (data.key.startsWith('swap:')) setTimeout(() => { state.swapped = true; publish() }, 1500)
        }
        else if (data.event === 'cancel') state.confirming = null
        else if (data.event === 'confirm') { state.confirming = null; state.sent += 1 }
      }
      publish()
    }
    window.addEventListener('message', onMessage)
    publish()
    // A hidden document has no owner gesture behind it; the host must refuse.
    window.parent.postMessage({ type: 'moebius:open-chat', chatId: 'fixture-hidden-navigation' }, window.location.origin)
    return () => window.removeEventListener('message', onMessage)
  }, [blockSession])
  return <pre id="block-log" data-session={blockSession.sessionId} data-generation={blockSession.generation}>{log.join('\n')}</pre>
}

function View() {
  const [intent, setIntent] = useState('')
  useEffect(() => {
    const onMessage = event => {
      if (event.source === window.parent && event.data?.type === 'moebius:app-intent') setIntent(event.data.intent)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])
  return <main><h1 id="fixture-view">Fixture view</h1><p id="fixture-intent">{intent}</p></main>
}

export default function App({ blockSession = null }) {
  return blockSession ? <Session blockSession={blockSession} /> : <View />
}
