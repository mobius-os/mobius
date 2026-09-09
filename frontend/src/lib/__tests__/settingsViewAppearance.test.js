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

const updates = readFileSync(
  new URL('../../components/SettingsView/PlatformUpdates.jsx', import.meta.url), 'utf8',
)
const updateCss = readFileSync(
  new URL('../../components/SettingsView/PlatformUpdates.css', import.meta.url), 'utf8',
)
const requests = readFileSync(
  new URL('../../components/SettingsView/usePlatformUpdates.js', import.meta.url), 'utf8',
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

test('last model keeps its normal-weight standard highlight', () => {
  assert.match(view, /provider-row__status-text settings__last-model/)
  assert.match(view, /Choose which models appear\. New chats use your last pick\./)
  assert.match(view, /Last model: <span className="settings__standard-highlight">/)
  assert.match(css, /\.settings__last-model\s*\{[^}]*color:\s*var\(--muted\);[^}]*font-weight:\s*400;/s)
  assert.match(css, /\.settings__standard-highlight\s*\{[^}]*color:\s*var\(--green\);[^}]*font-weight:\s*inherit;/s)
})

test('version details distinguish served Möbius from its container identity', () => {
  assert.match(view, /<PlatformUpdates/)
  assert.match(updates, /platformVersionIdentity\(platform, version\)/)
  assert.match(updates, /containerVersionIdentity\(version\)/)
  assert.match(updates, /contained_upstream_committed_at/)
  assert.match(updates, /<dt>Installed update<\/dt>/)
  assert.match(updates, /<dt>Current system<\/dt>/)
  assert.match(updates, /mobiusVersion\.primarySha/)
  assert.match(updates, /containerVersion\.sha/)
  assert.doesNotMatch(updates, /Current with upstream|Last checked|upstream_checked_at/)
})

test('restart explains the interruption and its container boundary before confirmation', () => {
  assert.match(updates, /Restart server<\/button>/)
  assert.match(updates, /aria-label="Confirm restart"/)
  assert.match(updates, /briefly interrupts active chats/)
  assert.match(updates, /does not replace the container/)
  assert.match(updates, /onClick=\{update\.restart\}/)
  assert.match(updates, /onClick=\{askRestart\}/)
  // An image replacement still belongs to its exact reviewed update, not a
  // second unreviewed maintenance action beside the server restart.
  assert.doesNotMatch(updates, /Rebuild now|Rebuild container|Replace now/)
})

test('Settings omits historical container diagnostics but keeps current action errors', () => {
  assert.doesNotMatch(updates, /Last container update|Update details|rebuild\.expected_sha|rebuild\.updated_at/)
  assert.doesNotMatch(updates, /The source is installed\./)
  assert.match(updates, /update\.error && <Alert/)
  assert.match(updates, /platformUpdateRepairReason/)
  assert.match(updates, /rebuildProgressMessage\(rebuild\)/)
  assert.doesNotMatch(updates + requests, /rebuildInitiatedHereRef|rebuildReviewedUpdateRef/)
  assert.match(requests, /if \(!reconnect\) return/)
})

test('status failures project unknown rather than retaining an unchecked current claim', () => {
  assert.match(requests, /platformStatusUnavailable/)
  assert.match(requests, /if \(!response\.ok\)/)
  assert.match(requests, /refreshPlatform[\s\S]*responseBody\(await api\.platform\.status\(\)\)/)
  assert.match(requests, /if \(!preserveCurrentOnFailure\) setPlatform\(current => platformStatusUnavailable\(current\)\)/)
  assert.match(requests, /results\[0\]\.status === 'rejected'[\s\S]*platformStatusUnavailable\(current\)/)
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

test('Möbius subscription is app-owned and follows Codex and Claude', () => {
  assert.match(view, /const mobiusAvailable = providerStatusQuery\.data\?\.mobius\?\.available === true/)
  assert.match(
    view,
    /name="OpenAI Codex"[\s\S]*name="Claude Code"[\s\S]*\{mobiusAvailable && \([\s\S]*name="Möbius subscription"/,
  )
  assert.match(view, /Sign in from Möbius · You to activate your trial\./)
  assert.match(view, /actionLabel="Open Möbius · You"/)
  assert.match(view, /onOpenApp\?\.\('identity'\)/)
  assert.doesNotMatch(view, /Claim trial|connectMobius|startLogin\(\)/)
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


test('original Settings keeps spacious rounded cards and full model summaries', () => {
  assert.match(css, /\.settings\s*\{[^}]*padding:\s*28px 22px 20px;/s)
  assert.match(css, /\.settings__section\s*\{[^}]*border-radius:\s*8px;[^}]*padding:\s*17px;/s)
  assert.doesNotMatch(css, /\.settings \.provider-row\s*\{/)
  assert.match(css, /\.settings-bg-row\s*\{[^}]*min-height:\s*50px;/s)
  assert.match(view, /title=\{selectedModel \|\| triggerLabel\}/)
  assert.match(view, /className="model-trigger__id"/)
  assert.match(view, /<ModelSheet[\s\S]*onEffortChange=\{onEffortChange\}/)
})

test('compact Updates pairs its status with actions without redundant success copy', () => {
  assert.match(updates, /platform-updates__heading[\s\S]*role="status"/)
  assert.doesNotMatch(updates, /No action needed\./)
  assert.match(updates, /aria-label="Confirm restart"/)
  assert.match(updates, /className={`settings__btn settings__btn--sm/)
  assert.match(updates, /settings__btn--outline settings__btn--sm platform-updates__restart"[^>]*>Restart server<\/button>/)
  assert.match(updateCss, /\.platform-updates > \.platform-updates__actions\s*\{[^}]*justify-content:\s*flex-end;/s)
  assert.doesNotMatch(updateCss, /\.platform-updates > \.platform-updates__actions\s*\{[^}]*flex-direction:\s*column;/s)
  assert.match(updates, /platform-updates__restart[^>]*>Restart server<\/button>/)
  assert.doesNotMatch(updateCss, /platform-updates__maintenance/)
})


test('provider actions keep their full labels on one line without a width cap', () => {
  assert.match(css, /\.settings \.provider-row__action\s*\{[^}]*white-space:\s*nowrap;/s)
  assert.doesNotMatch(css, /\.settings \.provider-row__action\s*\{[^}]*max-width:/s)
})


test('simple Updates keeps versions and server restart visible outside optional details', () => {
  const visible = updates.slice(0, updates.indexOf('{review && ('))
  assert.match(visible, />Updates<\/h2>/)
  assert.match(visible, /<dt>Installed update<\/dt>/)
  assert.match(visible, /<dt>Current system<\/dt>/)
  assert.match(visible, /onClick=\{askRestart\}>Restart server<\/button>/)
  assert.match(updates, /aria-label="Confirm restart"/)
})

test('external update instructions come from the activation owner, not a second UI policy', () => {
  assert.match(updates, /platform\?\.activation\?\.guidance/)
  assert.doesNotMatch(updates, /scripts\/deploy-prod\.sh|hostMaintenanceNeeded|little maintenance/)
})

test('only top provider actions are pills; Updates inherits standard Settings corners', () => {
  const providerCss = readFileSync(new URL('../../components/ProviderAuth/ProviderAuth.css', import.meta.url), 'utf8')
  assert.match(css, /\.settings__btn\s*\{[^}]*border-radius:\s*8px;/s)
  assert.doesNotMatch(updateCss, /border-radius:\s*999px/)
  assert.match(providerCss, /\.provider-row__action\s*\{[^}]*border-radius:\s*999px;/s)
})
