import { settingsQueries } from '../../hooks/queries.js'
import BrainUsageIcon from './BrainUsageIcon.jsx'
import {
  providerAllowance,
  providerAllowanceSummary,
  providerExtraUsage,
  providerWeeklyAllowance,
} from '../SettingsView/providerUsage.js'
import {
  contextUsedPercent,
  formatRoundedTokenCount,
} from './brainUsage.js'
import useContextTokens from './hooks/useContextTokens.js'

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
  liveContext = null,
  started = true,
}) {
  const providerUsageQuery = settingsQueries.providerUsage.useQuery(provider, {
    enabled: usageEnabled && Boolean(provider),
  })
  const { tokens: contextTokens } = useContextTokens({
    enabled: usageEnabled,
    chatId,
    provider,
    providerSessionId,
    model,
    liveContext,
    started,
  })
  const allowance = providerUsageQuery.isLoading
    ? providerAllowance(provider, null)
    : providerAllowance(provider, providerUsageQuery.data)
  const allowanceSummary = providerAllowanceSummary(provider, allowance)
  const weeklyAllowance = providerUsageQuery.isLoading
    ? null
    : providerWeeklyAllowance(provider, providerUsageQuery.data)
  const weeklySummary = weeklyAllowance
    ? providerAllowanceSummary(provider, weeklyAllowance)
    : null
  const extraUsage = providerExtraUsage(providerUsageQuery.data)
  const leftPercent = allowance.usedPercent
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
    weeklySummary ? `${providerLabel} ${weeklySummary}` : null,
    extraUsage?.summary ? `${providerLabel} ${extraUsage.summary}` : null,
    contextTokens === null || rightPercent === null
      ? 'Context used: unknown'
      : `Context used: ${formatRoundedTokenCount(contextTokens.used)} of ${formatRoundedTokenCount(contextTokens.maximum)} tokens (${Math.round(rightPercent)}%); ${Math.round(100 - rightPercent)}% remains before compaction`,
  ].filter(Boolean).join(' · ')

  return children({
    icon: <BrainUsageIcon leftPercent={leftPercent} rightPercent={rightPercent} />,
    ariaLabel: `Chat options. ${usageSummary}`,
    providerUsage: {
      provider,
      providerLabel,
      extraUsageLabel: extraUsage?.label || null,
      extraUsageSummary: extraUsage?.summary || null,
      extraUsagePercent: extraUsage?.usedPercent ?? null,
      allowanceKind: allowance.kind,
      allowanceLabel: allowance.label,
      allowanceUsedPercent: leftPercent,
      allowanceSummary,
      weeklyAllowanceSummary: weeklySummary,
      contextTokensUsed: contextTokens?.used ?? null,
      contextTokensMaximum: contextTokens?.maximum ?? null,
    },
  })
}
