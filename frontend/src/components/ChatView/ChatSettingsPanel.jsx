/**
 * ChatSettingsPanel — the per-chat model + effort picker inside the
 * composer's `+` popover. Renders the design-iter row-style layout
 * (provider logo + title + subtitle + radio dot; effort slider under
 * the selected row) instead of the older `ProviderModelPicker` radio
 * list. Logic (stale-PATCH guard, optimistic state, providers fetch)
 * is unchanged from the SlashPicker era.
 *
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║                                                                  ║
 * ║   FOUR LOAD-BEARING CONTRACTS — read before refactoring          ║
 * ║                                                                  ║
 * ║   1. PER-PROVIDER EFFORT MEMORY                                  ║
 * ║      The Codex and Claude effort enums DO NOT MAP across         ║
 * ║      providers. Codex `medium` is roughly Claude `low`. So       ║
 * ║      each provider remembers its OWN last-picked effort via      ║
 * ║      `effort_by_provider: { codex, claude }` in agent_settings.  ║
 * ║      Switching providers swaps the active `effort` to that       ║
 * ║      provider's last value (fallback: current effort).           ║
 * ║      Picking effort writes BOTH `effort` (active) and the        ║
 * ║      updated `effort_by_provider` map in one PATCH.              ║
 * ║                                                                  ║
 * ║      Enum reference:                                             ║
 * ║        Codex (6): none / minimal / low / medium / high / xhigh   ║
 * ║        Claude (5):           low / medium / high / xhigh / max   ║
 * ║      Runners forward the value as-is; an out-of-enum effort      ║
 * ║      surfaces as a 400 at turn time, not at PATCH (consistent    ║
 * ║      with the platform's "reversibility over prevention"         ║
 * ║      philosophy — see mobius/CLAUDE.md design philosophy).       ║
 * ║                                                                  ║
 * ║   2. CROSS-PROVIDER LOCK                                         ║
 * ║      After a chat has at least one assistant turn, the OTHER     ║
 * ║      provider's models grey out. Sessions are not portable       ║
 * ║      between providers (Claude session id ≠ Codex thread id),   ║
 * ║      and the agent loses ALL conversation context on switch.     ║
 * ║      Same-provider model swaps stay available because both       ║
 * ║      SDKs preserve context within a session on `set_model`.      ║
 * ║      `hasAssistantTurns` is LIVE-DERIVED in the parent           ║
 * ║      (ChatView): `chatInfo.has_assistant_turns ||                ║
 * ║      messages.some(m => m.role === 'assistant')` — the           ║
 * ║      persisted flag isn't refreshed mid-turn, so the messages    ║
 * ║      check engages the lock the moment a reply lands.            ║
 * ║                                                                  ║
 * ║   3. STALE-PATCH GUARD                                           ║
 * ║      `latestReqId` is a MONOTONIC counter owned by the parent    ║
 * ║      (ComposerPopover) — NOT panel-local. A panel-local ref      ║
 * ║      would reset every time the popover closes and reopens,      ║
 * ║      defeating the guard. Rapid picks (model A → model B in      ║
 * ║      quick succession) only let the latest PATCH's response      ║
 * ║      update state; older responses return 'stale' and are        ║
 * ║      dropped.                                                    ║
 * ║                                                                  ║
 * ║   4. KEYBOARD-STATE PRESERVATION                                 ║
 * ║      `refocusChatInput` gates on `wasInputFocusedRef?.current`   ║
 * ║      (captured in ComposerPopover at + tap-time). Picking a      ║
 * ║      model or effort with the keyboard DOWN does NOT pop it      ║
 * ║      up. Every interactive row in this panel has                 ║
 * ║      `onPointerDown={(e) => e.preventDefault()}` — see           ║
 * ║      ComposerPopover.jsx's three-guard contract for the full     ║
 * ║      story.                                                      ║
 * ║                                                                  ║
 * ║   The EffortSlider component renders a stepper track — NOT       ║
 * ║   pills, NOT a chip group. The slider was explicitly chosen      ║
 * ║   over chips by the user; an earlier proposed revert to chips    ║
 * ║   was rejected. Each provider's slider has its own length        ║
 * ║   (6 stops for Codex, 5 for Claude). `findIndex` defaults to     ║
 * ║   0 when the persisted value isn't in the provider's enum, so    ║
 * ║   a cross-provider effort carryover renders gracefully.          ║
 * ║                                                                  ║
 * ║   The provider logo SVGs are inlined — the apps-sdk-ui icon     ║
 * ║   set ships UI glyphs, not vendor brand marks. Paths come        ║
 * ║   from Simple-Icons + the mobius-design-iter prototype.          ║
 * ║                                                                  ║
 * ╚══════════════════════════════════════════════════════════════════╝
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '../../api/client.js'
import { modelQueries } from '../../hooks/queries.js'
import {
  CLAUDE_MODELS,
  CODEX_MODELS,
} from '../ProviderModelPicker/ProviderModelPicker.jsx'
import ManageModelsModal from './ManageModelsModal.jsx'
import './ChatSettingsPanel.css'


/** Claude product mark — four-petal flower / starburst silhouette,
 *  the recognizable Claude.ai icon (distinct from the Anthropic
 *  angular-A corporate mark). */
