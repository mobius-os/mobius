import { chatQueries, modelQueries, settingsQueries } from '../../hooks/queries.js'
import BrainUsageIcon from './BrainUsageIcon.jsx'
import {
  providerAllowance,
  providerAllowanceSummary,
} from '../SettingsView/providerUsage.js'
import {
  contextUsedPercent,
  formatRoundedTokenCount,
  resolvedContextTokenCounts,
} from './brainUsage.js'

const PROVIDER_LABELS = {
  claude: 'Claude',
  codex: 'Codex',
  mobius: 'Möbius',
}

export default function BrainUsageButton({
  children,
  usageEnabled = true,
  chatId = null,
  provider = null,
  providerSessionId = null,
  model = null,
}) {
  const providerUsageQuery = settingsQueries.providerUsage.useQuery(provider, {
    enabled: usageEnabled && Boolean(provider),
  })
  const contextUsageQuery = chatQueries.currentUsage.useQuery(
    chatId,
    provider,
    providerSessionId,
    { enabled: usageEnabled },
  )
  const modelRegistryQuery = modelQueries.registry.useQuery({
    enabled: usageEnabled && !providerSessionId && Boolean(provider && model),
  })
  const allowance = providerUsageQuery.isLoading
    ? providerAllowance(provider, null)
    : providerAllowance(provider, providerUsageQuery.data)
  const allowanceSummary = providerAllowanceSummary(provider, allowance)
  const leftPercent = allowance.usedPercent
  const contextSnapshot = (
    contextUsageQuery.isLoading
    || contextUsageQuery.data?.provider !== provider
    || contextUsageQuery.data?.provider_session_id !== providerSessionId
  )
    ? null
    : contextUsageQuery.data
  const contextTokens = resolvedContextTokenCounts(
    contextSnapshot,
    modelRegistryQuery.isLoading ? null : modelRegistryQuery.data,
    provider,
    model,
  )
  const rightPercent = contextTokens === null
    ? null
    : contextUsedPercent({
      input_tokens: contextTokens.used,
      context_window: contextTokens.maximum,
    })
  const providerLabel = PROVIDER_LABELS[provider] || 'Current model'

  const usageSummary = [
    leftPercent === null
      ? `${providerLabel} usage: unknown`
      : (provider === 'mobius' ? allowanceSummary : `${providerLabel} ${allowanceSummary}`),
    contextTokens === null || rightPercent === null
      ? 'Context used: unknown'
      : `Context used: ${formatRoundedTokenCount(contextTokens.used)} of ${formatRoundedTokenCount(contextTokens.maximum)} tokens (${Math.round(rightPercent)}%); ${Math.round(100 - rightPercent)}% remains before compaction`,
  ].join(' · ')

  return children({
    icon: <BrainUsageIcon leftPercent={leftPercent} rightPercent={rightPercent} />,
    ariaLabel: `Chat options. ${usageSummary}`,
    providerUsage: {
      provider,
      providerLabel,
      allowanceKind: allowance.kind,
      allowanceLabel: allowance.label,
      allowanceUsedPercent: leftPercent,
      allowanceSummary,
      contextTokensUsed: contextTokens?.used ?? null,
      contextTokensMaximum: contextTokens?.maximum ?? null,
    },
  })
}
