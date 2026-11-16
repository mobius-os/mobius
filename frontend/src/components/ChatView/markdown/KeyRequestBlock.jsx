/* A `mobius-key-request` block in an assistant reply: the live key request card, in the chat whose agent asked. See secure-input.md. */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { isSharedBrowserAuth } from '../../../api/client.js'
import KeyRequest from '../../SettingsView/KeyRequest.jsx'
import { KEYS_QUERY, useAppNames, useKeys } from '../../SettingsView/keysQueries.js'
import { keyAnswerWords, requestAppPaused, requestFormKey } from '../../../lib/keysSettingsText.js'
import { KEY_REQUEST_FENCE, chatKeyRequestView, keyAnswerAsk } from '../../../lib/keyRequestPlaces.js'
import { settingsDeepLink } from '../../../lib/settingsSections.js'
import { sharedBrowserShellHref } from '../../../lib/sharedBrowserWorkspace.js'
import { openInternalLink } from '../internalLink.js'
import { useAppBlockChat } from './appBlockChat.js'
import '../../SettingsView/KeyRequestPlace.css'

function SettingsKeysLink({ onInternalNav, children }) {
  const href = sharedBrowserShellHref(settingsDeepLink('keys'))
  return (
    <a className="key-request-place__link" href={href} onClick={event => openInternalLink(event, href, onInternalNav)}>
      {children}
    </a>
  )
}

/**
 * Only the owner's own browser answers, through the owner-only routes; the
 * pasted value goes to the approve route alone, never into the chat. After
 * an answer here the card posts one ordinary owner message ("Allowed: …" or
 * "Not now: …") through this chat's send path, so the asking agent resumes.
 */
export default function KeyRequestBlock({ block, onInternalNav }) {
  const chat = useAppBlockChat(`${KEY_REQUEST_FENCE}\n${block.id}`)
  const owner = !isSharedBrowserAuth()
  const keysQuery = useKeys({ enabled: owner })
  const { names } = useAppNames()
  const queryClient = useQueryClient()
  const [answered, setAnswered] = useState(null)
  const [toldAgent, setToldAgent] = useState(true)
  const view = chatKeyRequestView({
    id: block.id,
    chatId: chat.context?.chatId ?? null,
    owner,
    keys: { data: keysQuery.data, error: keysQuery.error },
    answered,
    latest: chat.context?.latest ?? true,
  })

  async function onAnswered(kind) {
    const request = view.request
    setAnswered({ request, kind })
    void queryClient.invalidateQueries({ queryKey: KEYS_QUERY })
    const sent = await chat.ask({ message: keyAnswerWords(request, kind, names).message, ...keyAnswerAsk(request, kind) })
    setToldAgent(sent)
  }

  let body
  switch (view.state) {
    case 'pending':
      body = (
        <KeyRequest
          key={requestFormKey(view.request)}
          request={{ ...view.request, app_paused: requestAppPaused(keysQuery.data?.keys, view.request) }}
          names={names}
          onAnswered={onAnswered}
        />
      )
      break
    case 'elsewhere':
      body = (
        <>
          <KeyRequest request={view.request} names={names} readOnly />
          <p className="key-request-place__note">
            Another chat asked for this. Answer it in <SettingsKeysLink onInternalNav={onInternalNav}>Settings › Keys</SettingsKeysLink>.
          </p>
        </>
      )
      break
    case 'answered':
      body = (
        <>
          <p className="key-request-place__line" role="status">{keyAnswerWords(view.request, view.kind, names).line}</p>
          {!toldAgent && (
            <p className="key-request-place__note">Couldn't tell your agent. Send it a message to carry on.</p>
          )}
        </>
      )
      break
    case 'denied':
      body = <p className="key-request-place__line">{keyAnswerWords(view.request, 'deny', names).line}</p>
      break
    case 'repeated':
      body = (
        <p className="key-request-place__line">
          This request is shown again further down.{' '}
          <button
            type="button"
            className="key-request-place__link"
            onClick={() => chat.newestNode()?.scrollIntoView({ block: 'center', behavior: 'smooth' })}
          >
            Answer it there
          </button>
        </p>
      )
      break
    case 'guest':
      body = <p className="key-request-place__line">Your agent asked for a key. Only the owner of this Möbius can answer.</p>
      break
    case 'loading':
      body = <p className="key-request-place__line" aria-busy="true">Loading the key request…</p>
      break
    case 'unavailable':
      body = <p className="key-request-place__line">The key vault isn't running. It starts with Möbius; a restart usually brings it back.</p>
      break
    case 'error':
      body = (
        <p className="key-request-place__line">
          Couldn't load this key request. It's also in <SettingsKeysLink onInternalNav={onInternalNav}>Settings › Keys</SettingsKeysLink>.
        </p>
      )
      break
    default:
      body = <p className="key-request-place__line">This request was answered or withdrawn.</p>
  }
  return (
    <section ref={chat.ref} className="key-request-place key-request-place--chat" aria-label="Key request">
      {body}
    </section>
  )
}
