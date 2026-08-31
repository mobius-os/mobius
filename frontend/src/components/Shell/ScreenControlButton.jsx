/* Active-session safety control. Starting screen control belongs to the app. */
import { useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { ShareScreenFilled } from '@openai/apps-sdk-ui/components/Icon'
import {
  getScreenControlState,
  stopActiveScreenControl,
  subscribeScreenControlState,
} from '../../lib/screenControlHost.js'

export default function ScreenControlButton() {
  const state = useSyncExternalStore(
    subscribeScreenControlState,
    getScreenControlState,
    getScreenControlState,
  )
  if (state.phase === 'idle' || typeof document === 'undefined') return null
  const active = state.phase === 'active'
  return createPortal(
    <button
      type="button"
      className="screen-control-button screen-control-button--active"
      aria-label={active ? 'Stop agent control' : 'Agent control is starting'}
      title={active ? 'Stop agent control' : 'Agent control is starting'}
      disabled={!active}
      onClick={() => { void stopActiveScreenControl() }}
    >
      <ShareScreenFilled width={18} height={18} aria-hidden="true" />
      <span>{active ? 'Agent control active' : 'Starting agent control\u2026'}</span>
      <span className="screen-control-button__live" aria-hidden="true" />
      {active && <span className="screen-control-button__stop">Stop</span>}
    </button>,
    document.body,
  )
}
