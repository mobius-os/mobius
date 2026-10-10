/* The Settings-wide identity client: one request helper and one shared cache.
   The Settings overview row and the Möbius account page both read the owner's
   identity and profile photo; they share these cache entries so opening and
   leaving the account page does not refetch, and an edit on the account page
   shows on the overview row immediately. */
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import {
  IdentityRequestError,
  parseAgentAccess,
  parseDeletionDiagnosis,
  parseIdentity,
  parseLinkAttempt,
  parseRailway,
} from './identity-contract.js'

export const IDENTITY_KEY = ['identity']
const AVATAR_KEY = [...IDENTITY_KEY, 'avatar']
// Fresh enough to skip the account page's mount read right after the overview
// loaded it; focus and Try again still force a fresh read.
export const IDENTITY_STALE_MS = 30_000

export async function identityRequest(token, path = '', options = {}) {
  let response
  try {
    response = await fetch(path.startsWith('/api/') ? path : `/api/identity${path}`, {
      ...options,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(options.headers || {}),
      },
    })
  } catch (error) {
    if (error?.name === 'AbortError') throw error
    throw new IdentityRequestError(
      'This Möbius could not reach its identity service. Check your connection and try again.',
    )
  }

  if (response.status === 204) return null
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = body.detail
    const message = typeof detail === 'string'
      ? detail
      : detail?.message || 'Identity is unavailable right now.'
    throw new IdentityRequestError(
      message,
      response.status,
      typeof detail?.code === 'string' ? detail.code : '',
    )
  }
  const pathname = path.split(/[?#]/, 1)[0]
  if (pathname === '/link/start') return parseLinkAttempt(body)
  if (pathname === '/agent' || pathname === '/agent/trial') return parseAgentAccess(body)
  if (pathname === '/railway') return parseRailway(body)
  if (/^\/railway\/deployments\/[^/]+\/deletion$/.test(pathname)) {
    return parseDeletionDiagnosis(body)
  }
  if (['', '/profile', '/avatar', '/link/complete'].includes(pathname)) {
    return parseIdentity(body)
  }
  return body
}

/** Read the shared identity, reusing a fresh cached copy or an in-flight read. */
export function loadIdentity(queryClient, token, { force = false } = {}) {
  return queryClient.fetchQuery({
    queryKey: IDENTITY_KEY,
    queryFn: ({ signal }) => identityRequest(token, '', { signal }),
    staleTime: force ? 0 : IDENTITY_STALE_MS,
  })
}

/** A confirmed unlink invalidates linked details even if its next read fails. */
export async function disconnectIdentity(queryClient, token) {
  await identityRequest(token, '/link', { method: 'DELETE' })
  await queryClient.cancelQueries({ queryKey: IDENTITY_KEY })
  queryClient.removeQueries({ queryKey: AVATAR_KEY })
  // Leave the cache absent until the backend supplies its full local identity,
  // including the current deployment. Do not invent a signed-out payload.
  void queryClient.resetQueries({ queryKey: IDENTITY_KEY, exact: true }).catch(() => {})
}

/** Record an identity returned by an edit, sign-in, or disconnect. */
export function publishIdentity(queryClient, next, { avatarChanged = false } = {}) {
  void queryClient.cancelQueries({ queryKey: IDENTITY_KEY, exact: true })
  queryClient.setQueryData(IDENTITY_KEY, next)
  if (avatarChanged) queryClient.removeQueries({ queryKey: AVATAR_KEY })
}

export function useIdentityQuery(token, { enabled = true } = {}) {
  return useQuery({
    queryKey: IDENTITY_KEY,
    queryFn: ({ signal }) => identityRequest(token, '', { signal }),
    staleTime: IDENTITY_STALE_MS,
    enabled,
  })
}

export async function fetchAvatarBlob(token, signal) {
  const response = await fetch('/api/identity/avatar', {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  })
  if (!response.ok) throw new Error('Profile photo is unavailable.')
  const blob = await response.blob()
  if (!blob.type.startsWith('image/')) throw new Error('Profile photo is not an image.')
  return blob
}

/** Object URL for the owner's protected profile photo, or '' to show initials. */
export function useAvatarSource(avatarUrl, token) {
  const { data: blob } = useQuery({
    queryKey: [...AVATAR_KEY, avatarUrl],
    queryFn: ({ signal }) => fetchAvatarBlob(token, signal),
    enabled: Boolean(avatarUrl),
    staleTime: Infinity,
    retry: false,
  })
  const [source, setSource] = useState('')
  useEffect(() => {
    if (!avatarUrl || !blob) {
      setSource('')
      return undefined
    }
    const objectUrl = URL.createObjectURL(blob)
    setSource(objectUrl)
    return () => URL.revokeObjectURL(objectUrl)
  }, [avatarUrl, blob])
  return source
}
