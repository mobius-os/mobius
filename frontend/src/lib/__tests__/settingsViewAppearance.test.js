import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const view = readFileSync(
  new URL('../../components/SettingsView/SettingsView.jsx', import.meta.url),
  'utf8',
)
const css = readFileSync(
  new URL('../../components/SettingsView/SettingsView.css', import.meta.url),
  'utf8',
)

test('appearance keeps one icon switch without making the section clickable', () => {
  assert.match(view, /settings__section--appearance/)
  assert.match(view, /className="settings__appearance-toggle"[\s\S]*role="switch"[\s\S]*onClick=\{toggleTheme\}/)
  assert.match(view, /settings__appearance-option/)
  assert.doesNotMatch(view, /settings__appearance-thumb/)
  assert.match(view, /<Sun[\s\S]*<Moon/)
  assert.doesNotMatch(view, /<span>Light<\/span>|<span>Dark<\/span>|type="radio"/)
  assert.doesNotMatch(view, /<section[^>]*onClick=\{toggleTheme\}/)
  assert.match(css, /\.settings__appearance-toggle\s*\{[^}]*grid-template-columns:\s*repeat\(2, 34px\);/s)
})

test('model and concise synced version use the same normal-weight standard highlight', () => {
  assert.match(view, /provider-row__status-text settings__last-model/)
  assert.match(view, /Choose which models appear\. New chats use your last pick\./)
  assert.match(view, /Last model: <span className="settings__standard-highlight">/)
  assert.match(view, /upstreamCommitDate[\s\S]*settings__standard-highlight/)
  assert.match(view, /contained_upstream_committed_at/)
  assert.match(view, /containerVersionIdentity\(version\)/)
  assert.match(view, /settings__build-kind">Möbius/)
  assert.match(view, /settings__build-kind">Container/)
  assert.doesNotMatch(view, /Current with upstream|Last checked|upstream_checked_at|settings__update-check/)
  assert.doesNotMatch(view, /Serving local \{mobiusVersion\.localSha\}/)
  assert.match(css, /\.settings__last-model\s*\{[^}]*color:\s*var\(--muted\);[^}]*font-weight:\s*400;/s)
  assert.match(css, /\.settings__standard-highlight\s*\{[^}]*color:\s*var\(--green\);[^}]*font-weight:\s*inherit;/s)
})

test('restart explains its container effect on demand', () => {
  assert.match(view, /SettingsInfoLabel[\s\S]*aria-expanded=\{expanded\}/)
  assert.match(view, /settings__info-bubble[\s\S]*role="tooltip"/)
  assert.match(view, /dismissOnOutsidePress[\s\S]*dismissOnEscape/)
  assert.match(view, /label="Restart"[\s\S]*settings-restart-info/)
  assert.match(view, /does not[\s\S]*install a newer container image/)
  // The standalone manual container-rebuild control was removed: an image-level
  // update now drives the rebuild on confirmation from the update review flow.
  assert.doesNotMatch(view, /label=\{rebuildBootstrap \? 'Container updates' : 'Rebuild container'\}/)
  assert.doesNotMatch(view, /Rebuild now|Rebuild container|settings-rebuild-info/)
  assert.doesNotMatch(view, /Replace container|Replace now|Replacing…/)
  assert.match(css, /\.settings__info-button\s*\{[^}]*width:\s*32px;[^}]*height:\s*32px;/s)
  assert.match(css, /\.settings__info-bubble\s*\{[^}]*position:\s*absolute;[^}]*bottom:\s*calc\(100% \+ 10px\);/s)
})

test('historical rebuild failures do not become permanent Settings errors', () => {
  // The host controller deliberately retains its last terminal status. Only a
  // rebuild initiated by this mounted, reviewed update flow may surface that
  // result; otherwise a stale failure would outlive the removed manual action.
  assert.match(
    view,
    /\['failed', 'rolled_back', 'needs_recovery'\][\s\S]*!rebuildInitiatedHereRef\.current[\s\S]*!rebuildReviewedUpdateRef\.current[\s\S]*return/,
  )
})

