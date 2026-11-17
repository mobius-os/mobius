/*
 * The key-request part of a saved question card (a card whose block carries
 * `key_request`, saved by the agent's request_key control; see
 * backend/app/key_request_cards.py). The card itself is the ordinary
 * QuestionCard: this hook reads the live request from the vault and answers
 * it through the owner-only key routes (useKeyAnswer, as every key card
 * does) before the card's answer is submitted. A pasted value goes only to
 * the approve route, never into the card answer.
 */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { isSharedBrowserAuth } from '../../api/client.js'
import { KEYS_QUERY, useAppNames, useKeys } from '../SettingsView/keysQueries.js'
import { useKeyAnswer } from '../SettingsView/KeyRequest.jsx'
import { KEY_ALLOW, KEY_DENY, keyAnswerAlreadyGiven, keyRequestCardState } from './keyRequestCardState.js'

export { KEY_ALLOW, KEY_DENY }

// Why an answer can't be given on a card in this state (the server's own reasons).
const KEY_STATE_ERRORS = {
  denied: 'You already said Not now to this request.',
  allowed: 'Your agents may already use this key there. Choose Allow.',
  gone: 'Nothing was allowed for this request. Choose Not now.',
}

/** Null for an ordinary card; else the key card's live state and answer. */
export default function useKeyRequestCard(keyRequest, { live }) {
  const owner = !isSharedBrowserAuth()
  const keysQuery = useKeys({ enabled: Boolean(keyRequest) && live && owner })
  const { names } = useAppNames()
  const queryClient = useQueryClient()
  const [settled, setSettled] = useState(null)
  // Allow on another app's key waits behind "Use <app>'s key anyway".
  const [anyway, setAnyway] = useState(false)
  const view = keyRequestCardState({
    id: keyRequest?.id, key: keyRequest?.key, sites: keyRequest?.sites,
    owner, live, keys: { data: keysQuery.data, error: keysQuery.error },
  })
  const answer = useKeyAnswer(view.state === 'pending' ? view.request : null, names)
  if (!keyRequest) return null
  const needsValue = Boolean(answer.words?.needsValue)

  return {
    ...view,
    answer,
    words: answer.words,
    guard: answer.guard,
    needsValue,
    /** Whether Allow stays out of the choices until the owner opens "anyway". */
    hidesAllow: Boolean(answer.words?.allowHidden) && !anyway,
    showAllow: () => setAnyway(true),
    /** Whether choosing ``kind`` leaves anything still to fill in. */
    ready: kind => kind !== KEY_ALLOW || !needsValue || Boolean(answer.value),
    /** Answer the vault (the owner's own input) for ``kind``; throws to keep the card open. */
    async settle(kind) {
      if (settled === kind) return
      const given = keyAnswerAlreadyGiven(view.state, kind)
      if (given === null) {
        throw new Error(KEY_STATE_ERRORS[view.state] || 'Couldn’t reach the key vault. Try again in a moment.')
      }
      if (!given) await answer.send(kind)
      setSettled(kind)
      void queryClient.invalidateQueries({ queryKey: KEYS_QUERY })
    },
  }
}
