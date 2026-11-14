/* The chat's context occupancy, read the one way the composer gauge and the
 * /context panel both show it: tokens used and the window size. */
import { chatQueries, modelQueries } from '../../../hooks/queries.js'
import { contextNeverStarted } from '../contextBreakdown.js'
import { resolvedContextTokenCounts } from '../brainUsage.js'
import { catalogProviders } from '../../../lib/modelCatalog.js'

/**
 * `tokens` is `{ used, maximum }` or null when unknown. `noSession` means the
 * chat has never started a provider session — the caller's explicit `started`
 * lifecycle signal, not a guess from the session id — so nothing is in
 * context and `used` is genuinely zero (the window then comes from the model
 * registry). An established chat whose resumable session id was cleared (by
 * compaction or a provider handoff) reports unknown usage instead of claiming
 * an empty context. Both readers share these queries, so opening /context
 * fetches nothing new.
 */
export default function useContextTokens({
  enabled = true,
  chatId = null,
  provider = null,
  providerSessionId = null,
  model = null,
  liveContext = null,
  started = true,
}) {
  const contextUsageQuery = chatQueries.currentUsage.useQuery(
    chatId,
    provider,
    providerSessionId,
    { enabled },
  )
  // The registry supplies the ceiling before a session exists, for a fresh
  // session, and during a turn whose provider reports no window mid-turn.
  const modelRegistryQuery = modelQueries.registry.useQuery({
    enabled: enabled && Boolean(provider && model),
  })
  // A chat with no provider session yet never enables the usage query, so the
  // snapshot is undefined rather than a null-session record. The registry
  // fallback still owns the pre-first-turn estimate there.
  const noSession = (
    enabled
    && contextNeverStarted({ started, providerSessionId })
    && Boolean(provider && model)
  )
  const tokens = resolvedContextTokenCounts(
    contextUsageQuery.isLoading ? null : contextUsageQuery.data,
    modelRegistryQuery.isLoading ? null : catalogProviders(modelRegistryQuery.data),
    provider,
    model,
    { noSession, live: enabled ? liveContext : null },
  )
  return {
    tokens,
    noSession,
    loading: contextUsageQuery.isLoading || modelRegistryQuery.isLoading,
  }
}
