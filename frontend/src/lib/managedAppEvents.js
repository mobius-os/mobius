export function managedAppEventForShellEvent(event, previousSequence = 0) {
  if (event?.type !== 'app_updated') return null
  return {
    type: 'app_updated',
    appId: event.appId == null ? null : String(event.appId),
    sequence: previousSequence + 1,
  }
}

export function managedAppFrameMessage(event, capabilityContract) {
  if (
    event?.type !== 'app_updated'
    || capabilityContract?.data?.manage_apps !== true
  ) return null
  return {
    type: 'moebius:managed-app-event',
    event: {
      type: 'app_updated',
      appId: event.appId,
      sequence: event.sequence,
    },
  }
}
