import { useEffect } from 'react'
import { initLocationTracking } from '../lib/nativeLocation.js'

// Boots native location tracking once per shell session (re-arming it after a
// reload when it was on in this app session, and flushing any queued
// positions). Tracking is started and stopped from the app that uses it,
// through the `location.tracking` capability. Safe to call from more than one
// place — the controller ignores repeat init calls.
export function useLocationBoot() {
  useEffect(() => { void initLocationTracking() }, [])
}