function ClaudeLogo() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z" />
    </svg>
  )
}


/** Codex provider mark (knot). Path from the mobius-design-iter
 *  prototype's brand-mark symbol, MIT-licensed. */
function OpenAILogo() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" />
    </svg>
  )
}


/** Provider metadata used by the row renderer. Effort levels are
 *  scoped to each provider since the two SDKs publish different
 *  enumerations:
 *
 *  - Codex `ReasoningEffort` (openai-codex 0.131+) has 6 variants:
 *    none / minimal / low / medium / high / xhigh.
 *  - Claude SDK `EffortLevel` has 5 variants: low / medium / high /
 *    xhigh / max (xhigh + max are Opus-tier only). Anthropic's
 *    legacy `thinking.budget_tokens` integer is deprecated on Opus
 *    4.7 — the discrete enum is the supported knob.
 *
 *  Both are rendered as a horizontal stepper-slider in
 *  ChatSettingsPanel: a single track with N stops, the selected
 *  one filled, the long-form label rendered next to the track. */
const PROVIDER_INFO = {
  codex: {
    id: 'codex',
    label: 'OpenAI Codex',
    Logo: OpenAILogo,
    // Models come from the live `/api/models` registry at render
    // time; this stays as a fallback for the panel-rendered-before-
    // queries-resolve frame.
    fallbackModels: CODEX_MODELS,
    efforts: [
      { value: 'none', label: 'None' },
      { value: 'minimal', label: 'Minimal' },
      { value: 'low', label: 'Low' },
      { value: 'medium', label: 'Medium' },
      { value: 'high', label: 'High' },
      { value: 'xhigh', label: 'Extra high' },
    ],
  },
  claude: {
    id: 'claude',
    label: 'Claude Code',
    Logo: ClaudeLogo,
    fallbackModels: CLAUDE_MODELS,
    efforts: [
      { value: 'low', label: 'Low' },
      { value: 'medium', label: 'Medium' },
      { value: 'high', label: 'High' },
      { value: 'xhigh', label: 'Extra high' },
      { value: 'max', label: 'Max' },
    ],
  },
}
const PROVIDER_ORDER = ['codex', 'claude']


/** Resolves the displayed model list for `providerId` from the live
 *  registry + owner prefs.
 *
 *  Rules (matches the codex-review spec):
 *    - Sort by registry order. The backend already returns entries
 *      in KNOWN_MODELS order followed by live-only IDs, so we just
 *      keep that order.
 *    - Hide entries whose ID appears in `hiddenIds`, UNLESS that ID
 *      is the chat's currently-selected model (`selectedId`). The
 *      currently-selected model is always visible so the user can
 *      switch away from it.
 *    - Stale prefs are tolerated: an entry in `hiddenIds` that
 *      doesn't appear in the registry simply has no effect (we
 *      can't filter out something we can't see). No error.
 */
function resolveDisplayedModels(
  registryEntries, hiddenIds, selectedId,
) {
  if (!Array.isArray(registryEntries)) return []
  if (!hiddenIds || hiddenIds.length === 0) return registryEntries
  const hidden = new Set(hiddenIds)
  return registryEntries.filter(
    m => !hidden.has(m.id) || m.id === selectedId,
  )
}


