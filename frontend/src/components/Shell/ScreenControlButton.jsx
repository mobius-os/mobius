/* Explicit owner affordance for sharing the current Möbius screen with this chat's agent. */
import { useEffect, useRef, useState } from 'react'
import { ShareScreen, ShareScreenFilled } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import {
  createScreenControlClient,
  requestCurrentTabCapture,
} from '../../lib/screenControlPage.js'

export default function ScreenControlButton({ chatId, onNotice }) {
  const [phase, setPhase] = useState('idle')
  const clientRef = useRef(null)

  useEffect(() => () => {
    void clientRef.current?.stop?.()
    clientRef.current = null
  }, [])

  async function stopControl() {
    if (phase !== 'active') return
    setPhase('stopping')
    const client = clientRef.current
    clientRef.current = null
    await client?.stop?.()
    setPhase('idle')
    onNotice?.('Agent control stopped')
  }

  async function startControl() {
    if (!chatId) {
      onNotice?.('Open the chat whose agent should control this screen', { variant: 'error' })
      return
    }
    setPhase('starting')
    let capture
    try {
      capture = await requestCurrentTabCapture()
      const session = await api.screenControl.start({
        chatId: String(chatId),
        route: `${location.pathname}${location.search}${location.hash}`,
        viewport: {
          width: Math.round(window.innerWidth),
          height: Math.round(window.innerHeight),
          pixelRatio: window.devicePixelRatio || 1,
        },
      })
      const client = createScreenControlClient({
        sessionId: session.sessionId,
        capture,
        onEnded: (reason, error) => {
          clientRef.current = null
          setPhase('idle')
          onNotice?.(
            reason === 'disconnected'
              ? (error?.message || 'Agent control disconnected')
              : 'Agent control stopped',
            reason === 'disconnected' ? { variant: 'error' } : undefined,
          )
        },
      })
      clientRef.current = client
      setPhase('active')
      onNotice?.('Agent can now inspect and control this screen for 15 minutes')
    } catch (error) {
      capture?.stream?.getTracks?.().forEach(track => track.stop())
      setPhase('idle')
      if (error?.name === 'NotAllowedError') {
        onNotice?.('Screen sharing was cancelled', { variant: 'error' })
      } else {
        onNotice?.(error?.message || 'Could not share this screen', { variant: 'error' })
      }
    }
  }

  const active = phase === 'active'
  const pending = phase === 'starting' || phase === 'stopping'
  const Icon = active ? ShareScreenFilled : ShareScreen
  const label = active ? 'Stop agent control' : 'Let agent control this screen'
  return (
    <button
      type="button"
      className={`screen-control-button${active ? ' screen-control-button--active' : ''}`}
      aria-label={label}
      aria-pressed={active}
      title={label}
      disabled={pending}
      onClick={active ? stopControl : startControl}
    >
      <Icon width={20} height={20} aria-hidden="true" />
      <span className="screen-control-button__label">
        {pending ? (phase === 'starting' ? 'Sharing…' : 'Stopping…') : (active ? 'Agent control' : 'Share screen')}
      </span>
      {active && <span className="screen-control-button__live" aria-hidden="true" />}
    </button>
  )
}
