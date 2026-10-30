import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { button, click, renderHook } from './confirmationHarness.mjs'

const { PlatformUpdatesView } = await import('../../components/SettingsView/PlatformUpdates.jsx')

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

test('appearance keeps one icon switch in the header without making the section clickable', () => {
  assert.match(view, /<header className="settings__header">/)
  assert.doesNotMatch(view, /settings__section--appearance/)
  assert.match(view, /className="settings__appearance-toggle"[\s\S]*role="switch"[\s\S]*onClick=\{toggleTheme\}/)
  assert.match(view, /settings__appearance-option/)
  assert.doesNotMatch(view, /settings__appearance-thumb/)
  assert.match(view, /<Sun[\s\S]*<Moon/)
  assert.doesNotMatch(view, /<span>Light<\/span>|<span>Dark<\/span>|type="radio"/)
  assert.doesNotMatch(view, /<section[^>]*onClick=\{toggleTheme\}/)
  assert.match(css, /\.settings__appearance-toggle\s*\{[^}]*grid-template-columns:\s*repeat\(2, 34px\);/s)
})

test('provider priority does not override the last-picked new-chat model', () => {
  assert.match(view, /New chats still use your last-picked chat model/)
  assert.doesNotMatch(view, /settings__priority-summary|firstBackgroundLabel|lastModelLabel/)
})

test('version details distinguish served Möbius from its container identity', () => {
  assert.match(view, /<PlatformUpdates/)
  assert.match(updates, /platformVersionIdentity\(versionPlatform, version\)/)
  assert.match(updates, /containerVersionIdentity\(version\)/)
  assert.match(updates, /contained_upstream_committed_at/)
  assert.match(updates, /<dt>Code<\/dt>/)
  assert.match(updates, /<dt>Container<\/dt>/)
  assert.match(updates, /mobiusVersion\.primarySha/)
  assert.match(updates, /containerVersion\.sha/)
  assert.doesNotMatch(updates, /Current with upstream|Last checked|upstream_checked_at/)
})

