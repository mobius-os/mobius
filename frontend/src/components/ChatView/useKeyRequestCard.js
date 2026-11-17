/*
 * The key-request part of a saved question card (a card whose block carries
 * `key_request`, saved by the agent's request_key control; see
 * backend/app/key_request_cards.py). The card itself is the ordinary
 * QuestionCard: this hook reads the live request from the vault and answers
 * it through the owner-only key routes before the card's answer is submitted.
 * A pasted value goes only to the approve route, never into the card answer.
 */
import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, isSharedBrowserAuth, jsonOrThrow } from '../../api/client.js'
import { KEYS_QUERY, useAppNames, useKeys } from '../SettingsView/keysQueries.js'
import useKeyAnswerGuard from '../SettingsView/useKeyAnswerGuard.js'
import { requestAppPaused, requestWording } from '../../lib/keysSettingsText.js'
import { KEY_ALLOW, KEY_DENY, keyAnswerAlreadyGiven, keyRequestCardState } from './keyRequestCardState.js'

export { KEY_ALLOW, KEY_DENY }

const BEARER = { type: 'header', name: 'Authorization', prefix: 'Bearer ' }
// Why an answer can't be given on a card in this state.
const KEY_STATE_ERRORS = {
  denied: 'You already said Not now to this request.',
}

/** Null for an ordinary card; else the key card's live state and answer. */
export default function useKeyRequestCard(keyRequest, { live }) {
  const owner = !isSharedBrowserAuth()
  const enabled = Boolean(keyRequest) && live && owner
  const keysQuery = useKeys({ enabled })
  const { names } = useAppNames()
  const queryClient = useQueryClient()
  const [value, setValue] = useState('')
  const [settled, setSettled] = useState(null)
  const view = keyRequestCardState({
    id: keyRequest?.id, sites: keyRequest?.sites ?? null,
    owner, live, keys: { data: keysQuery.data, error: keysQuery.error },
  })
  const guard = useKeyAnswerGuard({ enabled: enabled && view.state === 'pending' })
  if (!keyRequest) return null
  const request = view.request
    ? { ...view.request, app_paused: requestAppPaused(keysQuery.data?.keys, view.request) }
    : null
  const words = request ? requestWording(request, names) : null
  const needsValue = Boolean(words?.needsValue)

  return {
    ...view,
    request,
    words,
    needsValue,
    value,
    setValue,
    guard,
    /** Whether choosing ``kind`` leaves anything still to fill in. */
    ready: kind => kind !== KEY_ALLOW || !needsValue || view.state !== 'pending' || Boolean(value),
    /** Answer the vault (the owner's own input) for ``kind``; throws to keep the card open. */
    async settle(kind) {
      if (settled === kind) return
      const given = keyAnswerAlreadyGiven(view.state, kind)
      if (given === null) {
        throw new Error(KEY_STATE_ERRORS[view.state] || 'Couldn’t reach the key vault. Try again in a moment.')
      }
      if (!given) {
        const response = kind === KEY_ALLOW
          ? await api.keys.approve(request.id, {
            ticket: request.ticket,
            sites: request.sites || [],
            ...(needsValue ? { value, inject: request.inject || BEARER } : {}),
          })
          : await api.keys.deny(request.id, request.ticket)
        await jsonOrThrow(response, kind === KEY_ALLOW ? 'Allow' : 'Not now')
        setValue('')
      }
      setSettled(kind)
      void queryClient.invalidateQueries({ queryKey: KEYS_QUERY })
    },
  }
}
