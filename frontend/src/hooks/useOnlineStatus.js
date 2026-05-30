import { useEffect, useState } from 'react'

// Single source of truth for connectivity. Subscribes to the window
// online/offline events; navigator.onLine seeds the initial value.
// Used by the chat composer (chat is online-only, so we disable + tell
// the user rather than failing a send into a dead stream).
export default function useOnlineStatus() {
  const [online, setOnline] = useState(
    typeof navigator === 'undefined' ? true : navigator.onLine,
  )
  useEffect(() => {
    const on = () => setOnline(true)
    const off = () => setOnline(false)
    window.addEventListener('online', on)
    window.addEventListener('offline', off)
    return () => {
      window.removeEventListener('online', on)
      window.removeEventListener('offline', off)
    }
  }, [])
  return online
}
