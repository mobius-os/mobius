import { settingsQueries } from '../../hooks/queries.js'
import BrainUsageIcon from './BrainUsageIcon.jsx'
import { mostConstrainedRemainingPercent } from '../SettingsView/providerUsage.js'

export default function BrainUsageButton({ children }) {
  const codexUsage = settingsQueries.providerUsage.useQuery('codex')
  const claudeUsage = settingsQueries.providerUsage.useQuery('claude')
  const leftPercent = codexUsage.isLoading
    ? null
    : mostConstrainedRemainingPercent(codexUsage.data)
  const rightPercent = claudeUsage.isLoading
    ? null
    : mostConstrainedRemainingPercent(claudeUsage.data)

  const usageSummary = [
    leftPercent === null ? 'Codex usage: unknown' : `Codex usage left: ${Math.round(leftPercent)}%`,
    rightPercent === null ? 'Claude usage: unknown' : `Claude usage left: ${Math.round(rightPercent)}%`,
  ].join(' · ')

  return children({
    icon: <BrainUsageIcon leftPercent={leftPercent} rightPercent={rightPercent} />,
    ariaLabel: `Choose model. ${usageSummary}`,
  })
}