/** Horizontal stepper-slider for picking an effort level. Renders
 *  a single-row track with N stops; the selected stop and any
 *  stops below it are filled, stops above are hollow. The
 *  selected stop's long-form label sits to the right of the track
 *  so the row fits in one line regardless of how many stops the
 *  provider exposes (Codex has 6, Claude has 5). */
function EffortSlider({ efforts, value, onChange }) {
  const selectedIndex = Math.max(0, efforts.findIndex(e => e.value === value))
  const selected = efforts[selectedIndex] || efforts[0]
  return (
    <div className="csp-effort">
      <div className="csp-effort__track" role="radiogroup" aria-label="Reasoning effort">
        {efforts.map((e, i) => (
          <button
            key={e.value}
            type="button"
            role="radio"
            aria-checked={i === selectedIndex}
            aria-label={e.label}
            className={
              'csp-effort__stop'
              + (i === selectedIndex ? ' csp-effort__stop--on' : '')
              + (i < selectedIndex ? ' csp-effort__stop--filled' : '')
            }
            // Keep textarea focused so the keyboard stays open.
            onPointerDown={(ev) => ev.preventDefault()}
            onClick={() => onChange(e.value)}
          />
        ))}
      </div>
      <span className="csp-effort__label">{selected.label}</span>
    </div>
  )
}