test('platform status failures replace cached current claims with unknown state', () => {
  assert.match(view, /platformStatusUnavailable,/)
  assert.match(
    view,
    /const refreshPlatform = useCallback[\s\S]*if \(!res\.ok\) throw new Error[\s\S]*catch \{[\s\S]*setPlatform\(current => platformStatusUnavailable\(current\)\)/,
  )
  assert.match(
    view,
    /const platformP = \(async \(\) => \{[\s\S]*catch \(error\) \{[\s\S]*setPlatform\(current => platformStatusUnavailable\(current\)\)[\s\S]*throw error/,
  )
})

test('background agents are always draggable without reorder chrome or a trailing caret', () => {
  assert.match(view, /settings-bg-row__effort-visual[\s\S]*settings-bg-row__effort-dot/)
  assert.match(view, /efforts=\{efforts\}[\s\S]*onEffortChange=\{onEffortChange\}/)
  assert.doesNotMatch(view, /settings-bg-row__effort-picker|<EffortStepper/)
  assert.doesNotMatch(view, /\{effortLabel\} effort<\/span>/)
  assert.match(view, /reorderMode\s*\n/)
  assert.match(view, /<GripVertical size=\{18\} strokeWidth=\{2\}/)
  assert.doesNotMatch(view, /settings-agent-group__reorder|>Reorder<|model-trigger__caret/)
  assert.match(view, /Background agents/)
  assert.match(view, /Used for memory, reflection, and other automatic tasks\. Tried in order\./)
  assert.match(css, /\.settings-bg-row\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent;/s)
  assert.match(css, /\.settings-bg-row__effort-visual\s*\{[^}]*min-width:\s*68px;/s)
  assert.doesNotMatch(view, /dropPosition|settings-bg-row--drop-before|settings-bg-row--drop-after/)
  assert.doesNotMatch(css, /settings-bg-row--drop-before|settings-bg-row--drop-after/)
})

test('provider-dependent settings stay unavailable until a provider is connected', () => {
  assert.match(view, /disabled=\{!hasConfiguredProvider\}/)
  assert.match(view, /No provider connected/)
  assert.match(view, /Connect an AI provider to choose chat models\./)
  assert.match(view, /settings-agent-group--disabled/)
  assert.match(view, /Connect an AI provider to configure automatic tasks\./)
  assert.match(view, /configuredProviders=\{configuredProviders\}/)
})

test('new provider connections use the curated unattended defaults', () => {
  assert.match(view, /claude: 'claude-opus-4-8'/)
  assert.match(view, /codex: 'gpt-5\.6-terra'/)
  assert.match(view, /authProvidersAtStartRef\.current = new Set\(configuredProvidersRef\.current\)/)
  assert.match(view, /const newlyConnected = !providersBefore\.has\(provider\)/)
  assert.match(view, /providersBefore\.size === 0[\s\S]*connectedRow[\s\S]*enabled: false/)
  assert.match(view, /const onProviderConnected = useCallback\(async \(provider\)/)
  assert.match(view, /await persistBackgroundAgents\([\s\S]*providersBefore\.size === 0 \? \{ provider \} : \{\}/)
  assert.match(view, /api\.settings\.save\(\{[\s\S]*\.\.\.companionSettings,[\s\S]*background_agents: payload/)
  assert.match(view, /await settleBackgroundAgentSave\([\s\S]*if \(stale\) return true/)
  assert.match(view, /if \(!saved\) return[\s\S]*setExpandedAuth\(null\)/)
  assert.doesNotMatch(view, /api\.settings\.save\(\{ provider \}\)/)
  assert.match(view, /effort: defaultEffort\(provider\)/)
})

test('Möbius subscription status uses the same consumed-credit copy as the brain', () => {
  assert.match(view, /enabled: active && providerReady && mobiusAvailable && mobiusAuthenticated/)
  assert.match(view, /providerAllowanceSummary\('mobius', mobiusAllowance\)/)
  assert.doesNotMatch(view, /mobiusRemaining|spendable_units/)
})

test('appearance indicator waits for the same seeded theme repaint as the palette', () => {
  assert.doesNotMatch(view, /setThemeMode\(newMode\)/)
  assert.match(view, /await themeService\.toggleTheme\(queryClient, currentMode, api\)/)
  assert.match(view, /setThemeMode\(themeModeQuery\.data === 'light'/)
})
