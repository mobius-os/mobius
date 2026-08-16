/**
 * BrainUsageButton — the model-picker trigger between the `+` button and
 * the prompt pill. This is what opens the model/effort picker now; `+`
 * (ComposerPopover with `showModel={false}`) no longer does. It's a thin
 * wrapper around a second, independent <ComposerPopover> instance
 * (`showAttachAndContext={false} showModel`) rather than a bespoke
 * popover — that reuses ComposerPopover's positioning and mobile-keyboard
 * focus-preservation logic (iOS/Android quirks, viewport math) instead of
 * duplicating it.
 *
 * The trigger itself is a two-hemisphere brain glyph that doubles as a live
 * usage gauge (left = Codex/purple, right = Claude/orange — see
 * BrainUsageIcon.jsx). Each provider can report BOTH a short (`5-hour`) and
 * a long (`Weekly`) rate-limit window from GET
 * /settings/provider-usage/{provider} (already used by Settings'
 * ProviderUsage.jsx) — a provider commonly hits the short window first even
 * with plenty of the week left. Fill = the LOWER of the two remaining
 * fractions, i.e. whichever window is closer to exhausted, since that's the
 * one that actually determines when the provider stops answering. Other
 * window kinds a provider may report (bonus/overage allowances, etc.) don't
 * gate normal usage the same way and are ignored here. 100% remaining =
 * fully colored, 0% remaining (that window fully exhausted) = fully grey.
 * Unknown/unloaded renders a dim outline rather than assuming full.
 */

import { settingsQueries } from '../../hooks/queries.js'
import BrainUsageIcon from './BrainUsageIcon.jsx'
import { clampUsagePercent } from '../SettingsView/providerUsage.js'
import ComposerPopover from './ComposerPopover.jsx'

const RATE_LIMIT_WINDOW_LABELS = new Set(['5-hour', 'Weekly'])

function mostConstrainedRemainingPercent(query) {
  const snapshot = query.data
  if (query.isLoading || !snapshot) return null
  if (snapshot.state !== 'ready') return null
  const windows = Array.isArray(snapshot.windows) ? snapshot.windows : []
  const rateLimitWindows = windows.filter(w => RATE_LIMIT_WINDOW_LABELS.has(w?.label))
  const candidates = rateLimitWindows.length > 0 ? rateLimitWindows : windows.slice(0, 1)
  if (candidates.length === 0) return null
  return Math.min(...candidates.map(w => 100 - clampUsagePercent(w.used_percent)))
}

export default function BrainUsageButton(props) {
  const codexUsage = settingsQueries.providerUsage.useQuery('codex')
  const claudeUsage = settingsQueries.providerUsage.useQuery('claude')
  const leftPercent = mostConstrainedRemainingPercent(codexUsage)
  const rightPercent = mostConstrainedRemainingPercent(claudeUsage)

  const usageSummary = [
    leftPercent === null ? 'Codex usage: unknown' : `Codex usage left: ${Math.round(leftPercent)}%`,
    rightPercent === null ? 'Claude usage: unknown' : `Claude usage left: ${Math.round(rightPercent)}%`,
  ].join(' · ')

  return (
    <ComposerPopover
      {...props}
      showAttachAndContext={false}
      showModel
      triggerIcon={<BrainUsageIcon leftPercent={leftPercent} rightPercent={rightPercent} />}
      triggerClassName="chat__brain-usage"
      triggerAriaLabel={`Choose model. ${usageSummary}`}
      dialogAriaLabel="Choose model"
    />
  )
}