export default function ChatSettingsPanel({
  chatId,
  provider,
  effective,
  hasAssistantTurns,
  onChange,
  // Stale-PATCH guard: parent passes a ref that survives panel
  // mount/unmount. See ComposerPopover for the rationale.
  reqIdRef,
  // Tracks whether the chat textarea was focused when the popover
  // opened. Used to decide whether to refocus after a picker
  // action so the soft keyboard stays in its previous state
  // (up if it was up, down if it was down).
  wasInputFocusedRef,
}) {
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [connectedProviders, setConnectedProviders] = useState(null)
  // True while the manage-models modal is mounted. When the modal
  // opens we still want the popover-anchored picker to live in the
  // background so the user can return to it; the modal is fully
  // self-contained.
  const [manageOpen, setManageOpen] = useState(false)
  const fallbackReqId = useRef(0)
  const latestReqId = reqIdRef || fallbackReqId

  // Live model registry + owner prefs. Both ride 5-minute caches so
  // a popover open doesn't refetch. The deferred-render guard below
  // waits for BOTH to resolve before applying the prefs filter —
  // otherwise we'd render the unfiltered list briefly, then snap
  // to the filtered list once prefs land, causing flicker.
  const registryQuery = modelQueries.registry.useQuery()
  const prefsQuery = modelQueries.prefs.useQuery()
  const registry = registryQuery.data
  const prefs = prefsQuery.data
  const dataReady = !!registry && !!prefs

  const [draftModel, setDraftModel] = useState(effective?.model || '')
  const [draftEffort, setDraftEffort] = useState(effective?.effort || '')
  const [draftProvider, setDraftProvider] = useState(provider || 'claude')
  // Per-provider effort memory. The two SDKs' effort enums don't
  // map across providers (Codex `medium` ≈ Claude `low`), so each
  // provider remembers its OWN last-picked effort and we swap
  // `draftEffort` to that value when the user switches providers.
  // Initial value mirrors what the server sent under
  // `effective.effort_by_provider`; defaults to the current effort
  // bound to the current provider when the server hasn't recorded
  // any per-provider value yet.
  const [draftEffortByProvider, setDraftEffortByProvider] = useState(
    () => effective?.effort_by_provider || {},
  )

  useEffect(() => {
    setDraftModel(effective?.model || '')
    setDraftEffort(effective?.effort || '')
    setDraftEffortByProvider(effective?.effort_by_provider || {})
  }, [
    effective?.model,
    effective?.effort,
    effective?.effort_by_provider,
    chatId,
  ])

  useEffect(() => {
    setDraftProvider(provider || 'claude')
  }, [provider, chatId])

  useEffect(() => {
    let cancelled = false
    apiFetch('/auth/providers/status')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (cancelled || !data) return
        const ids = Object.entries(data)
          .filter(([, v]) => v?.authenticated)
          .map(([k]) => k)
        setConnectedProviders(ids)
      })
      .catch(() => { /* silent — picker falls back to "show all" */ })
    return () => { cancelled = true }
  }, [])

  const patchChat = useCallback(async (body) => {
    if (!chatId) return 'fail'
    const reqId = ++latestReqId.current
    setSaving(true)
    setError('')
    try {
      const res = await apiFetch(`/chats/${chatId}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      })
      if (reqId !== latestReqId.current) return 'stale'
      if (!res.ok) {
        setError('Could not save. Try again.')
        return 'fail'
      }
      const data = await res.json()
      onChange?.({
        agent_settings_json: data.agent_settings_json,
        provider: data.provider,
        effective: data.effective,
      })
      return 'ok'
    } catch {
      if (reqId !== latestReqId.current) return 'stale'
      setError('Network error.')
      return 'fail'
    } finally {
      if (reqId === latestReqId.current) setSaving(false)
    }
  }, [chatId, onChange, latestReqId])

  // Conditional refocus — only restores textarea focus if it was
  // ALREADY focused when the popover opened. Without this guard,
  // tapping + with the keyboard down and then picking a model
  // would force-focus the textarea, popping the keyboard up.
  // iOS Safari sometimes drops focus during the button click even
  // with pointerdown preventDefault; this restores it in that case
  // but only when the user was actually typing.
  const refocusChatInput = useCallback(() => {
    if (!wasInputFocusedRef?.current) return
    const el = document.querySelector('.chat__input')
    if (el) el.focus({ preventScroll: true })
  }, [wasInputFocusedRef])

  const handleEffortChange = useCallback((value) => {
    // Remember this effort under the active provider so a later
    // provider-switch restores it; ship BOTH `effort` (active) and
    // the full `effort_by_provider` map so the server stores the
    // memory verbatim.
    const nextMap = { ...draftEffortByProvider, [draftProvider]: value }
    setDraftEffort(value)
    setDraftEffortByProvider(nextMap)
    patchChat({
      agent_settings_json: { effort: value, effort_by_provider: nextMap },
    })
    refocusChatInput()
  }, [draftProvider, draftEffortByProvider, patchChat, refocusChatInput])

  const handlePickModel = useCallback(async (value, providerValue) => {
    refocusChatInput()
    const prevProvider = draftProvider
    const prevModel = draftModel
    const prevEffort = draftEffort
    setDraftModel(value)
    if (providerValue !== draftProvider) {
      // Cross-provider switch: restore this provider's last-known
      // effort (or fall back to the value already on screen — which
      // becomes that provider's first memory once they accept it).
      // The effort enums don't overlap perfectly (Codex has `none`
      // + `minimal` that Claude lacks, Claude has `max` that Codex
      // lacks), so a fallback that's invalid for the new provider
      // is harmless — the runner ignores unknown values at turn
      // time. The picker also auto-defaults the slider to index 0
      // if the persisted value doesn't appear in the provider's
      // enum (see EffortSlider's findIndex/Math.max guard).
      const nextEffort = draftEffortByProvider[providerValue] ?? draftEffort
      setDraftProvider(providerValue)
      setDraftEffort(nextEffort)
      const outcome = await patchChat({
        provider: providerValue,
        agent_settings_json: { model: value, effort: nextEffort },
      })
      if (outcome === 'fail') {
        setDraftProvider(prevProvider)
        setDraftModel(prevModel)
        setDraftEffort(prevEffort)
      }
      return
    }
    const outcome = await patchChat({
      agent_settings_json: { model: value },
    })
    if (outcome === 'fail') setDraftModel(prevModel)
  }, [
    draftProvider,
    draftModel,
    draftEffort,
    draftEffortByProvider,
    patchChat,
    refocusChatInput,
  ])

  const isCodex = draftProvider === 'codex'
  const codexSwitchWarning = (
    isCodex && hasAssistantTurns
    && effective?.model && draftModel
    && draftModel !== effective?.model
  )

  const connectedSet = connectedProviders ? new Set(connectedProviders) : null
  const hiddenIds = prefs?.hidden_ids || []

  // Build the per-provider displayed-models list once per render.
  // Falls back to the bundled CLAUDE_MODELS / CODEX_MODELS until the
  // registry query resolves — but we gate the actual model rows on
  // `dataReady` below (showing a skeleton) to avoid the flicker the
  // spec calls out (prefs filter applied AFTER the unfiltered list
  // already painted).
  const displayedByProvider = useMemo(() => {
    const out = {}
    for (const pid of PROVIDER_ORDER) {
      const live = registry?.[pid]
      const source = Array.isArray(live) && live.length
        ? live
        // Fallback: shape the static list to match the registry
        // entry shape so the renderer downstream doesn't need to
        // branch. Used only when the registry query is still
        // loading AND the deferred-render gate has been bypassed
        // (it normally hasn't — see `dataReady` above).
        : PROVIDER_INFO[pid].fallbackModels.map(m => (
          { id: m.value, label: m.label, provider: pid, available: true }
        ))
      const selectedHere = draftProvider === pid ? draftModel : null
      out[pid] = resolveDisplayedModels(source, hiddenIds, selectedHere)
    }
    return out
  }, [registry, hiddenIds, draftModel, draftProvider])

  return (
    <div className="csp">
      <div className="csp__label">Model</div>
      {!dataReady && (
        // Skeleton placeholder while registry + prefs resolve. Two
        // rows mirror the typical visible count without committing
        // to a specific model list — covers the case where prefs
        // hide most of the rows once they land.
        <div className="csp__skeleton" aria-hidden="true">
          <div className="csp__skeleton-row" />
          <div className="csp__skeleton-row" />
        </div>
      )}
      {dataReady && PROVIDER_ORDER.map(pid => {
        const info = PROVIDER_INFO[pid]
        // Hide providers the user hasn't authenticated, EXCEPT the
        // chat's currently-selected provider (so the user can always
        // see what's active and switch away from it).
        if (connectedSet && !connectedSet.has(pid) && draftProvider !== pid) {
          return null
        }
        // Provider lock: once the chat has assistant turns, the
        // other provider's models grey out — cross-provider switch
        // loses session context (see CLAUDE.md slash-picker section).
        // Same-provider model swaps remain available because both
        // SDKs preserve context within a session on model change.
        const isCrossProvider = hasAssistantTurns && pid !== draftProvider
        const models = displayedByProvider[pid] || []
        return models.map(m => {
          const isSelected = draftModel === m.id && draftProvider === pid
          return (
            <div key={`${pid}-${m.id}`}>
              <button
                type="button"
                className={`csp-row${isSelected ? ' csp-row--selected' : ''}${isCrossProvider ? ' csp-row--locked' : ''}`}
                // Keep textarea focused so the keyboard stays open.
                onPointerDown={(ev) => ev.preventDefault()}
                onClick={() => !isCrossProvider && handlePickModel(m.id, pid)}
                disabled={isCrossProvider}
                title={isCrossProvider ? 'Cross-provider switch not allowed after the chat has started' : undefined}
              >
                <span className="csp-row__icon"><info.Logo /></span>
                <span className="csp-row__main">
                  <span className="csp-row__title">{m.label}</span>
                  <span className="csp-row__sub">{info.label}</span>
                </span>
                <span className="csp-row__dot" />
              </button>
              {isSelected && (
                <EffortSlider
                  efforts={info.efforts}
                  value={draftEffort}
                  onChange={handleEffortChange}
                />
              )}
            </div>
          )
        })
      })}
      {dataReady && (
        <button
          type="button"
          className="csp__manage"
          // Keep textarea focused so the keyboard stays open.
          onPointerDown={(ev) => ev.preventDefault()}
          onClick={() => setManageOpen(true)}
        >
          + Manage models
        </button>
      )}
      {(codexSwitchWarning || error) && (
        <div className="csp__foot">
          {codexSwitchWarning && (
            <p className="csp__note">
              Codex injects a one-time model-switch note on the next
              reply — may briefly affect that turn.
            </p>
          )}
          {error && <p className="csp__error">{error}</p>}
        </div>
      )}
      {manageOpen && (
        <ManageModelsModal
          onClose={() => setManageOpen(false)}
          providerOrder={PROVIDER_ORDER}
          providerInfo={PROVIDER_INFO}
        />
      )}
    </div>
  )
}
