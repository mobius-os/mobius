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

export function providerIsConfigured(info) {
  return info?.configured === true
    || (info?.configured === undefined && info?.authenticated === true)
}

export function configuredProviderSet(statusByProvider) {
  return new Set(
    Object.entries(statusByProvider || {})
      .filter(([, info]) => providerIsConfigured(info))
      .map(([providerId]) => providerId),
  )
}

export function resolveProviderAvailability(statusQuery) {
  if (statusQuery?.data !== undefined) {
    return {
      phase: PROVIDER_AVAILABILITY_PHASE.READY,
      configuredProviders: configuredProviderSet(statusQuery.data),
    }
  }
  return {
    phase: statusQuery?.isError
      ? PROVIDER_AVAILABILITY_PHASE.ERROR
      : PROVIDER_AVAILABILITY_PHASE.LOADING,
    configuredProviders: new Set(),
  }
}

export function providerAvailabilityNeedsAttention(availability) {
  return availability.phase === PROVIDER_AVAILABILITY_PHASE.ERROR
    || (
      availability.phase === PROVIDER_AVAILABILITY_PHASE.READY
      && availability.configuredProviders.size === 0
    )
}

/**
 * A disconnected active provider keeps only its selected row for context. The
 * rest of that provider's registry is not actionable and must not look like a
 * list of available choices.
 */
export function visibleProviderModels(
  providerId,
  models,
  configuredProviders,
  retainedProvider = '',
  retainedModel = '',
) {
  const rows = Array.isArray(models) ? models : []
  if (configuredProviders.has(providerId)) return rows
  if (providerId !== retainedProvider) return []
  return rows.filter(model => model?.id === retainedModel)
}
