/**
 * Shared model + effort picker. Used by:
 *   • ChatSettingsPanel (per-chat override, via PATCH /api/chats/{id})
 *   • SlashPicker (RETIRED — kept as reference; no active importers)
 *
 * ChatSettingsPanel is the live consumer; SlashPicker still compiles
 * against this file but is no longer wired into any composer. The
 * shared option lists and radio-list shape are extracted here so a
 * model addition in this file lights up the live picker with no code
 * churn elsewhere.
 *
 * Why not a select element: radio rows make the current choice and
 * the alternatives visible at once, which matches the Claude.ai and
 * Codex CLI `/model` pickers users are already trained on.
 */

import { useId } from 'react'
import './ProviderModelPicker.css'


/** Known models per provider. The set the SDK accepts is wider than
 *  what we surface — the picker shows the user-facing names only.
 *  Add to this list when a new model lands; the value is what gets
 *  passed to ClaudeAgentOptions(model=...) / Codex thread.start. */
// Anthropic switched to dateless pinned IDs starting with the 4.6
// generation — `claude-opus-4-8` IS the pinned snapshot, no date
// suffix exists. Dated entries for older generations stay listed so
// existing chats that persisted them in agent_settings_json keep
// resolving (the API treats them as aliases).
export const CLAUDE_MODELS = [
  { value: 'claude-opus-4-8', label: 'Opus 4.8' },
  { value: 'claude-opus-4-7', label: 'Opus 4.7' },
  { value: 'claude-opus-4-6', label: 'Opus 4.6' },
  { value: 'claude-opus-4-5-20251001', label: 'Opus 4.5' },
  { value: 'claude-sonnet-4-6', label: 'Sonnet 4.6' },
  { value: 'claude-sonnet-4-7-20251215', label: 'Sonnet 4.7' },
  { value: 'claude-sonnet-4-5-20251001', label: 'Sonnet 4.5' },
  { value: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5' },
]

export const CODEX_MODELS = [
  // gpt-5.5 became the default Codex model on 2026-04-24. gpt-5.4
  // stays as fallback. -codex suffix variants (gpt-5.3-codex etc.)
  // require API-key auth; ChatGPT-account auth (Möbius's Codex
  // bridge) returns 400 for them — drop until per-auth model gating
  // is in place.
  { value: 'gpt-5.5', label: 'gpt-5.5' },
  { value: 'gpt-5.4', label: 'gpt-5.4' },
]

// Effort tiers shared by both providers. Codex's ReasoningEffort enum
// exposes none/minimal/low/medium/high/xhigh — we surface the upper
// four since the lower two are rarely useful for build work. `max`
// was previously exposed for Claude but isn't a recognized Codex tier;
// dropped for consistency until both providers agree on a fifth level.
// Used internally by the radio-list render below; no external
// importers (ChatSettingsPanel renders its own stepper).
const EFFORT_LEVELS = [
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'xhigh', label: 'Extra high' },
]

export function modelsForProvider(providerId) {
  if (providerId === 'codex') return CODEX_MODELS
  return CLAUDE_MODELS
}


/**
 * The radio-list shape both callers render. Props:
 *   provider          — 'claude' | 'codex' (controls which model is
 *                       currently selected when shown as combined list)
 *   model             — currently selected model value (or undefined)
 *   effort            — currently selected effort value (or undefined)
 *   onPickModel(model, provider)
 *                     — fires when the user picks a model row. The
 *                       caller is expected to PATCH provider+model in
 *                       one request when provider differs from current.
 *                       When omitted, the picker renders read-only.
 *   onChangeEffort(v) — receives new effort value
 *   disabledReason    — string shown grayed-out next to the Model
 *                       heading as a contextual note (e.g. provider
 *                       locked). Display only — per-row disabling is
 *                       driven by lockedToProvider.
 *   lockedToProvider  — provider id ('claude' | 'codex') that the
 *                       chat is pinned to (sessions are not
 *                       cross-provider portable). When set, model rows
 *                       belonging to the OTHER provider are disabled.
 *                       Same-provider model swaps remain available.
 *                       Null/undefined means no lock.
 *   connectedProviders — Set/array of provider ids that are
 *                       authenticated. When non-null, the picker only
 *                       renders sections for those providers (and the
 *                       currently-selected provider, so the user can
 *                       always see what's active). Null = show all.
 */

