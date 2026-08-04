import { useSyncExternalStore } from 'react'
import {
  Stop,
  TextToSpeech,
  TriangleExclamationErrorWarning,
} from '@openai/apps-sdk-ui/components/Icon'
import {
  chatSpeechSnapshot,
  chatSpeechKey,
  subscribeChatSpeech,
  toggleChatSpeech,
} from './chatSpeechPlayer.js'

export default function MessageSpeakButton({ chatId, messageKey, text }) {
  const speech = useSyncExternalStore(
    subscribeChatSpeech,
    chatSpeechSnapshot,
    chatSpeechSnapshot,
  )
  const key = chatSpeechKey(chatId, messageKey)
  const own = speech.key === key
  const active = own && (speech.phase === 'loading' || speech.phase === 'playing')
  const error = own && speech.phase === 'error'
  const label = active
    ? 'Stop speaking message'
    : error
      ? `Try speaking message again: ${speech.error}`
      : 'Speak message'

  return (
    <button
      type="button"
      className={`chat__msg-speak${active ? ' chat__msg-speak--active' : ''}${error ? ' chat__msg-speak--error' : ''}`}
      onClick={(event) => {
        event.stopPropagation()
        toggleChatSpeech({ chatId, messageKey, text })
      }}
      aria-label={label}
      title={label}
    >
      {active
        ? <Stop width={14} height={14} aria-hidden="true" />
        : error
          ? <TriangleExclamationErrorWarning width={14} height={14} aria-hidden="true" />
          : <TextToSpeech width={14} height={14} aria-hidden="true" />}
    </button>
  )
}
