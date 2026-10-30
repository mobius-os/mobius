/* Loads the Storage and Memory overview and runs its owner actions. */
import { useCallback, useEffect, useState } from 'react'
import { api, jsonOrThrow } from '../../../api/client.js'

export default function useStorageMemory({ active }) {
  const [overview, setOverview] = useState(null)
  const [error, setError] = useState('')
  const [measuring, setMeasuring] = useState(false)

  const load = useCallback(async ({ refresh = false } = {}) => {
    if (refresh) setMeasuring(true)
    try {
      setOverview(await api.resources.overview({ refresh }))
      setError('')
    } catch (err) {
      setError(err?.message || 'Storage and memory are unavailable right now.')
    } finally {
      if (refresh) setMeasuring(false)
    }
  }, [])

  useEffect(() => {
    if (active) void load()
  }, [active, load])

  const preview = useCallback(target => api.resources.previewCleanup(target), [])

  const clear = useCallback(async (target) => {
    const result = await api.resources.runCleanup(target)
    setOverview(result.resources)
    setError('')
    return result
  }, [])

  // Settings cannot resend cleared messages, so the server refuses (409) when
  // the owner has typed messages queued; a fresh reading then offers the chat.
  // A Stop that timed out answers 200 with stopped: false; that is a failure
  // the owner can retry, not a success.
  const stopTurn = useCallback(async (chatId) => {
    let result
    try {
      result = await jsonOrThrow(await api.chats.stop(chatId, { refuseIfQueued: true }), 'Could not stop this turn')
    } catch (err) {
      if (err?.status === 409) void load()
      throw err
    }
    if (result?.stopped !== true) throw new Error('The turn did not stop in time. Try again.')
    await load()
  }, [load])

  return {
    overview,
    error,
    measuring,
    measure: () => load({ refresh: true }),
    preview,
    clear,
    stopTurn,
  }
}