// Provider section metadata. Display order is intentional: Codex's
// "OpenAI Codex" first matches the setup wizard's Codex-first
// ordering (ticket 025); Claude follows. Adding a provider here also
// requires extending CLAUDE_MODELS / CODEX_MODELS above.
const PROVIDER_SECTIONS = [
  { provider: 'codex', label: 'OpenAI Codex', models: CODEX_MODELS },
  { provider: 'claude', label: 'Claude Code', models: CLAUDE_MODELS },
]

export default function ProviderModelPicker({
  provider,
  model,
  effort,
  onPickModel,
  onChangeEffort,
  disabledReason,
  lockedToProvider,
  connectedProviders,
}) {
  // Per-instance namespace so two pickers in the same document don't
  // share a radio group.
  const groupId = useId()

  // Normalize the connected-filter to a Set for O(1) lookup. Falsy
  // means "no filter, show everything". The currently-selected
  // provider is ALWAYS shown so the user can see what's active even
  // if that provider is somehow disconnected (e.g. token expired
  // mid-chat) — they need the row to switch away from it.
  const connectedSet = connectedProviders
    ? new Set(connectedProviders)
    : null
  const sections = PROVIDER_SECTIONS.filter(s =>
    !connectedSet || connectedSet.has(s.provider) || provider === s.provider
  )
  // Drop a lock that references an unsupported provider (e.g. a
  // historical chat with chat.provider='gemini'). Without this the
  // lock matches no section and disables every model row — the user
  // is stuck. Treating an unknown lock as "no lock" lets them pick
  // any model and effectively rescue the chat.
  const knownLock = PROVIDER_SECTIONS.some(s => s.provider === lockedToProvider)
    ? lockedToProvider
    : null

  return (
    <div className="pmp">
      <div className="pmp__group">
        <div className="pmp__group-head">
          <span className="pmp__group-title">Model</span>
          {disabledReason && (
            <span className="pmp__group-hint">{disabledReason}</span>
          )}
        </div>
        {sections.map(section => (
          <div key={section.provider} className="pmp__section">
            <div className="pmp__section-head">{section.label}</div>
            <div className="pmp__radios">
              {section.models.map(m => {
                const isSelected = model === m.value
                  && provider === section.provider
                const isDisabled = !!knownLock
                  && section.provider !== knownLock
                return (
                  <label
                    key={`${section.provider}-${m.value}`}
                    className={`pmp__radio${isSelected ? ' pmp__radio--on' : ''}${isDisabled ? ' pmp__radio--disabled' : ''}`}
                  >
                    <input
                      type="radio"
                      name={`pmp-model-${groupId}`}
                      value={`${section.provider}/${m.value}`}
                      checked={isSelected}
                      disabled={isDisabled}
                      onChange={() => onPickModel?.(m.value, section.provider)}
                    />
                    <span className="pmp__radio-dot" />
                    <span className="pmp__radio-label">{m.label}</span>
                  </label>
                )
              })}
            </div>
          </div>
        ))}
      </div>

      <div className="pmp__group">
        <div className="pmp__group-head">
          <span className="pmp__group-title">Effort</span>
        </div>
        <div className="pmp__radios pmp__radios--row">
          {EFFORT_LEVELS.map(e => (
            <label
              key={e.value}
              className={`pmp__radio pmp__radio--chip${effort === e.value ? ' pmp__radio--on' : ''}`}
            >
              <input
                type="radio"
                name={`pmp-effort-${groupId}`}
                value={e.value}
                checked={effort === e.value}
                onChange={() => onChangeEffort?.(e.value)}
              />
              <span className="pmp__radio-label">{e.label}</span>
            </label>
          ))}
        </div>
      </div>
    </div>
  )
}
