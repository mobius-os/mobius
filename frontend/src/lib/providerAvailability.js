/**
 * Canonical provider-availability policy for model pickers.
 *
 * Model discovery and provider connection are deliberately separate: the
 * registry can contain fallback models for providers that cannot run. Resolve
 * the status request into an explicit phase, then apply this fail-closed rule
 * everywhere a provider choice is exposed.
 */
export const PROVIDER_AVAILABILITY_PHASE = Object.freeze({
  LOADING: 'loading',
  READY: 'ready',
  ERROR: 'error',
})

export function connectedProviderSet(statusByProvider) {
  return new Set(
    Object.entries(statusByProvider || {})
      .filter(([, info]) => (
        info?.configured === true
        || (info?.configured === undefined && info?.authenticated === true)
      ))
      .map(([providerId]) => providerId),
  )
}

export function resolveProviderAvailability(statusQuery) {
  if (statusQuery?.data !== undefined) {
    return {
      phase: PROVIDER_AVAILABILITY_PHASE.READY,
      connectedProviders: connectedProviderSet(statusQuery.data),
    }
  }
  return {
    phase: statusQuery?.isError
      ? PROVIDER_AVAILABILITY_PHASE.ERROR
      : PROVIDER_AVAILABILITY_PHASE.LOADING,
    connectedProviders: new Set(),
  }
}

/**
 * Retaining one already-saved provider is intentional: hiding the active value
 * would make stale configuration impossible to understand or switch away from.
 * New choices omit `retainedProvider` and remain strictly fail-closed.
 */
export function shouldShowProvider(
  providerId,
  connectedProviders,
  retainedProvider = '',
) {
  return providerId === retainedProvider || connectedProviders.has(providerId)
}