test('update versions paint from the persisted status cache without an Unknown flash', () => {
  assert.match(requests, /platformStatusQueries\.current\.useQuery\(\{ enabled: false \}\)/)
  assert.match(requests, /setQueryData\(platformStatusQueries\.current\.key/)
  assert.match(requests, /const \[platform, setPlatform\] = useState\(null\)/)
  assert.match(updates, /const versionPlatform = platform \|\| cachedPlatform/)
  assert.doesNotMatch(updates, /'Unknown'/)
  assert.match(updates, /versionPlatform \? 'Unavailable' : 'Checking…'/)
})

test('restart keeps interruption guidance without a cancel or unreviewed rebuild action', () => {
  assert.doesNotMatch(updates, /Not now|Restart server/)
  assert.match(updates, /briefly pauses active chats/)
  assert.match(updates, /page will reconnect automatically/)
  assert.doesNotMatch(updates, /does not replace the container/)
  // An image replacement still belongs to its exact reviewed update, not a
  // second unreviewed maintenance action beside the server restart.
  assert.doesNotMatch(updates, /Rebuild now|Rebuild container|Replace now/)
})

function mountUpdates(t) {
  let now = 0
  let restarts = 0
  t.mock.timers.enable({ apis: ['setTimeout'] })
  t.mock.method(performance, 'now', () => now)
  const update = {
    platform: {
      state: 'restart_needed', needs_restart: true, available: false,
      activation: { level: 'server_restart', required_actions: ['server_restart'] },
    },
    rebuild: null, version: null, phase: 'idle', busy: false,
    clearError() {}, restart() { restarts++ },
  }
  const view = renderHook(() => PlatformUpdatesView({ update }))
  t.after(() => view.unmount())
  return {
    view,
    restarts: () => restarts,
    advance(ms) { now += ms; t.mock.timers.tick(ms) },
  }
}

for (const label of ['Restart to finish', 'Restart']) {
  test(`${label}: first click arms, a double-click cannot restart, later confirmation restarts once`, t => {
    const { view, advance, restarts } = mountUpdates(t)
    click(view, label)
    assert.equal(restarts(), 0)
    click(view, 'Confirm restart')
    assert.equal(restarts(), 0, 'an immediate second click is the same gesture')
    advance(499)
    click(view, 'Confirm restart')
    assert.equal(restarts(), 0, 'confirmation must wait the full 500ms')
    advance(1)
    click(view, 'Confirm restart')
    assert.equal(restarts(), 1)
    assert.ok(button(view, label), 'successful confirmation disarms the button')
    click(view, label)
    assert.equal(restarts(), 1, 'another click must arm a new confirmation, not replay restart')
  })

  test(`${label}: confirmation expires at four seconds and a later click only re-arms`, t => {
    const { view, advance, restarts } = mountUpdates(t)
    click(view, label)
    advance(3999)
    assert.ok(button(view, 'Confirm restart'))
    assert.equal(restarts(), 0)
    advance(1)
    assert.ok(button(view, label), 'the four-second timeout must disarm')
    click(view, label)
    assert.equal(restarts(), 0, 'an expired confirmation must never restart')
    advance(500)
    click(view, 'Confirm restart')
    assert.equal(restarts(), 1)
  })
}

for (const [first, other] of [['Restart to finish', 'Restart'], ['Restart', 'Restart to finish']]) {
  test(`arming ${first} cannot be confirmed by ${other}`, t => {
    const { view, advance, restarts } = mountUpdates(t)
    click(view, first)
    advance(600)
    click(view, other)
    assert.equal(restarts(), 0, 'a different button must arm itself, never confirm its neighbour')
    assert.ok(button(view, first), 'only the newly armed button may show confirmation')
    click(view, 'Confirm restart')
    assert.equal(restarts(), 0, 'switching buttons must reset the double-click guard')
    advance(3400)
    assert.ok(button(view, 'Confirm restart'), 'the old button’s expiry must not disarm the new prompt')
    click(view, 'Confirm restart')
    assert.equal(restarts(), 1)
    assert.ok(button(view, first))
    assert.ok(button(view, other))
  })
}

test('Settings omits historical container diagnostics but keeps current action errors', () => {
  assert.doesNotMatch(updates, /Last container update|Update details|rebuild\.expected_sha|rebuild\.updated_at/)
  assert.doesNotMatch(updates, /The source is installed\./)
  assert.match(updates, /update\.error && <Alert/)
  assert.match(updates, /platformUpdateRepairReason/)
  assert.match(updates, /rebuildStatusLine\(rebuild\)/)
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

test('provider rows keep six-dot background priority without reorder chrome', () => {
  assert.match(view, /settings-bg-row__effort-visual[\s\S]*settings-bg-row__effort-dot/)
  assert.match(view, /efforts=\{efforts\}[\s\S]*onEffortChange=\{onEffortChange\}/)
  assert.doesNotMatch(view, /settings-bg-row__effort-picker|<EffortStepper/)
  assert.doesNotMatch(view, /\{effortLabel\} effort<\/span>/)
  assert.match(view, /<GripVertical size=\{18\} strokeWidth=\{2\}/)
  assert.doesNotMatch(view, /settings-agent-group__reorder|>Reorder<|model-trigger__caret/)
  assert.match(view, /background-task priority/)
  assert.match(css, /\.settings-bg-row\s*\{[^}]*border:\s*1px solid var\(--border\);[^}]*background:\s*var\(--surface\);/s)
  assert.match(css, /\.settings-bg-row__effort-visual\s*\{[^}]*min-width:\s*68px;/s)
  assert.doesNotMatch(view, /dropPosition|settings-bg-row--drop-before|settings-bg-row--drop-after/)
  assert.doesNotMatch(css, /settings-bg-row--drop-before|settings-bg-row--drop-after/)
})

test('provider usage is fetched only for the open provider page', () => {
  assert.match(view, /&& selectedProvider === 'codex'/)
  assert.match(view, /&& selectedProvider === 'claude'/)
  assert.doesNotMatch(view, /PlanUsageToggle|expandedUsage/)
})

test('the Möbius account and Möbius sign-in hand off to Möbius · You', () => {
  assert.match(view, /onOpenApp\?\.\('identity'\)/)
  assert.match(view, /onOpenAccount=\{openMobiusYou\}/)
  assert.doesNotMatch(view, /IdentityAccount|SettingsView\/identity\/|useIdentityQuery/)
})

test('provider-dependent model controls stay unavailable until connected', () => {
  assert.match(view, /disabled=\{!configuredProviders\.has\(row\.provider\)\}/)
  assert.match(view, /disabled=\{!configured\}/)
  assert.match(view, /configuredProviders=\{configuredProviders\}/)
})

test('Settings detail back button has a 44px touch target and keeps the shared 18px icon', () => {
  assert.match(view, /className="settings__back"[\s\S]*<ArrowLeft width=\{18\} height=\{18\}/)
  assert.match(css, /\.settings__back\s*\{[^}]*display:\s*grid;[^}]*place-items:\s*center;/s)
  const backStyle = css.match(/\.settings__back\s*\{([^}]+)\}/s)[1]
  assert.match(backStyle, /width:\s*44px;/)
  assert.match(backStyle, /height:\s*44px;/)
  assert.doesNotMatch(view, />←<\/button>/)
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

test('appearance indicator waits for the same seeded theme repaint as the palette', () => {
  assert.doesNotMatch(view, /setThemeMode\(newMode\)/)
  assert.match(view, /themeService\.toggleTheme\(queryClient, currentMode, api\)/)
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
  assert.match(updates, /Confirm restart/)
  assert.match(updates, /className={`settings__btn settings__btn--sm/)
  // Dedicated restart is vertically centered to the right of both versions.
  assert.match(updates, /platform-updates__version-row">[\s\S]*<dl className="platform-updates__versions">[\s\S]*<\/dl>[\s\S]*: 'Restart'/)
  assert.match(updateCss, /\.platform-updates__version-row\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto;[^}]*align-items:\s*center;/s)
  assert.match(updateCss, /\.platform-updates > \.platform-updates__actions\s*\{[^}]*justify-content:\s*flex-end;/s)
  assert.doesNotMatch(updateCss, /\.platform-updates > \.platform-updates__actions\s*\{[^}]*flex-direction:\s*column;/s)
  assert.doesNotMatch(updateCss, /platform-updates__maintenance/)
})


test('Updates offers one repair-or-next-step action and hides raw error dumps', () => {
  // The repair path is folded into the single primary action, not a second
  // button competing with "Review update".
  assert.match(updates, /platform-updates__actions">\s*\{repairReason \? \([\s\S]*?<UpdateRepairAction[\s\S]*?buttonRef=\{actionRef\}[\s\S]*?\) : \(\s*<button ref=\{actionRef\}/)
  assert.doesNotMatch(updates, /Finish installed update|Review all updates|Check for more/)
  // Raw npm/build/rollback error text is not shown in Settings; it goes to the
  // repair chat instead. Human-facing activation guidance stays visible.
  assert.doesNotMatch(updates, /<summary>Technical details<\/summary>/)
  assert.doesNotMatch(updates, /platform\?\.rollback_error/)
  assert.match(updates, /platform\?\.activation\?\.guidance/)
})

test('provider actions keep their full labels on one line without a width cap', () => {
  assert.match(css, /\.settings \.provider-row__action\s*\{[^}]*white-space:\s*nowrap;/s)
  assert.doesNotMatch(css, /\.settings \.provider-row__action\s*\{[^}]*max-width:/s)
})


test('simple Updates keeps versions and dedicated restart visible outside optional details', () => {
  const visible = updates.slice(0, updates.indexOf('{review && ('))
  assert.match(visible, />Updates<\/h2>/)
  assert.match(visible, /<dt>Code<\/dt>/)
  assert.match(visible, /<dt>Container<\/dt>/)
  assert.match(visible, /pressRestart\('dedicated'\)/)
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
